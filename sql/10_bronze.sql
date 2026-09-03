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

-- Les actes sont déposés en UNE FOIS, pour toute la période, et non jour par
-- jour comme les séjours ou le monitoring. La date de l'acte n'est donc PAS
-- celle du dépôt : `_jour_depot` vaut 2026-08-29 pour un acte du 6 août.
-- Partitionner par `_jour_depot` reste néanmoins juste — la partition est
-- l'unité de rejeu du dépôt, pas une tranche temporelle métier.
--
-- L'acte ne référence le patient qu'indirectement, par `stay_id` : aucune
-- pseudonymisation n'est nécessaire en amont (cf. eds/lake.py).
CREATE TABLE IF NOT EXISTS bronze.actes
(
    stay_id         String,
    code_ccam       LowCardinality(String),
    acte_ts         DateTime,

    _jour_depot     Date,
    -- Chemin du fichier d'origine, relevé par ClickHouse à la lecture :
    -- pour toute ligne de l'entrepôt, on sait de quel dépôt elle provient.
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
PARTITION BY _jour_depot
ORDER BY (stay_id, acte_ts);

-- Référentiels : hors du flux incrémental journalier, rechargés intégralement
-- à chaque exécution — sinon un pipeline démarré au jour 2 n'aurait aucune
-- nomenclature. Ils ne sont pas tous déposés le même jour : `services.csv` et
-- `cim10.csv` le sont au premier jour, `ccam.csv` et `description_service.csv`
-- au dépôt d'évolution du 29 août. Chaque référentiel est donc résolu PAR
-- FICHIER, sur son dépôt le plus récent (cf. eds/warehouse.py).
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

-- Nomenclature des actes techniques. Le tarif est NULLABLE et lu en mode
-- tolérant : c'est une donnée de facturation, pas une clé — un tarif illisible
-- ne doit pas faire échouer le chargement de la nomenclature entière.
CREATE TABLE IF NOT EXISTS bronze.ref_ccam
(
    code_ccam       LowCardinality(String),
    libelle         String,
    tarif_euros     Nullable(Decimal(10, 2)),
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
ORDER BY (code_ccam);

-- Description administrative des services : catégorie, capacité, pôle.
--
-- Ce référentiel est INCOMPLET à la source — il décrit 7 des 8 services
-- (NEURO est absent). Aucune valeur n'est inventée ici : bronze reflète la
-- source. Le comblement en 'non renseigné' a lieu en silver, pour que les
-- totaux par catégorie ou par pôle continuent de se conserver.
--
-- `capacite_lits` est NULLABLE parce qu'elle sert de DÉNOMINATEUR : un
-- service sans capacité connue n'a pas de taux, et un ratio indéfini doit
-- rester absent — jamais valoir zéro, qui se confondrait avec « aucun acte ».
CREATE TABLE IF NOT EXISTS bronze.ref_description_service
(
    service_code    LowCardinality(String),
    categorie       LowCardinality(String),
    capacite_lits   Nullable(UInt16),
    pole            LowCardinality(String),
    _fichier_source String,
    _ingested_at    DateTime,
    _run_id         String
)
ENGINE = MergeTree
ORDER BY (service_code);
