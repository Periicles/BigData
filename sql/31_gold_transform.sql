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

-- `ref_services` (les 8 services) commande, `ref_description_service` (7
-- d'entre eux) complète : un LEFT JOIN, jamais un INNER — celui-ci ferait
-- DISPARAÎTRE le service non décrit de la dimension, et avec lui 18 % des
-- actes de tout indicateur joint dessus.
--
-- On teste `d.service_code = ''` et non `IS NULL` : join_use_nulls vaut 0,
-- un LEFT JOIN sans correspondance remplit les colonnes String avec la
-- chaîne vide (même piège que dans 21_silver_transform.sql).
--
-- `capacite_lits` doit rester NULL pour un service non décrit : c'est un
-- dénominateur. Le CAST explicite est nécessaire — sans lui, ClickHouse
-- infère UInt16 sur la branche NULL et la ramènerait à 0.
TRUNCATE TABLE gold_pilotage.dim_service;
INSERT INTO gold_pilotage.dim_service
SELECT s.service_code,
       s.service_label,
       if(d.service_code = '', 'non renseigné', d.categorie),
       if(d.service_code = '', CAST(NULL AS Nullable(UInt16)), d.capacite_lits),
       if(d.service_code = '', 'non renseigné', d.pole),
       '{run_id}', now()
FROM bronze.ref_services AS s
LEFT JOIN bronze.ref_description_service AS d ON s.service_code = d.service_code;

TRUNCATE TABLE gold_pilotage.dim_cim10;
INSERT INTO gold_pilotage.dim_cim10
SELECT code_cim10, libelle, '{run_id}', now()
FROM bronze.ref_cim10;

TRUNCATE TABLE gold_pilotage.dim_ccam;
INSERT INTO gold_pilotage.dim_ccam
SELECT code_ccam, libelle, tarif_euros, '{run_id}', now()
FROM bronze.ref_ccam;


-- ══ FAIT SÉJOUR ═════════════════════════════════════════════════════════
-- Les drapeaux de réadmission sont calculés ici, une fois pour toutes — DEUX
-- définitions, l'AJUSTÉE et la BRUTE (cf. l'en-tête de leurs colonnes en
-- 30_gold.sql). `readmissions` calcule le même signal de fond — « ce séjour
-- clos est-il suivi d'une réadmission du même patient sous 30 j » — pour
-- TOUT séjour clos, décès compris ; c'est au moment d'assigner
-- `est_sejour_index` / `suivi_readmission_30j` (ajustés) que le décès est
-- exclu, sans toucher au calcul brut qui, lui, le garde.
--
-- Séjour index (AJUSTÉ) = séjour CLOS dont le patient n'est PAS décédé. Sans
-- cette exclusion, 223 paires compteraient un patient réadmis après sa mort
-- — une incohérence de saisie que la définition ajustée écarte, mais que la
-- BRUTE, elle, conserve : c'est la référence de l'intervenant.
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
    WHERE i.est_en_cours = 0
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
    (s.est_en_cours = 0 AND s.discharge_mode != 'deces')       AS est_sejour_index,
    if(est_sejour_index, coalesce(r.readmis, 0), 0),
    if(s.est_en_cours = 0, coalesce(r.readmis, 0), 0),
    '{run_id}', now()
FROM silver.sejours AS s
LEFT JOIN gold_pilotage.dim_patient AS p ON s.patient_pseudo = p.patient_pseudo
LEFT JOIN readmissions             AS r ON s.stay_id        = r.stay_id;


-- ══ FAIT DIAGNOSTIC ═════════════════════════════════════════════════════
-- `silver.diagnostics` porte déjà `patient_pseudo`, `service_code` et
-- `admission_ts` (enrichis contre bronze.sejours dès silver, cf. son
-- en-tête) : pas de jointure vers silver.sejours ici, INNER ou LEFT — un
-- diagnostic dont le séjour est temporellement incohérent reste dans le
-- fait, `sejour_coherent` simplement recopié à 0. Seul `dim_patient` reste
-- nécessaire, pour l'âge et le sexe.
--
-- Le sexe est lu dans dim_patient, pas re-lu dans silver.patients : c'est un
-- attribut de dimension, il n'a qu'une seule source de vérité.
TRUNCATE TABLE gold_pilotage.fact_diagnostic;

INSERT INTO gold_pilotage.fact_diagnostic
SELECT
    d.stay_id,
    d.patient_pseudo,
    d.code_cim10,
    d.service_code,
    toDate(d.admission_ts),
    d.type_diag,
    d.type_diag = 'principal',
    d.sejour_coherent,
    if(p.patient_pseudo = '' OR d.admission_ts IS NULL, NULL,
       toYear(d.admission_ts) - p.birth_year)         AS age_au_sejour,
    if(age_au_sejour IS NULL, 'inconnu',
       concat(toString(intDiv(age_au_sejour, 10) * 10), '-',
              toString(intDiv(age_au_sejour, 10) * 10 + 9))),
    if(p.patient_pseudo = '', 'inconnu', p.sexe),
    '{run_id}', now()
FROM silver.diagnostics AS d
LEFT JOIN gold_pilotage.dim_patient AS p ON d.patient_pseudo = p.patient_pseudo;


-- ══ FAIT RELEVÉ ═════════════════════════════════════════════════════════
-- Silver a garanti la PLAUSIBILITÉ de la mesure (plages du §3). Reste à
-- qualifier l'ALERTE, qui est une décision clinique paramétrée.
--
-- `silver.monitoring` porte déjà patient, service et `sejour_coherent` :
-- aucune jointure n'est nécessaire ici, pas même vers dim_patient — ce fait
-- ne calcule ni âge ni sexe.
TRUNCATE TABLE gold_pilotage.fact_releve;

INSERT INTO gold_pilotage.fact_releve
SELECT
    m.stay_id,
    m.patient_pseudo,
    m.service_code,
    m.ts,
    toDate(m.ts),
    m.heart_rate, m.spo2, m.temp_c,
    (m.heart_rate < {fc_basse} OR m.heart_rate > {fc_haute}) AS alerte_fc,
    (m.spo2 < {spo2_basse})                                  AS alerte_spo2,
    (m.temp_c > {temp_haute})                                AS alerte_temp,
    (alerte_fc OR alerte_spo2 OR alerte_temp)                AS en_alerte,
    m.sejour_coherent,
    '{run_id}', now()
FROM silver.monitoring AS m;


-- ══ FAIT ACTE ═══════════════════════════════════════════════════════════
-- `silver.actes` porte déjà `service_code` et `admission_ts`, recopiés du
-- séjour porteur : aucune jointure vers `silver.sejours` ni vers
-- `fact_sejour` ici. C'est exactement la consigne du sujet d'évolution —
-- « récupérez-le sans relier deux tables de faits entre elles ».
--
-- Aucune jointure vers `dim_patient` non plus : ce fait ne porte pas de
-- pseudonyme (cf. 30_gold.sql pour la justification).
TRUNCATE TABLE gold_pilotage.fact_acte;

INSERT INTO gold_pilotage.fact_acte
SELECT
    stay_id,
    code_ccam,
    service_code,
    toDate(acte_ts),
    toDate(admission_ts),
    sejour_coherent,
    '{run_id}', now()
FROM silver.actes;


-- ══ RECHERCHE — agrégats dérivés des faits ══════════════════════════════
-- Construits depuis fact_diagnostic, qui porte déjà patient, âge et sexe.
-- Le filtre des petits effectifs est appliqué À L'ÉCRITURE.
--
-- `nb_patients` — LE CHIFFRE DE RÉFÉRENCE, et celui qui porte le filtre k >=
-- 5 — compte sur TOUS les types de diagnostic (principal ET associé), séjours
-- incohérents compris : c'est la définition retenue par l'intervenant
-- (valeurs de référence fournies), qui fait foi. Elle mesure la prévalence de la pathologie
-- dans la population suivie, comorbidités comprises.
--
-- `nb_patients_principal` reste exposée à côté : c'est l'ANCIENNE définition
-- de ce projet, restreinte au diagnostic PRINCIPAL — le motif
-- d'hospitalisation, les patients hospitalisés POUR cette pathologie. Elle ne
-- détermine plus la diffusion (k >= 5 s'applique sur `nb_patients`), mais
-- reste utile à qui veut la prévalence au sens strict. `nb_sejours` compte de
-- même sur tous les types, en cohérence avec `nb_patients`.

TRUNCATE TABLE gold_recherche.coh_prevalence;

INSERT INTO gold_recherche.coh_prevalence
SELECT f.code_cim10, c.pathologie,
       uniqExact(f.patient_pseudo)                        AS nb_patients,
       uniqExactIf(f.patient_pseudo, f.est_principal = 1)  AS nb_patients_principal,
       count()                                             AS nb_sejours,
       '{run_id}', now()
FROM gold_pilotage.fact_diagnostic AS f
INNER JOIN gold_pilotage.dim_cim10 AS c ON f.code_cim10 = c.code_cim10
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
       round(avg(f.duree_jours) * 24, 1),
       round(median(f.duree_jours), 2),
       round(quantile(0.9)(f.duree_jours), 2),
       round(max(f.duree_jours), 2),
       count(),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE f.est_en_cours = 0
GROUP BY f.service_code, s.service, mois;

TRUNCATE TABLE gold_pilotage.kpi_urgences_jour;

-- `duree_moy_heures` ne porte que sur les séjours du service URGENCES CLOS
-- (avgIf) : un séjour encore en cours n'a pas de durée, l'inclure biaiserait
-- la moyenne comme pour la DMS. `avgIf` sur un jour sans aucun séjour clos du
-- service renverrait NaN (0/0) — coalesce le ramène à 0, cas qui ne se
-- présente pas sur ce dépôt mais que la requête doit couvrir.
INSERT INTO gold_pilotage.kpi_urgences_jour
SELECT
    date_admission AS jour,
    countIf(service_code = 'URGENCES')                       AS nb_passages_urgences,
    countIf(service_code = 'URGENCES' AND est_en_cours = 1)   AS nb_encore_presents,
    round(coalesce(
        avgIf(duree_jours * 24, service_code = 'URGENCES' AND est_en_cours = 0), 0
    ), 1)                                                     AS duree_moy_heures,
    countIf(est_urgence = 1)                                  AS nb_admissions_en_urgence,
    count()                                                   AS nb_sejours,
    '{run_id}', now()
FROM gold_pilotage.fact_sejour
GROUP BY jour;

TRUNCATE TABLE gold_pilotage.kpi_readmission_service;

-- Les deux taux valent 0 quand leur dénominateur est nul pour le service : le
-- ratio serait indéfini, et laisser la ligne absente ferait disparaître le
-- service du tableau au lieu de dire « pas encore mesurable ».
INSERT INTO gold_pilotage.kpi_readmission_service
SELECT f.service_code, s.service,
       count()                            AS nb_sejours,
       sum(f.readmission_30j_brute)       AS nb_readmis_brut,
       if(nb_sejours = 0, 0, round(100 * nb_readmis_brut / nb_sejours, 2)),
       sum(f.est_sejour_index)            AS nb_index,
       sum(f.suivi_readmission_30j)       AS nb_readmis,
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


-- ── Les quatre vues d'activité du cinquième point du § 4 ────────────────

TRUNCATE TABLE gold_pilotage.kpi_occupation_jour;

-- Un séjour est déroulé sur son intervalle : `range()` produit un jour par
-- journée d'hospitalisation, `arrayJoin` les transforme en lignes. Les
-- séjours en cours courent jusqu'au dernier jour de dépôt — au-delà, on
-- n'observe plus rien, et prolonger la courbe donnerait une décroissance
-- purement documentaire.
INSERT INTO gold_pilotage.kpi_occupation_jour
SELECT j, f.service_code, s.service,
       count()                                  AS nb_presents,
       countIf(toDate(f.admission_ts) = j)      AS nb_admissions,
       countIf(f.date_sortie = j)               AS nb_sorties,
       '{run_id}', now()
FROM (
    SELECT service_code, admission_ts, date_sortie,
           toDate(arrayJoin(range(
               toUInt32(toDate(admission_ts)),
               toUInt32(coalesce(date_sortie, (SELECT max(date_admission)
                                               FROM gold_pilotage.fact_sejour))) + 1
           ))) AS j
    FROM gold_pilotage.fact_sejour
) AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE j <= (SELECT max(date_admission) FROM gold_pilotage.fact_sejour)
GROUP BY j, f.service_code, s.service;

TRUNCATE TABLE gold_pilotage.kpi_mortalite_service;

-- Dénominateur : les séjours CLOS. Un séjour en cours n'a pas d'issue
-- connue ; le compter reviendrait à le supposer vivant, ce qui minore le
-- taux d'autant.
INSERT INTO gold_pilotage.kpi_mortalite_service
SELECT f.service_code, s.service,
       count()                                   AS nb_clos,
       countIf(f.discharge_mode = 'deces')       AS nb_deces,
       if(nb_clos = 0, 0, round(100 * nb_deces / nb_clos, 2)),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE f.est_en_cours = 0
GROUP BY f.service_code, s.service;

TRUNCATE TABLE gold_pilotage.kpi_casemix_service;

-- La part est calculée SUR LE SERVICE, pas sur l'hôpital : la question est
-- « de quoi ce service soigne-t-il ses patients », pas « quel poids ce
-- service a-t-il dans l'activité totale ». Les parts d'un même service
-- somment donc à 100.
INSERT INTO gold_pilotage.kpi_casemix_service
SELECT d.service_code, s.service, d.code_cim10, c.pathologie,
       count() AS nb,
       round(100 * nb / sum(nb) OVER (PARTITION BY d.service_code), 2),
       '{run_id}', now()
FROM gold_pilotage.fact_diagnostic AS d
INNER JOIN gold_pilotage.dim_service AS s ON d.service_code = s.service_code
INNER JOIN gold_pilotage.dim_cim10   AS c ON d.code_cim10   = c.code_cim10
WHERE d.est_principal = 1
GROUP BY d.service_code, s.service, d.code_cim10, c.pathologie;

TRUNCATE TABLE gold_pilotage.kpi_origine_service;

-- La région est un attribut de DIMENSION : elle se lit dans `dim_patient`,
-- jamais re-dérivée de silver — même règle que `age_au_sejour` en tête de
-- fichier. La part est calculée SUR LE SERVICE, comme le case-mix : les
-- parts d'un même service somment donc à 100.
INSERT INTO gold_pilotage.kpi_origine_service
SELECT f.service_code, s.service, p.region AS region_code,
       count()                     AS nb_sejours,
       uniqExact(f.patient_pseudo) AS nb_patients,
       round(100 * nb_sejours / sum(nb_sejours) OVER (PARTITION BY f.service_code), 2),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
INNER JOIN gold_pilotage.dim_patient AS p ON f.patient_pseudo = p.patient_pseudo
GROUP BY f.service_code, s.service, p.region;


-- ── Les cinq indicateurs du sujet d'évolution ───────────────────────────

-- ① Activité et DMS par catégorie de service.
TRUNCATE TABLE gold_pilotage.kpi_activite_categorie;

INSERT INTO gold_pilotage.kpi_activite_categorie
SELECT s.categorie,
       count(),
       countIf(f.est_en_cours = 0),
       round(avgIf(f.duree_jours, f.est_en_cours = 0), 2),
       round(avgIf(f.duree_jours, f.est_en_cours = 0) * 24, 1),
       round(medianIf(f.duree_jours, f.est_en_cours = 0), 2),
       round(quantileIf(0.9)(f.duree_jours, f.est_en_cours = 0), 2),
       '{run_id}', now()
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
GROUP BY s.categorie;

-- ② Nombre d'actes par service, et actes par séjour.
--
-- DEUX AGRÉGATS INDÉPENDANTS, joints sur `service_code` — jamais les deux
-- faits ligne à ligne. `fact_acte` est réduit à une ligne par service,
-- `fact_sejour` aussi, et seules ces deux réductions se rencontrent : aucune
-- ligne ne peut être multipliée. C'est la lecture littérale de la consigne
-- du sujet.
--
-- Le FULL JOIN, et non un INNER : un service sans aucun acte doit apparaître
-- avec nb_actes = 0, et un service dont tous les séjours auraient disparu
-- doit rester visible plutôt que d'escamoter ses actes.
TRUNCATE TABLE gold_pilotage.kpi_actes_service;

INSERT INTO gold_pilotage.kpi_actes_service
SELECT d.service_code,
       d.service,
       d.categorie,
       a.nb_actes,
       sj.nb_sejours,
       a.nb_sejours_avec_acte,
       if(sj.nb_sejours = 0, 0, round(a.nb_actes / sj.nb_sejours, 2)),
       if(a.nb_sejours_avec_acte = 0, 0, round(a.nb_actes / a.nb_sejours_avec_acte, 2)),
       '{run_id}', now()
FROM gold_pilotage.dim_service AS d
LEFT JOIN (
    SELECT service_code, count() AS nb_actes,
           uniqExact(stay_id) AS nb_sejours_avec_acte
    FROM gold_pilotage.fact_acte GROUP BY service_code
) AS a ON d.service_code = a.service_code
LEFT JOIN (
    SELECT service_code, count() AS nb_sejours
    FROM gold_pilotage.fact_sejour GROUP BY service_code
) AS sj ON d.service_code = sj.service_code;

-- ③ Répartition des actes par type d'acte.
--
-- La part est calculée sur le total de `fact_acte`, lu une fois en
-- sous-requête scalaire : sans elle il faudrait resommer la table pour
-- interpréter une ligne.
TRUNCATE TABLE gold_pilotage.kpi_actes_type;

INSERT INTO gold_pilotage.kpi_actes_type
SELECT f.code_ccam,
       if(c.code_ccam = '', 'inconnu', c.acte),
       count(),
       round(100 * count() / (SELECT count() FROM gold_pilotage.fact_acte), 2),
       uniqExact(f.stay_id),
       c.tarif_euros,
       '{run_id}', now()
FROM gold_pilotage.fact_acte AS f
LEFT JOIN gold_pilotage.dim_ccam AS c ON f.code_ccam = c.code_ccam
GROUP BY f.code_ccam, c.code_ccam, c.acte, c.tarif_euros;

-- ④ Densité d'actes par lit.
--
-- L'INNER JOIN et le `capacite_lits IS NOT NULL` disent la même chose deux
-- fois, volontairement : le premier est la mécanique, le second l'intention.
-- Un service sans capacité connue N'A PAS DE LIGNE — un ratio indéfini est
-- absent, jamais nul. La lacune se lit par différence avec
-- `kpi_actes_service`, et `tests.verifier` mesure exactement cet écart.
TRUNCATE TABLE gold_pilotage.kpi_densite_actes_lit;

INSERT INTO gold_pilotage.kpi_densite_actes_lit
SELECT d.service_code,
       d.service,
       d.categorie,
       d.capacite_lits,
       count(),
       round(count() / d.capacite_lits, 2),
       '{run_id}', now()
FROM gold_pilotage.fact_acte AS f
INNER JOIN gold_pilotage.dim_service AS d ON f.service_code = d.service_code
WHERE d.capacite_lits IS NOT NULL
GROUP BY d.service_code, d.service, d.categorie, d.capacite_lits;

-- ⑤ Montant facturé par service (T2A).
--
-- Le tarif vient de la DIMENSION. Un acte dont le code est absent de la
-- nomenclature n'a pas de tarif : `sumIf` l'exclut du montant, et
-- `nb_actes_sans_tarif` le rend visible — sans quoi un total sous-évalué
-- ressemblerait à une activité plus faible.
TRUNCATE TABLE gold_pilotage.kpi_facturation_service;

INSERT INTO gold_pilotage.kpi_facturation_service
SELECT f.service_code,
       d.service,
       d.categorie,
       count(),
       countIf(c.tarif_euros IS NULL),
       sum(coalesce(c.tarif_euros, toDecimal64(0, 2))),
       if(countIf(c.tarif_euros IS NOT NULL) = 0, 0,
          round(sum(coalesce(c.tarif_euros, toDecimal64(0, 2)))
                / countIf(c.tarif_euros IS NOT NULL), 2)),
       '{run_id}', now()
FROM gold_pilotage.fact_acte AS f
INNER JOIN gold_pilotage.dim_service AS d ON f.service_code = d.service_code
LEFT JOIN gold_pilotage.dim_ccam AS c ON f.code_ccam = c.code_ccam
GROUP BY f.service_code, d.service, d.categorie;
