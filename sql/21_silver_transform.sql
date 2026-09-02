-- ═══════════════════════════════════════════════════════════════════════
-- Construction de la couche silver depuis bronze.
-- Recalcul intégral : chaque table est vidée puis reconstruite.
-- Le marqueur {run_id} est substitué par le pipeline.
--
-- CE QUE FAIT CETTE COUCHE, ET RIEN D'AUTRE : appliquer les règles de
-- VALIDITÉ que le sujet fournit, tracer chaque exclusion, enrichir depuis
-- les référentiels. Les règles métier paramétrables — seuils d'alerte, âge
-- à l'événement — sont en gold : voir l'en-tête de 20_silver.sql.
--
-- PLAGES DE PLAUSIBILITÉ (§3, fournies par le sujet). Un relevé qui en sort
-- n'est pas une alerte, c'est une donnée invalide :
--     FC 20–250 bpm · SpO2 50–100 % · température 30–45 °C
--
-- CONTRÔLES DU §3 DU SUJET, un par un :
--   patients    doublons -> déduplication (snapshot cumulatif)
--   patients    sexe normalisé M/F -> sinon 'inconnu', ligne CORRIGÉE
--   sejours     discharge_ts < admission_ts -> ÉCARTÉE
--   sejours     discharge_ts vide = séjour en cours -> conservé
--   sejours     dates valides -> une date illisible ÉCARTE la ligne
--   monitoring  plages physiologiques -> ÉCARTÉE
--
-- Toute ligne fautive part en `quarantaine.rejets` avec son motif et son
-- issue, de sorte que  bronze = silver + quarantaine('ecarte')  à la ligne
-- près, les corrections étant tracées sans être soustraites.
-- ═══════════════════════════════════════════════════════════════════════
TRUNCATE TABLE quarantaine.rejets;

-- ── 1. PATIENTS ─────────────────────────────────────────────────────────
-- `patients` est un SNAPSHOT CUMULATIF : chaque fichier journalier contient
-- toute la population connue à date (18 000 lignes pour 6 000 patients).
-- On retient la version du jour de dépôt le plus récent.
TRUNCATE TABLE silver.patients;

-- Règle du sujet : « sexe normalisé (M/F) ». La casse et les espaces sont
-- redressés ; toute autre valeur devient 'inconnu'.
--
-- Décision documentée : on CORRIGE, on n'écarte pas. Écarter le patient
-- orphelinerait tous ses séjours pour un attribut purement descriptif, qui
-- n'entre dans aucune clé. La ligne est donc conservée et signalée.
INSERT INTO
    quarantaine.rejets
SELECT
    'patients',
    patient_pseudo,
    'sexe_non_normalise',
    'corrige',
    concat('sexe source [', sex_source, '] -> inconnu'),
    _jour_depot_retenu,
    _fichier_source_retenu,
    '{run_id}',
    now()
FROM
    (
        SELECT
            patient_pseudo,
            argMax(sex, _jour_depot) AS sex_source,
            max(_jour_depot) AS _jour_depot_retenu,
            argMax(_fichier_source, _jour_depot) AS _fichier_source_retenu
        FROM
            bronze.patients
        GROUP BY
            patient_pseudo
    )
WHERE
    upper(trim(sex_source)) NOT IN ('M', 'F');

INSERT INTO
    silver.patients
SELECT
    patient_pseudo,
    birth_year,
    if(sex IN ('M', 'F'), sex, 'inconnu'),
    region_code,
    _jour_depot_retenu,
    _fichier_source_retenu,
    '{run_id}',
    now()
FROM
    (
        SELECT
            patient_pseudo,
            argMax(birth_year, _jour_depot) AS birth_year,
            upper(trim(argMax(sex, _jour_depot))) AS sex,
            argMax(region_code, _jour_depot) AS region_code,
            max(_jour_depot) AS _jour_depot_retenu,
            argMax(_fichier_source, _jour_depot) AS _fichier_source_retenu
        FROM
            bronze.patients
        GROUP BY
            patient_pseudo
    );

-- ── 2. SÉJOURS ──────────────────────────────────────────────────────────
-- Règle du sujet : écarter si discharge_ts < admission_ts.
-- Règle du sujet : discharge_ts vide = séjour EN COURS, légitime, conservé.
-- Règle du sujet : « dates valides ».
-- Décision documentée : discharge_mode vide est normalisé en 'inconnu' plutôt
--   qu'écarté — la durée reste calculable. Sur ce dépôt, seuls les 683 séjours
--   EN COURS sont concernés ; aucun séjour clos n'est privé de mode de sortie.
--
-- L'ordre compte : une date illisible est écartée D'ABORD, sinon la
-- comparaison temporelle porterait sur un NULL et la ligne échapperait aux
-- deux contrôles. Les deux motifs sont donc mutuellement exclusifs, et
-- l'équation de conservation ne double-compte aucune ligne.
INSERT INTO
    quarantaine.rejets
SELECT
    'sejours',
    stay_id,
    'date_illisible',
    'ecarte',
    if(
        admission_ts IS NULL,
        'date d''admission illisible dans la source',
        'date de sortie non vide et illisible dans la source'
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours
WHERE
    admission_ts IS NULL
    OR _discharge_illisible = 1;

INSERT INTO
    quarantaine.rejets
SELECT
    'sejours',
    stay_id,
    'incoherence_temporelle',
    'ecarte',
    concat(
        'admission=',
        toString(admission_ts),
        ' sortie=',
        toString(discharge_ts)
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours
WHERE
    admission_ts IS NOT NULL
    AND _discharge_illisible = 0
    AND discharge_ts IS NOT NULL
    AND discharge_ts < admission_ts;

TRUNCATE TABLE silver.sejours;

INSERT INTO
    silver.sejours
SELECT
    s.stay_id,
    s.patient_pseudo,
    s.service_code,
    coalesce(r.service_label, 'inconnu'),
    assumeNotNull(s.admission_ts),
    s.discharge_ts,
    s.admission_mode,
    if(
        s.discharge_mode = '',
        'inconnu',
        s.discharge_mode
    ),
    if(
        s.discharge_ts IS NULL,
        NULL,
        dateDiff('minute', s.admission_ts, s.discharge_ts) / 1440.0
    ),
    s.discharge_ts IS NULL,
    s._jour_depot,
    s._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours AS s
    LEFT JOIN bronze.ref_services AS r ON s.service_code = r.service_code
WHERE
    s.admission_ts IS NOT NULL
    AND s._discharge_illisible = 0
    AND (
        s.discharge_ts IS NULL
        OR s.discharge_ts >= s.admission_ts
    );

-- ── 3. DIAGNOSTICS ──────────────────────────────────────────────────────
-- Aucune anomalie propre : intégrité référentielle intégralement vérifiée
-- (0 code hors nomenclature, 0 séjour orphelin, 1 principal par séjour).
-- Seuls les diagnostics rattachés à un séjour écarté sont retirés, pour ne
-- pas laisser d'orphelins en silver.
INSERT INTO
    quarantaine.rejets
SELECT
    'diagnostics',
    concat(stay_id, '/', code_cim10),
    'sejour_ecarte',
    'ecarte',
    'diagnostic rattaché à un séjour exclu pour incohérence temporelle',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.diagnostics
WHERE
    stay_id NOT IN (
        SELECT
            stay_id
        FROM
            silver.sejours
    );

TRUNCATE TABLE silver.diagnostics;

INSERT INTO
    silver.diagnostics
SELECT
    d.stay_id,
    d.code_cim10,
    d.type_diag,
    coalesce(c.libelle, 'inconnu'),
    d._jour_depot,
    d._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.diagnostics AS d
    LEFT JOIN bronze.ref_cim10 AS c ON d.code_cim10 = c.code_cim10
WHERE
    d.stay_id IN (
        SELECT
            stay_id
        FROM
            silver.sejours
    );

-- ── 4. MONITORING ───────────────────────────────────────────────────────
-- Règle du sujet : écarter hors plage physiologique.
--
-- CONSTAT : FC et SpO2 sont TOUJOURS aberrantes ensemble (858 fois les
-- deux, 0 fois l'une seule), sur 4 combinaisons de butée — (0|500) × (0|120).
-- Ce n'est pas du bruit de mesure mais un CAPTEUR DÉCONNECTÉ. Le relevé
-- entier est écarté : un capteur en panne ne garantit la fiabilité d'aucune
-- de ses mesures, et ne conserver que la température créerait des relevés
-- partiels au grain incohérent.
INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(stay_id, '@', toString(ts)),
    'capteur_hors_plage',
    'ecarte',
    concat(
        'fc=',
        toString(heart_rate),
        ' spo2=',
        toString(spo2),
        ' temp=',
        toString(temp_c)
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring
WHERE
    heart_rate NOT BETWEEN 20
    AND 250
    OR spo2 NOT BETWEEN 50
    AND 100
    OR temp_c NOT BETWEEN 30
    AND 45;

-- Les relevés rattachés à un séjour écarté partent avec lui. C'est bien la
-- MÊME cause que l'incohérence temporelle : sur un séjour dont la sortie
-- précède l'admission, tout relevé est « après la sortie » par construction.
INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(stay_id, '@', toString(ts)),
    'sejour_ecarte',
    'ecarte',
    'relevé rattaché à un séjour exclu pour incohérence temporelle',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring
WHERE
    stay_id NOT IN (
        SELECT
            stay_id
        FROM
            silver.sejours
    )
    AND heart_rate BETWEEN 20
    AND 250
    AND spo2 BETWEEN 50
    AND 100
    AND temp_c BETWEEN 30
    AND 45;

TRUNCATE TABLE silver.monitoring;

INSERT INTO
    silver.monitoring
SELECT
    m.stay_id,
    m.ts,
    m.heart_rate,
    m.spo2,
    m.temp_c,
    m._jour_depot,
    m._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring AS m
WHERE
    m.heart_rate BETWEEN 20
    AND 250
    AND m.spo2 BETWEEN 50
    AND 100
    AND m.temp_c BETWEEN 30
    AND 45
    AND m.stay_id IN (
        SELECT
            stay_id
        FROM
            silver.sejours
    );
