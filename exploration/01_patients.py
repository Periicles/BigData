"""Profilage qualite de la source patients."""
import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()
con.execute(f"""
CREATE VIEW patients AS
SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
FROM read_csv('{SRC}/patients/*/patients.csv', all_varchar=true, filename=true)
""")

def titre(t): print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")
def q(sql): print(con.sql(sql))

titre("1.1  Unicite de patient_id")
q("""
SELECT count(*) AS lignes_totales,
       count(DISTINCT patient_id) AS patients_distincts,
       count(*) - count(DISTINCT patient_id) AS doublons_inter_jours
FROM patients
""")

titre("1.2  Doublons A L'INTERIEUR d'un meme fichier journalier")
q("""
SELECT jour_depot, count(*) AS lignes, count(DISTINCT patient_id) AS ids_distincts,
       count(*) - count(DISTINCT patient_id) AS doublons_intra_jour
FROM patients GROUP BY jour_depot ORDER BY jour_depot
""")

titre("1.3  Presence d'un meme patient sur N jours de depot")
q("""
SELECT nb_jours_presents, count(*) AS nb_patients
FROM (SELECT patient_id, count(DISTINCT jour_depot) AS nb_jours_presents
      FROM patients GROUP BY patient_id)
GROUP BY nb_jours_presents ORDER BY nb_jours_presents
""")

titre("1.4  Les redepots modifient-ils les attributs ? (justifie 'garder le plus recent')")
q("""
SELECT patient_id,
       count(DISTINCT nom)        AS v_nom,
       count(DISTINCT prenom)     AS v_prenom,
       count(DISTINCT birth_date) AS v_birth,
       count(DISTINCT sex)        AS v_sex,
       count(DISTINCT region_code) AS v_region,
       count(DISTINCT nir)        AS v_nir
FROM patients GROUP BY patient_id
HAVING greatest(v_nom, v_prenom, v_birth, v_sex, v_region, v_nir) > 1
LIMIT 10
""")
q("""
SELECT count(*) AS patients_avec_attributs_divergents FROM (
  SELECT patient_id FROM patients GROUP BY patient_id
  HAVING count(DISTINCT nom) > 1 OR count(DISTINCT prenom) > 1 OR count(DISTINCT birth_date) > 1
      OR count(DISTINCT sex) > 1 OR count(DISTINCT region_code) > 1 OR count(DISTINCT nir) > 1)
""")

titre("1.5  Valeurs manquantes par colonne")
q("""
SELECT 'patient_id' AS colonne, count(*) FILTER (patient_id IS NULL OR trim(patient_id) = '') AS vides FROM patients
UNION ALL SELECT 'nir',         count(*) FILTER (nir IS NULL OR trim(nir) = '') FROM patients
UNION ALL SELECT 'nom',         count(*) FILTER (nom IS NULL OR trim(nom) = '') FROM patients
UNION ALL SELECT 'prenom',      count(*) FILTER (prenom IS NULL OR trim(prenom) = '') FROM patients
UNION ALL SELECT 'birth_date',  count(*) FILTER (birth_date IS NULL OR trim(birth_date) = '') FROM patients
UNION ALL SELECT 'sex',         count(*) FILTER (sex IS NULL OR trim(sex) = '') FROM patients
UNION ALL SELECT 'region_code', count(*) FILTER (region_code IS NULL OR trim(region_code) = '') FROM patients
ORDER BY vides DESC
""")

titre("1.6  Domaine de la colonne sex (normalisation attendue M/F)")
q("SELECT coalesce(sex,'<NULL>') AS sex, count(*) AS n FROM patients GROUP BY ALL ORDER BY n DESC")

titre("1.7  birth_date — formats et bornes")
q("""
SELECT CASE
         WHEN birth_date IS NULL OR trim(birth_date) = '' THEN '<vide>'
         WHEN regexp_matches(birth_date, '^\\d{4}-\\d{2}-\\d{2}$') THEN 'AAAA-MM-JJ'
         WHEN regexp_matches(birth_date, '^\\d{2}/\\d{2}/\\d{4}$') THEN 'JJ/MM/AAAA'
         WHEN regexp_matches(birth_date, '^\\d{2}-\\d{2}-\\d{4}$') THEN 'JJ-MM-AAAA'
         ELSE 'autre: ' || birth_date END AS format_detecte,
       count(*) AS n
FROM patients GROUP BY ALL ORDER BY n DESC LIMIT 12
""")
q("""
SELECT min(d) AS naissance_min, max(d) AS naissance_max,
       count(*) FILTER (d > current_date) AS dates_futures,
       count(*) FILTER (d < DATE '1900-01-01') AS avant_1900,
       count(*) FILTER (d IS NULL) AS non_parsables
FROM (SELECT try_cast(birth_date AS DATE) AS d FROM patients)
""")

titre("1.8  NIR — longueur et composition (donnee directement identifiante)")
q("""
SELECT length(nir) AS longueur, count(*) AS n,
       count(*) FILTER (NOT regexp_matches(nir, '^\\d+$')) AS non_numeriques
FROM patients GROUP BY ALL ORDER BY n DESC
""")

titre("1.9  region_code")
q("""
SELECT count(DISTINCT region_code) AS codes_distincts,
       count(*) FILTER (NOT regexp_matches(region_code, '^\\d{2,3}$')) AS format_inattendu
FROM patients
""")
