"""Profilage qualite de la source sejours + nature des depots."""

import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()
for name, path, reader in [
    ("patients", "patients/*/patients.csv", "read_csv"),
    ("sejours", "sejours/*/sejours.csv", "read_csv"),
]:
    con.execute(f"""
    CREATE VIEW {name} AS
    SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
    FROM {reader}('{SRC}/{path}', all_varchar=true, filename=true)
    """)
con.execute(
    f"CREATE VIEW services AS SELECT * FROM read_csv('{SRC}/referentiels/*/services.csv', all_varchar=true)"
)


def titre(t):
    print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")


def q(sql):
    print(con.sql(sql))


titre("2.0  NATURE DU DEPOT — snapshot cumulatif ou delta ?")
q("""
SELECT jour_premiere_apparition, count(*) AS nb_patients
FROM (SELECT patient_id, min(jour_depot) AS jour_premiere_apparition FROM patients GROUP BY patient_id)
GROUP BY ALL ORDER BY 1
""")
q("""
SELECT jour_depot,
       count(*) AS sejours_du_fichier,
       count(DISTINCT stay_id) AS stay_id_distincts
FROM sejours GROUP BY ALL ORDER BY 1
""")
q("""
SELECT nb_jours, count(*) AS nb_stay_id
FROM (SELECT stay_id, count(DISTINCT jour_depot) AS nb_jours FROM sejours GROUP BY stay_id)
GROUP BY ALL ORDER BY 1
""")

titre("2.1  Unicite de stay_id et valeurs manquantes")
q("""
SELECT count(*) AS lignes, count(DISTINCT stay_id) AS stay_distincts,
       count(*) FILTER (stay_id IS NULL OR trim(stay_id)='') AS stay_id_vide,
       count(*) FILTER (patient_id IS NULL OR trim(patient_id)='') AS patient_id_vide,
       count(*) FILTER (service_code IS NULL OR trim(service_code)='') AS service_vide,
       count(*) FILTER (admission_ts IS NULL OR trim(admission_ts)='') AS admission_vide,
       count(*) FILTER (discharge_ts IS NULL OR trim(discharge_ts)='') AS discharge_vide
FROM sejours
""")

titre("2.2  CONTROLE — coherence temporelle (discharge_ts < admission_ts)")
q("""
SELECT count(*) AS lignes_totales,
       count(*) FILTER (a IS NULL) AS admission_non_parsable,
       count(*) FILTER (d IS NULL AND discharge_ts IS NOT NULL AND trim(discharge_ts) <> '') AS discharge_non_parsable,
       count(*) FILTER (d IS NULL OR trim(coalesce(discharge_ts,'')) = '') AS sejour_en_cours_legitime,
       count(*) FILTER (d IS NOT NULL AND a IS NOT NULL AND d < a) AS INCOHERENCE_TEMPORELLE
FROM (SELECT discharge_ts, try_cast(admission_ts AS TIMESTAMP) a, try_cast(discharge_ts AS TIMESTAMP) d FROM sejours)
""")
q("""
SELECT jour_depot, count(*) FILTER (d < a) AS incoherences, count(*) FILTER (d IS NULL) AS en_cours
FROM (SELECT jour_depot, try_cast(admission_ts AS TIMESTAMP) a, try_cast(discharge_ts AS TIMESTAMP) d FROM sejours)
GROUP BY ALL ORDER BY 1
""")

titre("2.3  Domaines admission_mode / discharge_mode")
q(
    "SELECT coalesce(nullif(trim(admission_mode),''),'<vide>') AS admission_mode, count(*) n FROM sejours GROUP BY ALL ORDER BY n DESC"
)
q(
    "SELECT coalesce(nullif(trim(discharge_mode),''),'<vide>') AS discharge_mode, count(*) n FROM sejours GROUP BY ALL ORDER BY n DESC"
)

titre("2.4  INTEGRITE — service_code absent du referentiel")
q("""
SELECT s.service_code, count(*) AS n
FROM sejours s LEFT JOIN services r ON s.service_code = r.service_code
WHERE r.service_code IS NULL GROUP BY ALL ORDER BY n DESC
""")

titre("2.5  INTEGRITE — sejours orphelins (patient_id absent de patients)")
q("""
SELECT count(DISTINCT s.stay_id) AS sejours_orphelins,
       count(DISTINCT s.patient_id) AS patients_inconnus
FROM sejours s LEFT JOIN (SELECT DISTINCT patient_id FROM patients) p USING (patient_id)
WHERE p.patient_id IS NULL
""")

titre("2.6  Duree de sejour (base DMS) — distribution")
q("""
SELECT round(min(duree_j),2) AS min_j, round(quantile_cont(duree_j,0.5),2) AS mediane_j,
       round(avg(duree_j),2) AS moyenne_j, round(quantile_cont(duree_j,0.99),2) AS p99_j,
       round(max(duree_j),2) AS max_j,
       count(*) FILTER (duree_j < 0) AS durees_negatives,
       count(*) FILTER (duree_j > 365) AS plus_d_un_an
FROM (SELECT date_diff('minute', try_cast(admission_ts AS TIMESTAMP), try_cast(discharge_ts AS TIMESTAMP)) / 1440.0 AS duree_j
      FROM sejours WHERE try_cast(discharge_ts AS TIMESTAMP) IS NOT NULL)
""")

titre("2.7  Fenetre temporelle des admissions")
q("""
SELECT jour_depot, min(a) AS admission_min, max(a) AS admission_max
FROM (SELECT jour_depot, try_cast(admission_ts AS TIMESTAMP) a FROM sejours)
GROUP BY ALL ORDER BY 1
""")
