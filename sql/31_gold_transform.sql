-- ═══════════════════════════════════════════════════════════════════════
-- Construction du modèle en étoile depuis silver. Recalcul intégral.
--
-- Une expression réutilisée partout : la tranche d'âge de 10 ans.
--   concat(toString(intDiv(age, 10) * 10), '-', toString(intDiv(age, 10) * 10 + 9))
-- Elle est calculée dans les faits, et NON dans dim_patient : l'âge est un
-- attribut de l'ÉVÉNEMENT (l'âge qu'avait le patient lors de ce séjour), pas
-- une propriété stable de la personne.
-- ═══════════════════════════════════════════════════════════════════════

-- ══ DIMENSIONS ══════════════════════════════════════════════════════════

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
    s.age_au_sejour,
    if(s.age_au_sejour IS NULL, 'inconnu',
       concat(toString(intDiv(s.age_au_sejour, 10) * 10), '-',
              toString(intDiv(s.age_au_sejour, 10) * 10 + 9))),
    s.duree_jours,
    s.est_en_cours,
    s.admission_mode = 'urgence',
    s.est_en_cours = 0 AND s.discharge_mode != 'deces',
    coalesce(r.readmis, 0),
    '{run_id}', now()
FROM silver.sejours AS s
LEFT JOIN readmissions AS r ON s.stay_id = r.stay_id;


-- ══ FAIT DIAGNOSTIC ═════════════════════════════════════════════════════
-- Patient, âge et sexe sont dénormalisés depuis le séjour : les questions de
-- recherche portent sur des cohortes de patients par pathologie, cette copie
-- leur évite deux jointures.
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
    s.age_au_sejour,
    if(s.age_au_sejour IS NULL, 'inconnu',
       concat(toString(intDiv(s.age_au_sejour, 10) * 10), '-',
              toString(intDiv(s.age_au_sejour, 10) * 10 + 9))),
    p.sex,
    '{run_id}', now()
FROM silver.diagnostics AS d
INNER JOIN silver.sejours  AS s ON d.stay_id = s.stay_id
INNER JOIN silver.patients AS p ON s.patient_pseudo = p.patient_pseudo;


-- ══ FAIT RELEVÉ ═════════════════════════════════════════════════════════
TRUNCATE TABLE gold_pilotage.fact_releve;

INSERT INTO gold_pilotage.fact_releve
SELECT
    m.stay_id,
    s.patient_pseudo,
    s.service_code,
    m.ts,
    toDate(m.ts),
    m.heart_rate, m.spo2, m.temp_c,
    m.alerte_fc, m.alerte_spo2, m.alerte_temp, m.en_alerte,
    '{run_id}', now()
FROM silver.monitoring AS m
INNER JOIN silver.sejours AS s ON m.stay_id = s.stay_id;


-- ══ RECHERCHE — agrégats dérivés des faits ══════════════════════════════
-- Construits depuis fact_diagnostic, qui porte déjà patient, âge et sexe.
-- Le filtre des petits effectifs est appliqué À L'ÉCRITURE.

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
