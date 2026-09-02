-- ─────────────────────────────────────────────────────────────────────────
-- BRONZE — tables typées, peu transformées, une par source.
--
-- Partitionnées par jour de dépôt : rejouer un jour se fait par
-- DROP PARTITION puis réinsertion, sans toucher aux autres jours.
--
-- Les colonnes _jour_depot / _ingested_at / _run_id sont présentes partout :
-- elles répondent au critère de traçabilité (d'où vient la ligne, quand elle
-- a été traitée, par quelle exécution).
-- ─────────────────────────────────────────────────────────────────────────

-- birth_year est NULLABLE : une date de naissance illisible dans la source
-- (cf. eds/lake.py `annee_naissance`) ne doit pas faire échouer le
-- chargement du jour ENTIER. Elle entre en NULL pour être comptée et
-- tracée ; silver conserve le patient (attribut descriptif, comme le sexe)
-- et signale la ligne en quarantaine.
--
-- patient_pseudo peut être une chaîne VIDE : un patient_id vide en source ne
-- produit pas de hachage (cf. eds/lake.py `pseudonymiser`). Silver écarte
-- ces lignes, motif 'patient_manquant' — les conserver agrégerait toutes les
-- lignes sans identifiant sous un faux patient partagé.
CREATE TABLE IF NOT EXISTS bronze.patients
(
    patient_pseudo  String,
    birth_year      Nullable(UInt16),
    sex             LowCardinality(String),
    region_code     LowCardinality(String),

    _jour_depot     Date,
    -- Chemin du fichier d'origine, relevé par ClickHouse à la lecture :
    -- pour toute ligne de l'entrepôt, on sait de quel dépôt elle provient.
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
PARTITION BY _jour_depot
ORDER BY (patient_pseudo);

-- Même principe que pour le monitoring : les dates sont lues en mode
-- TOLÉRANT. Une date illisible doit pouvoir entrer pour être comptée et
-- tracée ; un parseur strict ferait échouer le chargement du jour ENTIER, et
-- l'on perdrait à la fois la donnée et la mesure de la qualité.
CREATE TABLE IF NOT EXISTS bronze.sejours
(
    stay_id         String,
    patient_pseudo  String,
    service_code    LowCardinality(String),
    -- NULL = date illisible dans la source. Silver l'écartera, en la traçant.
    admission_ts    Nullable(DateTime),
    -- NULL = séjour en cours (légitime, pas une anomalie) OU date illisible.
    -- Le drapeau ci-dessous sépare les deux cas, que le NULL confond.
    discharge_ts    Nullable(DateTime),
    _discharge_illisible UInt8,
    admission_mode  LowCardinality(String),
    -- Peut être vide : un séjour en cours n'a pas encore de mode de sortie.
    -- La normalisation en 'inconnu' a lieu en silver, pas ici.
    discharge_mode  LowCardinality(String),

    _jour_depot     Date,
    -- Chemin du fichier d'origine, relevé par ClickHouse à la lecture :
    -- pour toute ligne de l'entrepôt, on sait de quel dépôt elle provient.
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
PARTITION BY _jour_depot
ORDER BY (stay_id);

-- Le JSON source est imbriqué (1..n codes par séjour). L'aplatissement est
-- fait par ClickHouse à l'insertion (ARRAY JOIN) : c'est une mise en forme
-- tabulaire, aucune ligne n'est créée ni perdue.
CREATE TABLE IF NOT EXISTS bronze.diagnostics
(
    stay_id         String,
    code_cim10      LowCardinality(String),
    type_diag       LowCardinality(String),

    _jour_depot     Date,
    -- Chemin du fichier d'origine, relevé par ClickHouse à la lecture :
    -- pour toute ligne de l'entrepôt, on sait de quel dépôt elle provient.
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
PARTITION BY _jour_depot
ORDER BY (stay_id, code_cim10);

-- Types volontairement LARGES et SIGNÉS : les valeurs aberrantes (0 et
-- 500 bpm, 0 et 120 %) doivent pouvoir ENTRER pour être comptées et tracées.
-- Un type contraint les rejetterait au chargement, et l'on perdrait la mesure
-- de la qualité elle-même.
CREATE TABLE IF NOT EXISTS bronze.monitoring
(
    stay_id         String,
    ts              DateTime,
    heart_rate      Int16,
    spo2            Int16,
    temp_c          Decimal(4, 1),

    _jour_depot     Date,
    -- Chemin du fichier d'origine, relevé par ClickHouse à la lecture :
    -- pour toute ligne de l'entrepôt, on sait de quel dépôt elle provient.
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
PARTITION BY _jour_depot
ORDER BY (stay_id, ts);

-- Référentiels : déposés le PREMIER JOUR seulement. Ils sont donc rechargés
-- intégralement à chaque exécution, hors du flux incrémental journalier —
-- sinon un pipeline démarré au jour 2 n'aurait aucune nomenclature.
CREATE TABLE IF NOT EXISTS bronze.ref_services
(
    service_code    LowCardinality(String),
    service_label   String,
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
ORDER BY (service_code);

CREATE TABLE IF NOT EXISTS bronze.ref_cim10
(
    code_cim10      LowCardinality(String),
    libelle         String,
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
ORDER BY (code_cim10);
