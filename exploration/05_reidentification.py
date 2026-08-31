"""Risque de re-identification sur les quasi-identifiants conserves apres pseudonymisation."""

import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()
J = "regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1) AS jour_depot"
con.execute(
    f"CREATE VIEW patients AS SELECT *, {J} FROM read_csv('{SRC}/patients/*/patients.csv', all_varchar=true, filename=true)"
)
con.execute(
    f"CREATE VIEW sejours AS SELECT *, {J} FROM read_csv('{SRC}/sejours/*/sejours.csv', all_varchar=true, filename=true)"
)
con.execute(
    f"CREATE VIEW diag_raw AS SELECT *, {J} FROM read_json('{SRC}/diagnostics/*/diagnostics.json', filename=true)"
)
con.execute(
    "CREATE VIEW diag AS SELECT stay_id, d.code_cim10, d.type FROM diag_raw, unnest(diagnostics) AS t(d)"
)
# population dedupliquee, telle qu'elle entrerait dans l'entrepot apres pseudonymisation
con.execute("""
CREATE VIEW pop AS
SELECT DISTINCT ON (patient_id) patient_id,
       year(try_cast(birth_date AS DATE)) AS annee_naissance, sex, region_code
FROM patients ORDER BY patient_id, jour_depot DESC
""")


def titre(t):
    print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")


def q(sql):
    print(con.sql(sql))


titre(
    "5.1  k-anonymat sur (annee_naissance, sexe, region) — quasi-identifiants conserves"
)
q("""
SELECT CASE WHEN k = 1 THEN 'k = 1  (UNIQUE — re-identifiable)'
            WHEN k < 5 THEN 'k = 2..4  (sous le seuil de 5)'
            ELSE 'k >= 5  (conforme)' END AS classe,
       count(*) AS nb_groupes, sum(k) AS nb_patients,
       round(100.0 * sum(k) / (SELECT count(*) FROM pop), 1) AS pct_population
FROM (SELECT annee_naissance, sex, region_code, count(*) AS k FROM pop GROUP BY ALL)
GROUP BY ALL ORDER BY nb_patients DESC
""")

titre("5.2  Effet de la generalisation en tranches d'age de 10 ans")
q("""
SELECT CASE WHEN k = 1 THEN 'k = 1' WHEN k < 5 THEN 'k = 2..4' ELSE 'k >= 5' END AS classe,
       count(*) AS nb_groupes, sum(k) AS nb_patients,
       round(100.0 * sum(k) / (SELECT count(*) FROM pop), 1) AS pct_population
FROM (SELECT (2026 - annee_naissance) // 10 AS tranche, sex, region_code, count(*) AS k FROM pop GROUP BY ALL)
GROUP BY ALL ORDER BY nb_patients DESC
""")

titre("5.3  Cohortes recherche sous le seuil de 5 patients (prevalence par pathologie)")
q("""
SELECT count(*) AS cohortes_sous_seuil FROM (
  SELECT d.code_cim10, p.sex, (2026 - p.annee_naissance) // 10 AS tranche, count(DISTINCT s.patient_id) AS n
  FROM diag d JOIN sejours s USING (stay_id) JOIN pop p ON s.patient_id = p.patient_id
  WHERE d.type = 'principal' GROUP BY ALL HAVING n < 5)
""")
q("""
SELECT d.code_cim10, count(DISTINCT s.patient_id) AS patients
FROM diag d JOIN sejours s USING (stay_id) WHERE d.type = 'principal'
GROUP BY ALL ORDER BY patients ASC LIMIT 5
""")
