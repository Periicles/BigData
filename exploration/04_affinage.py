"""Affinage : nature des valeurs aberrantes monitoring, couverture, hors-fenetre."""
import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()
J = "regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1) AS jour_depot"
con.execute(f"CREATE VIEW sejours AS SELECT *, {J} FROM read_csv('{SRC}/sejours/*/sejours.csv', all_varchar=true, filename=true)")
con.execute(f"CREATE VIEW monitoring AS SELECT *, {J} FROM read_parquet('{SRC}/monitoring/*/monitoring.parquet', filename=true)")

def titre(t): print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")
def q(sql): print(con.sql(sql))

titre("4.4  Les aberrations FC et SpO2 sont-elles portees par les MEMES lignes ?")
q("""
SELECT count(*) FILTER (fc_ko AND spo2_ko) AS les_deux_ensemble,
       count(*) FILTER (fc_ko AND NOT spo2_ko) AS fc_seule,
       count(*) FILTER (spo2_ko AND NOT fc_ko) AS spo2_seule
FROM (SELECT heart_rate NOT BETWEEN 20 AND 250 AS fc_ko, spo2 NOT BETWEEN 50 AND 100 AS spo2_ko FROM monitoring)
""")

titre("4.5  Valeurs sentinelles exactes")
q("""
SELECT heart_rate, spo2, count(*) AS n
FROM monitoring WHERE heart_rate NOT BETWEEN 20 AND 250 OR spo2 NOT BETWEEN 50 AND 100
GROUP BY ALL ORDER BY n DESC LIMIT 10
""")

titre("4.6  Quels services sont monitores ? (le monitoring ne couvre pas tous les sejours)")
q("""
SELECT s.service_code,
       count(DISTINCT s.stay_id) AS sejours,
       count(DISTINCT m.stay_id) AS sejours_monitores,
       round(100.0 * count(DISTINCT m.stay_id) / count(DISTINCT s.stay_id), 1) AS pct
FROM sejours s LEFT JOIN (SELECT DISTINCT stay_id FROM monitoring) m USING (stay_id)
GROUP BY ALL ORDER BY pct DESC
""")

titre("4.7  Releves hors fenetre — avant admission ou apres sortie ?")
q("""
SELECT count(*) FILTER (m.ts < a) AS avant_admission,
       count(*) FILTER (d IS NOT NULL AND m.ts > d) AS apres_sortie,
       count(DISTINCT m.stay_id) FILTER (m.ts < a OR (d IS NOT NULL AND m.ts > d)) AS sejours_concernes
FROM monitoring m JOIN (SELECT stay_id, try_cast(admission_ts AS TIMESTAMP) a,
                               try_cast(discharge_ts AS TIMESTAMP) d FROM sejours) s USING (stay_id)
""")

titre("4.8  ALERTES — volumetrie apres exclusion des aberrations (KPI 'releves en alerte / jour')")
q("""
SELECT count(*) AS releves_valides,
       count(*) FILTER (heart_rate > 120 OR heart_rate < 40) AS alerte_fc,
       count(*) FILTER (spo2 < 92) AS alerte_spo2,
       count(*) FILTER (temp_c > 38.5) AS alerte_temp
FROM monitoring
WHERE heart_rate BETWEEN 20 AND 250 AND spo2 BETWEEN 50 AND 100 AND temp_c BETWEEN 30 AND 45
""")

titre("4.9  Recapitulatif des lignes a ecarter par la couche silver")
q("""
SELECT 'patients — redepots a dedupliquer' AS regle, 10200 AS lignes_concernees, '16200 lignes -> 6000 patients' AS effet
UNION ALL SELECT 'sejours — discharge_ts < admission_ts', 136, 'ecarter (duree negative)'
UNION ALL SELECT 'sejours — discharge_ts vide', 1190, 'CONSERVER : sejour en cours, legitime'
UNION ALL SELECT 'sejours — discharge_mode absent sur sejour clos', 1992, 'a arbitrer : non prevu par la fiche'
UNION ALL SELECT 'monitoring — FC et SpO2 sentinelles', 1369, 'ecarter le releve'
UNION ALL SELECT 'monitoring — releves hors fenetre du sejour', 520, 'a arbitrer : non prevu par la fiche'
ORDER BY lignes_concernees DESC
""")
