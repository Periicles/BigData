"""Anomalie discharge_mode, faisabilite readmission, puis diagnostics et monitoring."""
import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()
J = "regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1) AS jour_depot"
con.execute(f"CREATE VIEW patients AS SELECT *, {J} FROM read_csv('{SRC}/patients/*/patients.csv', all_varchar=true, filename=true)")
con.execute(f"CREATE VIEW sejours  AS SELECT *, {J} FROM read_csv('{SRC}/sejours/*/sejours.csv',  all_varchar=true, filename=true)")
con.execute(f"CREATE VIEW monitoring AS SELECT *, {J} FROM read_parquet('{SRC}/monitoring/*/monitoring.parquet', filename=true)")
con.execute(f"CREATE VIEW diag_raw AS SELECT *, {J} FROM read_json('{SRC}/diagnostics/*/diagnostics.json', filename=true)")
con.execute(f"CREATE VIEW cim10 AS SELECT * FROM read_csv('{SRC}/referentiels/*/cim10.csv', all_varchar=true)")
con.execute("""
CREATE VIEW diag AS
SELECT stay_id, jour_depot, d.code_cim10, d.type
FROM diag_raw, unnest(diagnostics) AS t(d)
""")

def titre(t): print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")
def q(sql): print(con.sql(sql))

titre("2.8  ANOMALIE NON LISTEE — discharge_mode vide alors que le sejour est clos")
q("""
SELECT CASE WHEN trim(coalesce(discharge_ts,'')) = '' THEN 'sejour en cours' ELSE 'sejour clos' END AS etat,
       CASE WHEN trim(coalesce(discharge_mode,'')) = '' THEN 'mode ABSENT' ELSE 'mode present' END AS mode,
       count(*) AS n
FROM sejours GROUP BY ALL ORDER BY 1, 2
""")

titre("2.9  FAISABILITE — taux de readmission a 30 jours")
q("""
SELECT nb_sejours, count(*) AS nb_patients
FROM (SELECT patient_id, count(*) AS nb_sejours FROM sejours GROUP BY patient_id)
GROUP BY ALL ORDER BY 1
""")
q("""
WITH s AS (
  SELECT patient_id, stay_id,
         try_cast(admission_ts AS TIMESTAMP) a, try_cast(discharge_ts AS TIMESTAMP) d, discharge_mode
  FROM sejours
), paires AS (
  SELECT s1.patient_id, s1.stay_id AS sejour_index, s2.stay_id AS sejour_readmission,
         date_diff('day', s1.d, s2.a) AS delai_j, s1.discharge_mode
  FROM s s1 JOIN s s2 ON s1.patient_id = s2.patient_id AND s2.a > s1.d
  WHERE s1.d IS NOT NULL
)
SELECT count(*) AS paires_readmission_30j,
       count(*) FILTER (discharge_mode = 'deces') AS dont_apres_un_deces,
       min(delai_j) AS delai_min_j, max(delai_j) AS delai_max_j
FROM paires WHERE delai_j <= 30
""")

titre("3.1  DIAGNOSTICS — structure et volumetrie")
q("""
SELECT jour_depot, count(DISTINCT stay_id) AS sejours, count(*) AS codes,
       round(count(*)::DOUBLE / count(DISTINCT stay_id), 2) AS codes_par_sejour
FROM diag GROUP BY ALL ORDER BY 1
""")
q("""
SELECT nb_codes, count(*) AS nb_sejours
FROM (SELECT stay_id, count(*) AS nb_codes FROM diag GROUP BY stay_id)
GROUP BY ALL ORDER BY 1
""")

titre("3.2  DIAGNOSTICS — domaine de 'type' et regle un principal par sejour")
q("SELECT coalesce(type,'<NULL>') AS type, count(*) n FROM diag GROUP BY ALL ORDER BY n DESC")
q("""
SELECT nb_principaux, count(*) AS nb_sejours
FROM (SELECT stay_id, count(*) FILTER (type = 'principal') AS nb_principaux FROM diag GROUP BY stay_id)
GROUP BY ALL ORDER BY 1
""")

titre("3.3  DIAGNOSTICS — integrite referentielle")
q("""
SELECT count(*) AS codes_hors_referentiel, count(DISTINCT d.code_cim10) AS codes_distincts_inconnus
FROM diag d LEFT JOIN cim10 r ON d.code_cim10 = r.code_cim10 WHERE r.code_cim10 IS NULL
""")
q("""
SELECT count(DISTINCT d.stay_id) AS diagnostics_orphelins
FROM diag d LEFT JOIN (SELECT DISTINCT stay_id FROM sejours) s USING (stay_id) WHERE s.stay_id IS NULL
""")
q("""
SELECT count(DISTINCT s.stay_id) AS sejours_sans_aucun_diagnostic
FROM sejours s LEFT JOIN (SELECT DISTINCT stay_id FROM diag) d USING (stay_id) WHERE d.stay_id IS NULL
""")

titre("4.1  MONITORING — volumetrie et couverture")
q("""
SELECT jour_depot, count(*) AS releves, count(DISTINCT stay_id) AS sejours_suivis,
       min(ts) AS ts_min, max(ts) AS ts_max
FROM monitoring GROUP BY ALL ORDER BY 1
""")
q("""
SELECT count(DISTINCT m.stay_id) AS monitoring_orphelin
FROM monitoring m LEFT JOIN (SELECT DISTINCT stay_id FROM sejours) s USING (stay_id) WHERE s.stay_id IS NULL
""")

titre("4.2  MONITORING — valeurs manquantes et hors plage physiologique")
q("""
SELECT count(*) AS releves,
       count(*) FILTER (heart_rate IS NULL) AS fc_null,
       count(*) FILTER (spo2 IS NULL) AS spo2_null,
       count(*) FILTER (temp_c IS NULL) AS temp_null,
       count(*) FILTER (heart_rate NOT BETWEEN 20 AND 250) AS fc_hors_plage,
       count(*) FILTER (spo2 NOT BETWEEN 50 AND 100) AS spo2_hors_plage,
       count(*) FILTER (temp_c NOT BETWEEN 30 AND 45) AS temp_hors_plage
FROM monitoring
""")
q("""
SELECT 'heart_rate' AS mesure, min(heart_rate) AS mini, max(heart_rate) AS maxi FROM monitoring
UNION ALL SELECT 'spo2', min(spo2), max(spo2) FROM monitoring
UNION ALL SELECT 'temp_c', min(temp_c), max(temp_c) FROM monitoring
""")

titre("4.3  MONITORING — releves hors fenetre du sejour")
q("""
SELECT count(*) AS releves_hors_fenetre_sejour
FROM monitoring m JOIN sejours s USING (stay_id)
WHERE m.ts < try_cast(s.admission_ts AS TIMESTAMP)
   OR (try_cast(s.discharge_ts AS TIMESTAMP) IS NOT NULL AND m.ts > try_cast(s.discharge_ts AS TIMESTAMP))
""")
