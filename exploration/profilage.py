"""Profilage des cinq sources du CHU, mené AVANT toute conception.

Le sujet le demande explicitement : « Commencez par explorer les fichiers
pour comprendre ce que vous avez entre les mains avant de coder quoi que ce
soit. » Trois constats en sont sortis, qui déterminent l'architecture et ne
se déduisaient pas du cahier des charges :

  - `patients` est un snapshot CUMULATIF (16 200 lignes pour 6 000 patients)
    quand les trois autres sources sont des deltas ; une ingestion uniforme
    multiplierait par 2,7 tout indicateur rapporté au patient ;
  - le monitoring DÉBORDE de son jour de dépôt (fichier du 26 août, relevés
    jusqu'au 28) : l'agrégation doit porter sur l'horodatage de mesure ;
  - les valeurs aberrantes sont une PANNE DE CAPTEUR, pas du bruit — FC et
    SpO2 toujours invalides ensemble, sur quatre valeurs de butée.

Lecture en VARCHAR : on veut constater les formats réels, pas ceux qu'un
auto-typage aurait silencieusement corrigés.

    python -m exploration.profilage            # les six sections
    python -m exploration.profilage patients   # une seule
"""

import sys

import duckdb

from eds import choisir_sections

SRC = "eds-chu-sujet/source-filestorage"
JOUR = "regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1) AS jour_depot"

con = duckdb.connect()


def _vues() -> None:
    """Les vues sont créées une fois pour toutes les sections."""
    con.execute(f"CREATE VIEW patients AS SELECT *, {JOUR} FROM read_csv('{SRC}/patients/*/patients.csv', all_varchar=true, filename=true)")
    con.execute(f"CREATE VIEW sejours AS SELECT *, {JOUR} FROM read_csv('{SRC}/sejours/*/sejours.csv', all_varchar=true, filename=true)")
    con.execute(f"CREATE VIEW monitoring AS SELECT *, {JOUR} FROM read_parquet('{SRC}/monitoring/*/monitoring.parquet', filename=true)")
    con.execute(f"CREATE VIEW diag_raw AS SELECT *, {JOUR} FROM read_json('{SRC}/diagnostics/*/diagnostics.json', filename=true)")
    con.execute(f"CREATE VIEW services AS SELECT * FROM read_csv('{SRC}/referentiels/*/services.csv', all_varchar=true)")
    con.execute(f"CREATE VIEW cim10 AS SELECT * FROM read_csv('{SRC}/referentiels/*/cim10.csv', all_varchar=true)")
    con.execute("CREATE VIEW diag AS SELECT stay_id, jour_depot, d.code_cim10, d.type FROM diag_raw, unnest(diagnostics) AS t(d)")
    # Population dédupliquée, telle qu'elle entrerait dans l'entrepôt.
    con.execute("""
    CREATE VIEW pop AS
    SELECT DISTINCT ON (patient_id) patient_id,
           year(try_cast(birth_date AS DATE)) AS annee_naissance, sex, region_code
    FROM patients ORDER BY patient_id, jour_depot DESC
    """)


def titre(t: str) -> None:
    print(f"\n{'-' * 74}\n{t}\n{'-' * 74}")


def q(sql: str) -> None:
    print(con.sql(sql))


# ── 0. Inventaire ────────────────────────────────────────────────────────
def inventaire() -> None:
    titre("0.1  Volumétrie par source et par jour de dépôt")
    q("""
    SELECT 'patients' AS source, jour_depot, count(*) AS lignes FROM patients GROUP BY ALL
    UNION ALL SELECT 'sejours', jour_depot, count(*) FROM sejours GROUP BY ALL
    UNION ALL SELECT 'diagnostics', jour_depot, count(*) FROM diag_raw GROUP BY ALL
    UNION ALL SELECT 'monitoring', jour_depot, count(*) FROM monitoring GROUP BY ALL
    ORDER BY source, jour_depot
    """)

    titre("0.2  Schémas réels")
    for v in ("patients", "sejours", "monitoring", "diag_raw"):
        print(f"\n-- {v} --")
        q(f"DESCRIBE SELECT * FROM {v}")

    titre("0.3  Référentiels")
    q("SELECT * FROM services")
    q("SELECT * FROM cim10")


# ── 1. Patients ──────────────────────────────────────────────────────────
def patients() -> None:
    titre("1.1  Unicité de patient_id")
    q("""
    SELECT count(*) AS lignes_totales, count(DISTINCT patient_id) AS patients_distincts,
           count(*) - count(DISTINCT patient_id) AS doublons_inter_jours
    FROM patients
    """)

    titre("1.2  Doublons À L'INTÉRIEUR d'un même fichier journalier")
    q("""
    SELECT jour_depot, count(*) AS lignes, count(DISTINCT patient_id) AS ids_distincts,
           count(*) - count(DISTINCT patient_id) AS doublons_intra_jour
    FROM patients GROUP BY jour_depot ORDER BY jour_depot
    """)

    titre("1.3  Présence d'un même patient sur N jours de dépôt")
    q("""
    SELECT nb_jours_presents, count(*) AS nb_patients
    FROM (SELECT patient_id, count(DISTINCT jour_depot) AS nb_jours_presents
          FROM patients GROUP BY patient_id)
    GROUP BY nb_jours_presents ORDER BY nb_jours_presents
    """)

    titre("1.4  Les redépôts modifient-ils les attributs ? (justifie « garder le plus récent »)")
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

    titre("1.8  NIR — longueur et composition (donnée directement identifiante)")
    q("""
    SELECT length(nir) AS longueur, count(*) AS n,
           count(*) FILTER (NOT regexp_matches(nir, '^\\d+$')) AS non_numeriques
    FROM patients GROUP BY ALL ORDER BY n DESC
    """)


# ── 2. Séjours ───────────────────────────────────────────────────────────
def sejours() -> None:
    titre("2.0  NATURE DU DÉPÔT — snapshot cumulatif ou delta ?")
    q("""
    SELECT jour_premiere_apparition, count(*) AS nb_patients
    FROM (SELECT patient_id, min(jour_depot) AS jour_premiere_apparition FROM patients GROUP BY patient_id)
    GROUP BY ALL ORDER BY 1
    """)
    q("""
    SELECT jour_depot, count(*) AS sejours_du_fichier, count(DISTINCT stay_id) AS stay_id_distincts
    FROM sejours GROUP BY ALL ORDER BY 1
    """)

    titre("2.1  Unicité de stay_id et valeurs manquantes")
    q("""
    SELECT count(*) AS lignes, count(DISTINCT stay_id) AS stay_distincts,
           count(*) FILTER (patient_id IS NULL OR trim(patient_id)='') AS patient_id_vide,
           count(*) FILTER (service_code IS NULL OR trim(service_code)='') AS service_vide,
           count(*) FILTER (admission_ts IS NULL OR trim(admission_ts)='') AS admission_vide,
           count(*) FILTER (discharge_ts IS NULL OR trim(discharge_ts)='') AS discharge_vide
    FROM sejours
    """)

    titre("2.2  CONTRÔLE — cohérence temporelle (discharge_ts < admission_ts)")
    q("""
    SELECT count(*) AS lignes_totales,
           count(*) FILTER (a IS NULL) AS admission_non_parsable,
           count(*) FILTER (d IS NULL OR trim(coalesce(discharge_ts,'')) = '') AS sejour_en_cours_legitime,
           count(*) FILTER (d IS NOT NULL AND a IS NOT NULL AND d < a) AS INCOHERENCE_TEMPORELLE
    FROM (SELECT discharge_ts, try_cast(admission_ts AS TIMESTAMP) a, try_cast(discharge_ts AS TIMESTAMP) d FROM sejours)
    """)

    titre("2.3  Domaines admission_mode / discharge_mode")
    q("SELECT coalesce(nullif(trim(admission_mode),''),'<vide>') AS admission_mode, count(*) n FROM sejours GROUP BY ALL ORDER BY n DESC")
    q("SELECT coalesce(nullif(trim(discharge_mode),''),'<vide>') AS discharge_mode, count(*) n FROM sejours GROUP BY ALL ORDER BY n DESC")

    titre("2.4  INTÉGRITÉ — service_code absent du référentiel, séjours orphelins")
    q("""
    SELECT s.service_code, count(*) AS n
    FROM sejours s LEFT JOIN services r ON s.service_code = r.service_code
    WHERE r.service_code IS NULL GROUP BY ALL ORDER BY n DESC
    """)
    q("""
    SELECT count(DISTINCT s.stay_id) AS sejours_orphelins, count(DISTINCT s.patient_id) AS patients_inconnus
    FROM sejours s LEFT JOIN (SELECT DISTINCT patient_id FROM patients) p USING (patient_id)
    WHERE p.patient_id IS NULL
    """)

    titre("2.5  Durée de séjour (base DMS) — distribution")
    q("""
    SELECT round(min(duree_j),2) AS min_j, round(quantile_cont(duree_j,0.5),2) AS mediane_j,
           round(avg(duree_j),2) AS moyenne_j, round(quantile_cont(duree_j,0.99),2) AS p99_j,
           round(max(duree_j),2) AS max_j,
           count(*) FILTER (duree_j < 0) AS durees_negatives, count(*) FILTER (duree_j > 365) AS plus_d_un_an
    FROM (SELECT date_diff('minute', try_cast(admission_ts AS TIMESTAMP), try_cast(discharge_ts AS TIMESTAMP)) / 1440.0 AS duree_j
          FROM sejours WHERE try_cast(discharge_ts AS TIMESTAMP) IS NOT NULL)
    """)

    titre("2.6  ANOMALIE NON LISTÉE — discharge_mode vide alors que le séjour est clos")
    q("""
    SELECT CASE WHEN trim(coalesce(discharge_ts,'')) = '' THEN 'sejour en cours' ELSE 'sejour clos' END AS etat,
           CASE WHEN trim(coalesce(discharge_mode,'')) = '' THEN 'mode ABSENT' ELSE 'mode present' END AS mode,
           count(*) AS n
    FROM sejours GROUP BY ALL ORDER BY 1, 2
    """)

    titre("2.7  FAISABILITÉ — taux de réadmission à 30 jours")
    q("""
    WITH s AS (
      SELECT patient_id, stay_id, try_cast(admission_ts AS TIMESTAMP) a,
             try_cast(discharge_ts AS TIMESTAMP) d, discharge_mode FROM sejours
    ), paires AS (
      SELECT s1.patient_id, date_diff('day', s1.d, s2.a) AS delai_j, s1.discharge_mode
      FROM s s1 JOIN s s2 ON s1.patient_id = s2.patient_id AND s2.a > s1.d WHERE s1.d IS NOT NULL
    )
    SELECT count(*) AS paires_readmission_30j, count(*) FILTER (discharge_mode = 'deces') AS dont_apres_un_deces,
           min(delai_j) AS delai_min_j, max(delai_j) AS delai_max_j
    FROM paires WHERE delai_j <= 30
    """)


# ── 3. Diagnostics ───────────────────────────────────────────────────────
def diagnostics() -> None:
    titre("3.1  Structure et volumétrie")
    q("""
    SELECT jour_depot, count(DISTINCT stay_id) AS sejours, count(*) AS codes,
           round(count(*)::DOUBLE / count(DISTINCT stay_id), 2) AS codes_par_sejour
    FROM diag GROUP BY ALL ORDER BY 1
    """)

    titre("3.2  Domaine de « type » et règle un principal par séjour")
    q("SELECT coalesce(type,'<NULL>') AS type, count(*) n FROM diag GROUP BY ALL ORDER BY n DESC")
    q("""
    SELECT nb_principaux, count(*) AS nb_sejours
    FROM (SELECT stay_id, count(*) FILTER (type = 'principal') AS nb_principaux FROM diag GROUP BY stay_id)
    GROUP BY ALL ORDER BY 1
    """)

    titre("3.3  Intégrité référentielle")
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


# ── 4. Monitoring ────────────────────────────────────────────────────────
def monitoring() -> None:
    titre("4.1  Volumétrie et couverture — le flux DÉBORDE de son jour de dépôt")
    q("""
    SELECT jour_depot, count(*) AS releves, count(DISTINCT stay_id) AS sejours_suivis,
           min(ts) AS ts_min, max(ts) AS ts_max
    FROM monitoring GROUP BY ALL ORDER BY 1
    """)
    q("""
    SELECT count(DISTINCT m.stay_id) AS monitoring_orphelin
    FROM monitoring m LEFT JOIN (SELECT DISTINCT stay_id FROM sejours) s USING (stay_id) WHERE s.stay_id IS NULL
    """)

    titre("4.2  Valeurs manquantes et hors plage physiologique")
    q("""
    SELECT count(*) AS releves,
           count(*) FILTER (heart_rate NOT BETWEEN 20 AND 250) AS fc_hors_plage,
           count(*) FILTER (spo2 NOT BETWEEN 50 AND 100) AS spo2_hors_plage,
           count(*) FILTER (temp_c NOT BETWEEN 30 AND 45) AS temp_hors_plage
    FROM monitoring
    """)

    titre("4.3  PANNE DE CAPTEUR — FC et SpO2 aberrantes sont-elles sur les MÊMES lignes ?")
    q("""
    SELECT count(*) FILTER (fc_ko AND spo2_ko) AS les_deux_ensemble,
           count(*) FILTER (fc_ko AND NOT spo2_ko) AS fc_seule,
           count(*) FILTER (spo2_ko AND NOT fc_ko) AS spo2_seule
    FROM (SELECT heart_rate NOT BETWEEN 20 AND 250 AS fc_ko, spo2 NOT BETWEEN 50 AND 100 AS spo2_ko FROM monitoring)
    """)

    titre("4.4  Valeurs sentinelles exactes (quatre butées, pas du bruit)")
    q("""
    SELECT heart_rate, spo2, count(*) AS n
    FROM monitoring WHERE heart_rate NOT BETWEEN 20 AND 250 OR spo2 NOT BETWEEN 50 AND 100
    GROUP BY ALL ORDER BY n DESC LIMIT 10
    """)

    titre("4.5  Quels services sont monitorés ?")
    q("""
    SELECT s.service_code, count(DISTINCT s.stay_id) AS sejours,
           count(DISTINCT m.stay_id) AS sejours_monitores,
           round(100.0 * count(DISTINCT m.stay_id) / count(DISTINCT s.stay_id), 1) AS pct
    FROM sejours s LEFT JOIN (SELECT DISTINCT stay_id FROM monitoring) m USING (stay_id)
    GROUP BY ALL ORDER BY pct DESC
    """)

    titre("4.6  Relevés hors fenêtre — avant admission ou après sortie ?")
    q("""
    SELECT count(*) FILTER (m.ts < a) AS avant_admission,
           count(*) FILTER (d IS NOT NULL AND m.ts > d) AS apres_sortie,
           count(DISTINCT m.stay_id) FILTER (m.ts < a OR (d IS NOT NULL AND m.ts > d)) AS sejours_concernes
    FROM monitoring m JOIN (SELECT stay_id, try_cast(admission_ts AS TIMESTAMP) a,
                                   try_cast(discharge_ts AS TIMESTAMP) d FROM sejours) s USING (stay_id)
    """)

    titre("4.7  ALERTES — volumétrie après exclusion des aberrations")
    q("""
    SELECT count(*) AS releves_valides,
           count(*) FILTER (heart_rate > 120 OR heart_rate < 40) AS alerte_fc,
           count(*) FILTER (spo2 < 92) AS alerte_spo2,
           count(*) FILTER (temp_c > 38.5) AS alerte_temp
    FROM monitoring
    WHERE heart_rate BETWEEN 20 AND 250 AND spo2 BETWEEN 50 AND 100 AND temp_c BETWEEN 30 AND 45
    """)


# ── 5. Ré-identification ─────────────────────────────────────────────────
def reidentification() -> None:
    titre("5.1  k-anonymat sur (année de naissance, sexe, région)")
    q("""
    SELECT CASE WHEN k = 1 THEN 'k = 1  (UNIQUE — re-identifiable)'
                WHEN k < 5 THEN 'k = 2..4  (sous le seuil de 5)'
                ELSE 'k >= 5  (conforme)' END AS classe,
           count(*) AS nb_groupes, sum(k) AS nb_patients,
           round(100.0 * sum(k) / (SELECT count(*) FROM pop), 1) AS pct_population
    FROM (SELECT annee_naissance, sex, region_code, count(*) AS k FROM pop GROUP BY ALL)
    GROUP BY ALL ORDER BY nb_patients DESC
    """)

    titre("5.2  Effet de la généralisation en tranches d'âge de 10 ans")
    q("""
    SELECT CASE WHEN k = 1 THEN 'k = 1' WHEN k < 5 THEN 'k = 2..4' ELSE 'k >= 5' END AS classe,
           count(*) AS nb_groupes, sum(k) AS nb_patients,
           round(100.0 * sum(k) / (SELECT count(*) FROM pop), 1) AS pct_population
    FROM (SELECT (2026 - annee_naissance) // 10 AS tranche, sex, region_code, count(*) AS k FROM pop GROUP BY ALL)
    GROUP BY ALL ORDER BY nb_patients DESC
    """)

    titre("5.3  Cohortes recherche sous le seuil de 5 patients")
    q("""
    SELECT count(*) AS cohortes_sous_seuil FROM (
      SELECT d.code_cim10, p.sex, (2026 - p.annee_naissance) // 10 AS tranche, count(DISTINCT s.patient_id) AS n
      FROM diag d JOIN sejours s USING (stay_id) JOIN pop p ON s.patient_id = p.patient_id
      WHERE d.type = 'principal' GROUP BY ALL HAVING n < 5)
    """)


SECTIONS = {
    "inventaire": inventaire,
    "patients": patients,
    "sejours": sejours,
    "diagnostics": diagnostics,
    "monitoring": monitoring,
    "reidentification": reidentification,
}


def main(argv: list[str] | None = None) -> int:
    args = choisir_sections(SECTIONS, argv)
    if args is None:
        return 2
    _vues()
    for nom in args:
        print(f"\n{'=' * 78}\n{nom.upper()}\n{'=' * 78}")
        SECTIONS[nom]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
