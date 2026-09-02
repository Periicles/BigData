-- ═══════════════════════════════════════════════════════════════════════
-- Construction du modèle en étoile depuis silver. Recalcul intégral.
--
-- ORDRE IMPOSÉ : les dimensions d'abord, les faits ensuite. Un fait qui a
-- besoin d'un attribut de dimension le lit DANS la dimension — il ne le
-- re-dérive pas depuis silver. C'est ce qui fait de l'étoile un modèle et
-- non trois tables qui se ressemblent.
--
-- Une expression réutilisée partout : la tranche d'âge de 10 ans.
--   concat(toString(intDiv(age, 10) * 10), '-', toString(intDiv(age, 10) * 10 + 9))
-- Elle est calculée dans les faits, et NON dans dim_patient : l'âge est un
-- attribut de l'ÉVÉNEMENT (l'âge qu'avait le patient lors de ce séjour), pas
-- une propriété stable de la personne. `dim_patient` ne porte donc que
-- `birth_year`, et le fait croise cette année avec sa propre date
-- d'admission — c'est la définition même d'un attribut dérivé de fait.
--
-- SEUILS D'ALERTE — {fc_basse}, {fc_haute}, {spo2_basse}, {temp_haute} sont
-- substitués depuis la configuration (eds/config.py), au même titre que
-- {run_id}. Ils ne sont pas en silver et ne sont pas en dur : il n'existe
-- aucun seuil d'alerte réglementaire, seulement des valeurs par défaut de
-- constructeur que chaque service — voire chaque patient — ajuste. C'est un
-- paramètre d'exploitation, pas une propriété de la donnée.
-- ═══════════════════════════════════════════════════════════════════════

-- ══ DIMENSIONS ══════════════════════════════════════════════════════════
-- Construites en premier : les faits s'appuient dessus.
--
-- `dim_patient` vient de silver, parce que la source est un snapshot
-- cumulatif qu'il faut d'abord dédupliquer. Les deux nomenclatures viennent
-- directement de bronze : elles sont déjà des dimensions, elles n'ont ni
-- doublon ni règle qualité à subir.

TRUNCATE TABLE gold_pilotage.dim_patient;
INSERT INTO gold_pilotage.dim_patient
SELECT patient_pseudo, birth_year, sex, region_code, '{run_id}', now()
FROM silver.patients;

TRUNCATE TABLE gold_pilotage.dim_service;
INSERT INTO gold_pilotage.dim_service
SELECT service_code, service_label, '{run_id}', now()
FROM bronze.ref_services;

TRUNCATE TABLE gold_pilotage.dim_cim10;
INSERT INTO gold_pilotage.dim_cim10
SELECT code_cim10, libelle, '{run_id}', now()
FROM bronze.ref_cim10;


-- ══ FAIT SÉJOUR ═════════════════════════════════════════════════════════
-- Le drapeau de réadmission est calculé ici, une fois pour toutes.
--
-- Séjour index = séjour CLOS dont le patient n'est PAS décédé. Sans cette
-- exclusion, 223 paires compteraient un patient réadmis après sa mort.
--
-- `age_au_sejour` croise dim_patient.birth_year et l'admission du séjour.
-- La jointure est un LEFT JOIN : un séjour dont le patient serait absent de
-- la dimension doit rester dans le fait avec un âge NULL, jamais disparaître
-- silencieusement. L'intégrité fait -> dimension est contrôlée à part
-- (tests.verifier qualite).
TRUNCATE TABLE gold_pilotage.fact_sejour;

INSERT INTO gold_pilotage.fact_sejour
WITH readmissions AS (
    SELECT i.stay_id,
           max(if(s.admission_ts >  i.discharge_ts
              AND s.admission_ts <= i.discharge_ts + INTERVAL 30 DAY, 1, 0)) AS readmis
    FROM silver.sejours AS i
    LEFT JOIN silver.sejours AS s
           ON i.patient_pseudo = s.patient_pseudo AND s.stay_id != i.stay_id
    WHERE i.est_en_cours = 0 AND i.discharge_mode != 'deces'
    GROUP BY i.stay_id
)
SELECT
    s.stay_id,
    s.patient_pseudo,
    s.service_code,
    toDate(s.admission_ts),
    if(s.discharge_ts IS NULL, NULL, toDate(s.discharge_ts)),
    s.admission_ts,
    s.discharge_ts,
    s.admission_mode,
    s.discharge_mode,
    -- Approximé à l'année : conséquence directe de la généralisation RGPD
    -- de la date de naissance. Erreur maximale 1 an, documentée.
    if(p.patient_pseudo = '', NULL,
       toYear(s.admission_ts) - p.birth_year)         AS age_au_sejour,
    if(age_au_sejour IS NULL, 'inconnu',
       concat(toString(intDiv(age_au_sejour, 10) * 10), '-',
              toString(intDiv(age_au_sejour, 10) * 10 + 9))),
    s.duree_jours,
    s.est_en_cours,
    s.admission_mode = 'urgence',
    s.est_en_cours = 0 AND s.discharge_mode != 'deces',
    coalesce(r.readmis, 0),
    '{run_id}', now()
FROM silver.sejours AS s
LEFT JOIN gold_pilotage.dim_patient AS p ON s.patient_pseudo = p.patient_pseudo
LEFT JOIN readmissions             AS r ON s.stay_id        = r.stay_id;


-- ══ FAIT DIAGNOSTIC ═════════════════════════════════════════════════════
-- Patient, âge et sexe sont dénormalisés depuis la dimension : les questions
-- de recherche portent sur des cohortes de patients par pathologie, cette
-- copie leur évite deux jointures.
--
-- Le sexe est lu dans dim_patient, pas re-lu dans silver.patients : c'est un
-- attribut de dimension, il n'a qu'une seule source de vérité.
TRUNCATE TABLE gold_pilotage.fact_diagnostic;

INSERT INTO gold_pilotage.fact_diagnostic
SELECT
    d.stay_id,
    s.patient_pseudo,
    d.code_cim10,
    s.service_code,
    toDate(s.admission_ts),
    d.type_diag,
    d.type_diag = 'principal',
    if(p.patient_pseudo = '', NULL,
       toYear(s.admission_ts) - p.birth_year)         AS age_au_sejour,
    if(age_au_sejour IS NULL, 'inconnu',
       concat(toString(intDiv(age_au_sejour, 10) * 10), '-',
              toString(intDiv(age_au_sejour, 10) * 10 + 9))),
    if(p.patient_pseudo = '', 'inconnu', p.sexe),
    '{run_id}', now()
FROM silver.diagnostics AS d
INNER JOIN silver.sejours           AS s ON d.stay_id        = s.stay_id
LEFT  JOIN gold_pilotage.dim_patient AS p ON s.patient_pseudo = p.patient_pseudo;


-- ══ FAIT RELEVÉ ═════════════════════════════════════════════════════════
-- Silver a garanti la PLAUSIBILITÉ de la mesure (plages du §3). Reste à
-- qualifier l'ALERTE, qui est une décision clinique paramétrée.
TRUNCATE TABLE gold_pilotage.fact_releve;

INSERT INTO gold_pilotage.fact_releve
SELECT
    m.stay_id,
    s.patient_pseudo,
    s.service_code,
    m.ts,
    toDate(m.ts),
    m.heart_rate, m.spo2, m.temp_c,
    (m.heart_rate < {fc_basse} OR m.heart_rate > {fc_haute}) AS alerte_fc,
    (m.spo2 < {spo2_basse})                                  AS alerte_spo2,
    (m.temp_c > {temp_haute})                                AS alerte_temp,
    (alerte_fc OR alerte_spo2 OR alerte_temp)                AS en_alerte,
    '{run_id}', now()
FROM silver.monitoring AS m
INNER JOIN silver.sejours AS s ON m.stay_id = s.stay_id;


-- ══ RECHERCHE — agrégats dérivés des faits ══════════════════════════════
-- Construits depuis fact_diagnostic, qui porte déjà patient, âge et sexe.
-- Le filtre des petits effectifs est appliqué À L'ÉCRITURE.
--
-- `est_principal = 1` — CE FILTRE DÉTERMINE LE CHIFFRE, il n'est pas anodin.
-- Chaque séjour porte un diagnostic principal et 0 à 2 associés : 6 729
-- principaux contre 5 864 associés. Ne compter que le principal, c'est
-- mesurer la prévalence du MOTIF D'HOSPITALISATION — les patients hospitalisés
-- POUR cette pathologie — et non celle de la pathologie dans la population,
-- comorbidités comprises, qui triplerait presque le compte.
--
-- Ce choix suit le grain du séjour, sur lequel tout le reste de l'entrepôt est
-- construit, et évite de compter comme « cohorte diabète » un patient
-- hospitalisé pour une fracture et diabétique par ailleurs. Il doit accompagner
-- l'indicateur partout où il est diffusé : un chiffre dont on ne dit pas ce
-- qu'il compte n'est pas exploitable.

TRUNCATE TABLE gold_recherche.coh_prevalence;

INSERT INTO gold_recherche.coh_prevalence
SELECT f.code_cim10, c.pathologie,
       uniqExact(f.patient_pseudo) AS nb_patients,
       count() AS nb_sejours,
       '{run_id}', now()
FROM gold_pilotage.fact_diagnostic AS f
INNER JOIN gold_pilotage.dim_cim10 AS c ON f.code_cim10 = c.code_cim10
WHERE f.est_principal = 1
GROUP BY f.code_cim10, c.pathologie
HAVING nb_patients >= 5;

TRUNCATE TABLE gold_recherche.coh_description;

INSERT INTO gold_recherche.coh_description
SELECT f.code_cim10, c.pathologie, f.tranche_age, f.sexe,
       uniqExact(f.patient_pseudo) AS nb_patients,
       '{run_id}', now()
FROM gold_pilotage.fact_diagnostic AS f
INNER JOIN gold_pilotage.dim_cim10 AS c ON f.code_cim10 = c.code_cim10
WHERE f.est_principal = 1 AND f.tranche_age != 'inconnu'
GROUP BY f.code_cim10, c.pathologie, f.tranche_age, f.sexe
HAVING nb_patients >= 5;


-- ══ PILOTAGE — INDICATEURS AGRÉGÉS ══════════════════════════════════════
-- Dérivés des FAITS, jamais de silver : un indicateur et le fait dont il
-- sort doivent donner le même chiffre par construction, sinon les deux
-- divergent silencieusement. `tests.verifier indicateurs` compare d'ailleurs
-- chaque table à son recalcul depuis les faits.

TRUNCATE TABLE gold_pilotage.kpi_dms_service;

INSERT INTO gold_pilotage.kpi_dms_service
SELECT f.service_code, s.service,
       toStartOfMonth(f.date_admission) AS mois,
       round(avg(f.duree_jours), 2),
       count(),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE f.est_en_cours = 0
GROUP BY f.service_code, s.service, mois;

TRUNCATE TABLE gold_pilotage.kpi_urgences_jour;

INSERT INTO gold_pilotage.kpi_urgences_jour
SELECT date_admission, countIf(est_urgence), count(), '{run_id}', now()
FROM gold_pilotage.fact_sejour
GROUP BY date_admission;

TRUNCATE TABLE gold_pilotage.kpi_readmission_service;

-- Le taux vaut 0 quand aucun séjour index n'existe pour le service : le
-- ratio serait indéfini, et laisser la ligne absente ferait disparaître le
-- service du tableau au lieu de dire « pas encore mesurable ».
INSERT INTO gold_pilotage.kpi_readmission_service
SELECT f.service_code, s.service,
       sum(f.est_sejour_index)      AS nb_index,
       sum(f.suivi_readmission_30j) AS nb_readmis,
       if(nb_index = 0, 0, round(100 * nb_readmis / nb_index, 2)),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
GROUP BY f.service_code, s.service;

TRUNCATE TABLE gold_pilotage.kpi_alertes_jour;

INSERT INTO gold_pilotage.kpi_alertes_jour
SELECT f.date_mesure, f.service_code, s.service,
       count()             AS nb_releves,
       countIf(f.en_alerte) AS nb_alertes,
       round(100 * nb_alertes / nb_releves, 2),
       '{run_id}', now()
FROM gold_pilotage.fact_releve AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
GROUP BY f.date_mesure, f.service_code, s.service;
