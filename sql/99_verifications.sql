-- ═══════════════════════════════════════════════════════════════════════
-- Requêtes de vérification — à exécuter UNE PAR UNE dans http://localhost:8123/play
-- Chacune prouve une propriété que le jury peut te demander de démontrer.
-- ═══════════════════════════════════════════════════════════════════════


-- ① QU'EST-CE QU'IL Y A DANS L'ENTREPÔT ?
-- Vue d'ensemble : toutes les tables et leur volume.
SELECT database, name AS table, total_rows AS lignes,
       formatReadableSize(total_bytes) AS taille
FROM system.tables
WHERE database IN ('bronze', 'silver', 'quarantaine', 'gold_pilotage', 'gold_recherche', 'ops')
ORDER BY database, name;


-- ② AUCUNE DONNÉE IDENTIFIANTE N'EST ENTRÉE
-- La question que le jury posera en premier. Regarde les colonnes :
-- ni nom, ni prénom, ni NIR, ni date de naissance complète.
DESCRIBE TABLE bronze.patients;

-- Et le contenu : un pseudonyme, une année.
SELECT * FROM bronze.patients LIMIT 5;


-- ③ LE PARTITIONNEMENT PAR JOUR DE DÉPÔT
-- C'est ce qui rend l'ingestion incrémentale et rejouable.
SELECT partition, name AS partie, rows AS lignes,
       formatReadableSize(bytes_on_disk) AS taille
FROM system.parts
WHERE database = 'bronze' AND table = 'sejours' AND active
ORDER BY partition;


-- ④ LA TRAÇABILITÉ : d'où vient chaque ligne, quand, par quel run
SELECT _jour_depot, _run_id, min(_ingested_at) AS charge_a, count() AS lignes
FROM bronze.sejours
GROUP BY _jour_depot, _run_id
ORDER BY _jour_depot;


-- ⑤ LES ANOMALIES SONT BIEN ENTRÉES EN BRONZE
-- Volontaire : on ne peut compter que ce qu'on a laissé entrer.
-- Silver les écartera, en les traçant.
SELECT
    countIf(discharge_ts IS NULL)                      AS sejours_en_cours,
    countIf(discharge_ts < admission_ts)               AS incoherence_temporelle,
    countIf(discharge_mode = '' AND discharge_ts IS NOT NULL) AS mode_sortie_absent
FROM bronze.sejours;

-- Les valeurs aberrantes du monitoring : ce sont des BUTÉES de capteur,
-- pas du bruit. FC et SpO2 sont toujours aberrantes ENSEMBLE.
SELECT heart_rate, spo2, count() AS n
FROM bronze.monitoring
WHERE heart_rate NOT BETWEEN 20 AND 250 OR spo2 NOT BETWEEN 50 AND 100
GROUP BY heart_rate, spo2
ORDER BY n DESC;


-- ⑥ LE SNAPSHOT CUMULATIF DE `patients`
-- 18 000 lignes pour 6 000 patients : chaque fichier journalier contient
-- TOUTE la population connue. C'est la découverte qui conditionne silver.
SELECT count() AS lignes, uniqExact(patient_pseudo) AS patients_distincts
FROM bronze.patients;

-- Détail : combien de patients apparaissent sur 1, 2 ou 3 jours de dépôt
SELECT nb_jours, count() AS nb_patients
FROM (SELECT patient_pseudo, uniqExact(_jour_depot) AS nb_jours
      FROM bronze.patients GROUP BY patient_pseudo)
GROUP BY nb_jours ORDER BY nb_jours;


-- ⑦ LE JSON IMBRIQUÉ A ÉTÉ APLATI PAR LE MOTEUR
-- 6 797 séjours -> 12 720 codes, 1 à 3 par séjour, 1 seul principal.
SELECT type_diag, count() AS n FROM bronze.diagnostics GROUP BY type_diag;

SELECT nb_codes, count() AS nb_sejours
FROM (SELECT stay_id, count() AS nb_codes FROM bronze.diagnostics GROUP BY stay_id)
GROUP BY nb_codes ORDER BY nb_codes;


-- ⑧ LE MODÈLE EN ÉTOILE
-- Trois faits à trois grains différents, trois dimensions conformes.
--
-- Le fait se construit SUR la dimension.
SELECT name AS table, total_rows AS lignes
FROM system.tables WHERE database = 'gold_pilotage' ORDER BY name;

-- Toute clé étrangère d'un fait doit désigner un membre existant de sa
-- dimension. Cette requête doit rendre 0 partout — sinon des lignes
-- disparaîtraient silencieusement de tout graphe joint à la dimension, sans
-- qu'aucune erreur ne soit levée.
SELECT 'fact_sejour.patient_pseudo' AS cle, count() AS orphelins
FROM gold_pilotage.fact_sejour
WHERE patient_pseudo NOT IN (SELECT patient_pseudo FROM gold_pilotage.dim_patient)
UNION ALL
SELECT 'fact_diagnostic.code_cim10', count()
FROM gold_pilotage.fact_diagnostic
WHERE code_cim10 NOT IN (SELECT code_cim10 FROM gold_pilotage.dim_cim10)
UNION ALL
SELECT 'fact_releve.service_code', count()
FROM gold_pilotage.fact_releve
WHERE service_code NOT IN (SELECT service_code FROM gold_pilotage.dim_service);

-- Le croisement que permet l'étoile, et qu'un entrepôt de KPI pré-agrégés
-- n'aurait pas offert sans l'avoir anticipé :
-- durée de séjour par service ET tranche d'âge.
SELECT s.service, f.tranche_age,
       round(avg(f.duree_jours), 2) AS dms_jours, count() AS sejours
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE f.est_en_cours = 0 AND f.tranche_age != 'inconnu'
GROUP BY s.service, f.tranche_age
ORDER BY s.service, f.tranche_age;

-- Contrôle de grain : chaque fait est-il bien au grain annoncé ?
SELECT 'fact_sejour'     AS fait, count() AS lignes, uniqExact(stay_id) AS cles
FROM gold_pilotage.fact_sejour
UNION ALL
SELECT 'fact_diagnostic', count(), uniqExact(concat(stay_id, code_cim10, type_diag))
FROM gold_pilotage.fact_diagnostic
UNION ALL
SELECT 'fact_releve', count(), uniqExact(concat(stay_id, toString(ts)))
FROM gold_pilotage.fact_releve;
