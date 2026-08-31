-- ═══════════════════════════════════════════════════════════════════════
-- Construction de la couche silver depuis bronze.
-- Recalcul intégral : chaque table est vidée puis reconstruite.
-- Le marqueur {run_id} est substitué par le pipeline.
--
-- SEUILS D'ALERTE (§4 « relevés en alerte »). Le sujet ne les fournit pas :
-- ceux retenus sont conventionnels et signalés comme à valider par le corps
-- médical dans les limites du rapport.
--     fréquence cardiaque : < 40 ou > 120 bpm
--     saturation SpO2     : < 92 %
--     température         : > 38,5 °C
--
-- PLAGES DE PLAUSIBILITÉ (§3, fournies par le sujet) — au-delà, le relevé
-- n'est pas une alerte mais une donnée invalide :
--     FC 20–250 bpm · SpO2 50–100 % · température 30–45 °C
-- ═══════════════════════════════════════════════════════════════════════

TRUNCATE TABLE silver.rejets;

-- ── 1. PATIENTS ─────────────────────────────────────────────────────────
-- `patients` est un SNAPSHOT CUMULATIF : chaque fichier journalier contient
-- toute la population connue à date (16 200 lignes pour 6 000 patients).
-- On retient la version du jour de dépôt le plus récent.
TRUNCATE TABLE silver.patients;

INSERT INTO silver.patients
SELECT
    patient_pseudo,
    argMax(birth_year,  _jour_depot),
    argMax(sex,         _jour_depot),
    argMax(region_code, _jour_depot),
    max(_jour_depot),
    '{run_id}', now()
FROM bronze.patients
GROUP BY patient_pseudo;


-- ── 2. SÉJOURS ──────────────────────────────────────────────────────────
-- Règle du sujet : écarter si discharge_ts < admission_ts.
-- Règle du sujet : discharge_ts vide = séjour EN COURS, légitime, conservé.
-- Décision documentée : discharge_mode vide sur séjour clos (1 992 lignes)
--   est normalisé en 'inconnu' plutôt qu'écarté — la durée reste calculable.
INSERT INTO silver.rejets
SELECT 'sejours', stay_id, 'incoherence_temporelle',
       concat('admission=', toString(admission_ts), ' sortie=', toString(discharge_ts)),
       '{run_id}', now()
FROM bronze.sejours
WHERE discharge_ts IS NOT NULL AND discharge_ts < admission_ts;

TRUNCATE TABLE silver.sejours;

INSERT INTO silver.sejours
SELECT
    s.stay_id,
    s.patient_pseudo,
    s.service_code,
    coalesce(r.service_label, 'inconnu'),
    s.admission_ts,
    s.discharge_ts,
    s.admission_mode,
    if(s.discharge_mode = '', 'inconnu', s.discharge_mode),
    if(s.discharge_ts IS NULL, NULL,
       dateDiff('minute', s.admission_ts, s.discharge_ts) / 1440.0),
    s.discharge_ts IS NULL,
    -- Approximé à l'année : conséquence directe de la généralisation RGPD
    -- de la date de naissance. Erreur maximale 1 an, documentée.
    if(p.patient_pseudo = '', NULL, toYear(s.admission_ts) - p.birth_year),
    '{run_id}', now()
FROM bronze.sejours AS s
LEFT JOIN bronze.ref_services AS r ON s.service_code = r.service_code
LEFT JOIN silver.patients     AS p ON s.patient_pseudo = p.patient_pseudo
WHERE s.discharge_ts IS NULL OR s.discharge_ts >= s.admission_ts;


-- ── 3. DIAGNOSTICS ──────────────────────────────────────────────────────
-- Aucune anomalie propre : intégrité référentielle intégralement vérifiée
-- (0 code hors nomenclature, 0 séjour orphelin, 1 principal par séjour).
-- Seuls les diagnostics rattachés à un séjour écarté sont retirés, pour ne
-- pas laisser d'orphelins en silver.
INSERT INTO silver.rejets
SELECT 'diagnostics', concat(stay_id, '/', code_cim10), 'sejour_ecarte',
       'diagnostic rattaché à un séjour exclu pour incohérence temporelle',
       '{run_id}', now()
FROM bronze.diagnostics
WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours);

TRUNCATE TABLE silver.diagnostics;

INSERT INTO silver.diagnostics
SELECT d.stay_id, d.code_cim10, d.type_diag,
       coalesce(c.libelle, 'inconnu'), '{run_id}', now()
FROM bronze.diagnostics AS d
LEFT JOIN bronze.ref_cim10 AS c ON d.code_cim10 = c.code_cim10
WHERE d.stay_id IN (SELECT stay_id FROM silver.sejours);


-- ── 4. MONITORING ───────────────────────────────────────────────────────
-- Règle du sujet : écarter hors plage physiologique.
--
-- CONSTAT : FC et SpO2 sont TOUJOURS aberrantes ensemble (1 369 fois les
-- deux, 0 fois l'une seule), sur 4 combinaisons de butée — (0|500) × (0|120).
-- Ce n'est pas du bruit de mesure mais un CAPTEUR DÉCONNECTÉ. Le relevé
-- entier est écarté : un capteur en panne ne garantit la fiabilité d'aucune
-- de ses mesures, et ne conserver que la température créerait des relevés
-- partiels au grain incohérent.
INSERT INTO silver.rejets
SELECT 'monitoring', concat(stay_id, '@', toString(ts)), 'capteur_hors_plage',
       concat('fc=', toString(heart_rate), ' spo2=', toString(spo2),
              ' temp=', toString(temp_c)),
       '{run_id}', now()
FROM bronze.monitoring
WHERE heart_rate NOT BETWEEN 20 AND 250
   OR spo2       NOT BETWEEN 50 AND 100
   OR temp_c     NOT BETWEEN 30 AND 45;

-- Les relevés rattachés à un séjour écarté partent avec lui. C'est bien la
-- MÊME cause que l'incohérence temporelle : sur un séjour dont la sortie
-- précède l'admission, tout relevé est « après la sortie » par construction.
INSERT INTO silver.rejets
SELECT 'monitoring', concat(stay_id, '@', toString(ts)), 'sejour_ecarte',
       'relevé rattaché à un séjour exclu pour incohérence temporelle',
       '{run_id}', now()
FROM bronze.monitoring
WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours)
  AND heart_rate BETWEEN 20 AND 250
  AND spo2       BETWEEN 50 AND 100
  AND temp_c     BETWEEN 30 AND 45;

TRUNCATE TABLE silver.monitoring;

INSERT INTO silver.monitoring
SELECT
    m.stay_id, m.ts, m.heart_rate, m.spo2, m.temp_c,
    (m.heart_rate < 40 OR m.heart_rate > 120) AS alerte_fc,
    (m.spo2 < 92)                             AS alerte_spo2,
    (m.temp_c > 38.5)                         AS alerte_temp,
    (alerte_fc OR alerte_spo2 OR alerte_temp) AS en_alerte,
    '{run_id}', now()
FROM bronze.monitoring AS m
WHERE m.heart_rate BETWEEN 20 AND 250
  AND m.spo2       BETWEEN 50 AND 100
  AND m.temp_c     BETWEEN 30 AND 45
  AND m.stay_id IN (SELECT stay_id FROM silver.sejours);
