-- ─────────────────────────────────────────────────────────────────────────
-- SILVER — nettoyé, dédupliqué, cohérent, enrichi.
--
-- Recalculé intégralement à chaque exécution depuis bronze. À ce volume
-- (15 000 séjours, 67 000 relevés) c'est instantané, et cela garantit qu'un
-- rejeu produit exactement le même état. La limite de ce choix est documentée
-- dans le rapport : il ne tiendrait pas sur plusieurs années d'historique.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.patients
(
    patient_pseudo   String,
    birth_year       UInt16,
    sex              LowCardinality(String),
    region_code      LowCardinality(String),
    -- Jour de dépôt de la version retenue : trace de la déduplication.
    _jour_depot_retenu Date,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
ORDER BY (patient_pseudo);

CREATE TABLE IF NOT EXISTS silver.sejours
(
    stay_id          String,
    patient_pseudo   String,
    service_code     LowCardinality(String),
    service_label    String,                 -- enrichi depuis le référentiel
    admission_ts     DateTime,
    discharge_ts     Nullable(DateTime),
    admission_mode   LowCardinality(String),
    discharge_mode   LowCardinality(String), -- '' normalisé en 'inconnu'
    duree_jours      Nullable(Float64),      -- NULL si séjour en cours
    est_en_cours     UInt8,
    age_au_sejour    Nullable(Int16),        -- approximé à l'année (RGPD)
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
ORDER BY (stay_id);

CREATE TABLE IF NOT EXISTS silver.diagnostics
(
    stay_id          String,
    code_cim10       LowCardinality(String),
    type_diag        LowCardinality(String),
    libelle          String,                 -- enrichi depuis le référentiel
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
ORDER BY (stay_id, code_cim10);

CREATE TABLE IF NOT EXISTS silver.monitoring
(
    stay_id          String,
    ts               DateTime,
    heart_rate       Int16,
    spo2             Int16,
    temp_c           Decimal(4, 1),
    alerte_fc        UInt8,
    alerte_spo2      UInt8,
    alerte_temp      UInt8,
    en_alerte        UInt8,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)     -- la source volumineuse : partitionnée sur la
ORDER BY (stay_id, ts);       -- date de la MESURE, jamais du dépôt

-- Table centrale pour le critère « qualité des traitements » : elle rend les
-- exclusions comptables et interrogeables, au lieu de silencieuses.
CREATE TABLE IF NOT EXISTS silver.rejets
(
    source           LowCardinality(String),
    cle              String,
    motif            LowCardinality(String),
    detail           String,
    _run_id          String,
    _rejected_at     DateTime
)
ENGINE = MergeTree
ORDER BY (source, motif, cle);
