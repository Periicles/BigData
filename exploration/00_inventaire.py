"""Inventaire des sources brutes du filestorage CHU.

Lecture en VARCHAR : on veut constater les formats reels, pas ceux
qu'un auto-typage aurait silencieusement corriges.
"""

import duckdb

SRC = "eds-chu-sujet/source-filestorage"
con = duckdb.connect()


def titre(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def q(sql):
    print(con.sql(sql))


titre("1. VOLUMETRIE PAR SOURCE ET PAR JOUR DE DEPOT")

con.execute(f"""
CREATE VIEW patients AS
SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
FROM read_csv('{SRC}/patients/*/patients.csv', all_varchar=true, filename=true)
""")
con.execute(f"""
CREATE VIEW sejours AS
SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
FROM read_csv('{SRC}/sejours/*/sejours.csv', all_varchar=true, filename=true)
""")
con.execute(f"""
CREATE VIEW monitoring AS
SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
FROM read_parquet('{SRC}/monitoring/*/monitoring.parquet', filename=true)
""")
con.execute(f"""
CREATE VIEW diagnostics_raw AS
SELECT *, regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS jour_depot
FROM read_json('{SRC}/diagnostics/*/diagnostics.json', filename=true)
""")

q("""
SELECT 'patients' AS source, jour_depot, count(*) AS lignes FROM patients GROUP BY ALL
UNION ALL SELECT 'sejours', jour_depot, count(*) FROM sejours GROUP BY ALL
UNION ALL SELECT 'diagnostics', jour_depot, count(*) FROM diagnostics_raw GROUP BY ALL
UNION ALL SELECT 'monitoring', jour_depot, count(*) FROM monitoring GROUP BY ALL
ORDER BY source, jour_depot
""")

titre("2. SCHEMAS REELS")
for v in ("patients", "sejours", "monitoring"):
    print(f"\n-- {v} --")
    q(f"DESCRIBE SELECT * FROM {v}")

print("\n-- diagnostics (json imbrique) --")
q("DESCRIBE SELECT * FROM diagnostics_raw")
print("\n-- echantillon diagnostics --")
q("SELECT stay_id, diagnostics FROM diagnostics_raw LIMIT 3")

titre("3. REFERENTIELS")
q(f"SELECT * FROM read_csv('{SRC}/referentiels/*/services.csv', all_varchar=true)")
q(f"SELECT * FROM read_csv('{SRC}/referentiels/*/cim10.csv', all_varchar=true)")
