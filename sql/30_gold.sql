-- ─────────────────────────────────────────────────────────────────────────
-- GOLD — modèle dimensionnel en étoile.
--
-- Trois tables de faits, à TROIS GRAINS DIFFÉRENTS, partageant des
-- dimensions conformes. C'est ce qui permet à un analyste de croiser
-- librement dans Metabase sans qu'on ait pré-calculé chaque combinaison.
--
--            dim_patient        dim_service        dim_cim10
--                 │                  │                  │
--       ┌─────────┼──────────────────┼──────────┐       │
--       │         │                  │          │       │
--  fact_sejour ───┘          fact_releve   fact_diagnostic
--  1 ligne = 1 séjour     1 ligne = 1 relevé   1 ligne = 1 code posé
--
-- Pourquoi trois faits et non un seul : les grains sont incompatibles. Un
-- séjour porte 1 à 4 diagnostics et 0 à n relevés ; les fusionner
-- multiplierait les lignes et fausserait toute somme (le piège classique du
-- « fan trap »).
--
-- CLOISONNEMENT : ces tables sont au grain de l'événement et portent le
-- pseudonyme patient. Elles vivent donc dans gold_pilotage, dont l'accès est
-- restreint. La base gold_recherche n'expose que des agrégats (§ ci-dessous).
-- ─────────────────────────────────────────────────────────────────────────

-- ══ DIMENSIONS CONFORMES ════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS gold_pilotage.dim_patient
(
    patient_pseudo   String,          -- clé de dimension (pseudonyme, non réversible)
    birth_year       UInt16,
    sexe             LowCardinality(String),
    region           LowCardinality(String),
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (patient_pseudo);

CREATE TABLE IF NOT EXISTS gold_pilotage.dim_service
(
    service_code     LowCardinality(String),
    service          String,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code);

CREATE TABLE IF NOT EXISTS gold_pilotage.dim_cim10
(
    code_cim10       LowCardinality(String),
    pathologie       String,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (code_cim10);


-- ══ FAIT 1 — SÉJOUR ═════════════════════════════════════════════════════
-- Grain : UN SÉJOUR. Une ligne par passage à l'hôpital.
--
-- Les indicateurs de qualité des soins sont pré-calculés ici, au grain du
-- fait, plutôt que laissés à la charge de l'outil de restitution : le calcul
-- de réadmission est une auto-jointure, impraticable dans Metabase. Le
-- porter dans le fait rend le KPI aussi simple qu'une moyenne.
CREATE TABLE IF NOT EXISTS gold_pilotage.fact_sejour
(
    -- Clés
    stay_id                String,                  -- dimension dégénérée
    patient_pseudo         String,                  -- -> dim_patient
    service_code           LowCardinality(String),  -- -> dim_service

    -- Axes temporels
    date_admission         Date,
    date_sortie            Nullable(Date),
    admission_ts           DateTime,
    discharge_ts           Nullable(DateTime),

    -- Attributs du fait
    admission_mode         LowCardinality(String),
    discharge_mode         LowCardinality(String),
    age_au_sejour          Nullable(Int16),
    tranche_age            LowCardinality(String),

    -- Mesures
    duree_jours            Nullable(Float64),   -- NULL si séjour en cours
    est_en_cours           UInt8,
    est_urgence            UInt8,
    est_sejour_index       UInt8,   -- clos ET patient non décédé : dénominateur
    suivi_readmission_30j  UInt8,   -- réadmis dans les 30 j : numérateur

    _run_id                String,
    _built_at              DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date_admission)
ORDER BY (service_code, date_admission, stay_id);


-- ══ FAIT 2 — DIAGNOSTIC ═════════════════════════════════════════════════
-- Grain : UN CODE POSÉ LORS D'UN SÉJOUR. 1 à 4 lignes par séjour.
--
-- `patient_pseudo` y est dénormalisé depuis le séjour : les questions de
-- recherche portent sur des cohortes de PATIENTS par pathologie, et cette
-- copie leur évite une jointure systématique avec fact_sejour.
CREATE TABLE IF NOT EXISTS gold_pilotage.fact_diagnostic
(
    stay_id          String,                  -- dimension dégénérée
    patient_pseudo   String,                  -- -> dim_patient
    code_cim10       LowCardinality(String),  -- -> dim_cim10
    service_code     LowCardinality(String),  -- -> dim_service

    date_admission   Date,
    type_diag        LowCardinality(String),  -- principal | associe
    est_principal    UInt8,

    age_au_sejour    Nullable(Int16),
    tranche_age      LowCardinality(String),
    sexe             LowCardinality(String),

    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date_admission)
ORDER BY (code_cim10, date_admission, stay_id);


-- ══ FAIT 3 — RELEVÉ DE CONSTANTES ═══════════════════════════════════════
-- Grain : UNE MESURE AU CHEVET. Le fait le plus volumineux.
--
-- Partitionné sur la date de la MESURE, jamais sur le jour de dépôt : les
-- fichiers de monitoring débordent de leur jour de dépôt.
CREATE TABLE IF NOT EXISTS gold_pilotage.fact_releve
(
    stay_id          String,
    patient_pseudo   String,                  -- -> dim_patient
    service_code     LowCardinality(String),  -- -> dim_service

    ts               DateTime,
    date_mesure      Date,

    -- Mesures
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
PARTITION BY toYYYYMM(date_mesure)
ORDER BY (service_code, date_mesure, stay_id, ts);


-- ══ RECHERCHE CLINIQUE — agrégats seulement ═════════════════════════════
--
-- La recherche ne reçoit PAS le modèle en étoile : ses faits sont au grain
-- de l'événement et portent le pseudonyme patient, ce qui permettrait de
-- reconstituer des cohortes sous le seuil. Elle reçoit des agrégats dérivés
-- des mêmes faits, filtrés à l'écriture :
--   1. HAVING count(DISTINCT patient) >= 5
--   2. âge en tranches de 10 ans, jamais l'année
--   3. aucun pseudonyme patient

CREATE TABLE IF NOT EXISTS gold_recherche.coh_prevalence
(
    code_cim10       String,
    pathologie       String,
    nb_patients      UInt32,
    nb_sejours       UInt32,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (code_cim10);

CREATE TABLE IF NOT EXISTS gold_recherche.coh_description
(
    code_cim10       String,
    pathologie       String,
    tranche_age      String,
    sexe             String,
    nb_patients      UInt32,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (code_cim10, tranche_age, sexe);
