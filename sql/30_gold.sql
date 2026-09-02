-- ─────────────────────────────────────────────────────────────────────────
-- GOLD — modèle dimensionnel en étoile.
--
-- Trois tables de faits, à TROIS GRAINS DIFFÉRENTS, partageant des
-- dimensions conformes. C'est ce qui permet à un analyste de croiser
-- librement les axes sans qu'on ait pré-calculé chaque combinaison.
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
-- COMMENT SE CONSTRUIT UN FAIT : les dimensions d'abord, les faits ensuite.
-- Tout attribut de dimension dont un fait a besoin est lu DANS la dimension,
-- jamais re-dérivé depuis silver. C'est le cas de `age_au_sejour`, qui n'est
-- ni un attribut de la personne (il change à chaque séjour) ni une donnée de
-- la source : il croise `dim_patient.birth_year` et l'axe `date_admission`
-- du fait, et se calcule donc ici, contre la dimension.
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
-- fait, plutôt que laissés à la charge du consommateur : le calcul de
-- réadmission est une auto-jointure, hors de portée d'une requête d'analyse
-- ordinaire. Le porter dans le fait rend le KPI aussi simple qu'une moyenne.
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
-- Grain : UN CODE POSÉ LORS D'UN SÉJOUR. 1 à 3 lignes par séjour.
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
--
-- Les quatre drapeaux d'alerte sont posés ICI et non en silver : il n'existe
-- aucun seuil d'alerte réglementaire. Les moniteurs sortent d'usine avec des
-- valeurs par défaut que chaque service, voire chaque patient, ajuste. C'est
-- donc une règle métier paramétrable — ses seuils viennent de la
-- configuration (cf. eds/config.py), pas du code.
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
--
-- GRAIN DE LA COHORTE : le DIAGNOSTIC PRINCIPAL, soit le motif
-- d'hospitalisation. Les diagnostics associés (comorbidités) sont exclus —
-- ils doubleraient le chiffre. Justification en tête de 31_gold_transform.sql,
-- à énoncer partout où l'indicateur est diffusé.

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


-- ══ PILOTAGE — INDICATEURS AGRÉGÉS ══════════════════════════════════════
--
-- Le § 6 du sujet décrit gold comme des « indicateurs par usage ». Ces
-- quatre tables sont exactement cela : un agrégat par indicateur du § 4,
-- prêt à être lu sans jointure ni calcul.
--
-- Elles NE REMPLACENT PAS les faits, elles s'y ajoutent. Une table figée
-- ne répond qu'à la question qu'on a anticipée : croiser la DMS par service
-- ET par tranche d'âge, ou la ventiler par pathologie, exige de revenir au
-- grain du séjour. L'étoile garde donc sa raison d'être — mais la lecture
-- courante, celle des six indicateurs demandés, n'a plus besoin d'elle.
--
-- Deux conséquences pratiques :
--   1. un indicateur = une table = une requête sans jointure ;
--   2. le compte de restitution peut être borné à ces seules tables, donc
--      privé du grain de l'événement et du pseudonyme patient.
--
-- Chaque table porte son EFFECTIF à côté de sa mesure. Un taux sans son
-- dénominateur n'est pas interprétable : 24 % d'alertes sur 29 relevés et
-- sur 2 000 ne se lisent pas de la même façon.

-- DMS par service et par mois. Les séjours EN COURS sont exclus : ils n'ont
-- pas de durée, et les compter au dénominateur écraserait la moyenne.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_dms_service
(
    service_code     LowCardinality(String),
    service          String,
    mois             Date,                -- premier jour du mois
    dms_jours        Float64,
    -- La MOYENNE SEULE MASQUE LA QUEUE. En réanimation, moyenne 9,05 mais
    -- P90 à 18,03 : la moitié de l'écart entre un séjour ordinaire et un
    -- séjour long est invisible si l'on ne publie que la DMS. Un service
    -- qui pilote ses lits a besoin des trois.
    mediane_jours    Float64,
    p90_jours        Float64,
    max_jours        Float64,
    nb_sejours_clos  UInt32,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code, mois);

-- Activité des urgences, par jour d'ADMISSION — jamais par jour de dépôt.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_urgences_jour
(
    jour             Date,
    nb_passages      UInt32,
    nb_sejours       UInt32,             -- toutes admissions confondues
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (jour);

-- Réadmission à 30 jours. Numérateur et dénominateur sont exposés à côté du
-- taux : sans eux, impossible de savoir si 12 % porte sur 50 ou 5 000
-- séjours, ni de recomposer l'indicateur sur un autre périmètre.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_readmission_service
(
    service_code     LowCardinality(String),
    service          String,
    nb_sejours_index UInt32,             -- dénominateur : clos et non décédés
    nb_readmis_30j   UInt32,             -- numérateur
    taux_pct         Float64,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code);

-- Relevés en alerte par jour de MESURE et par service. Le périmètre réel
-- est restreint aux services équipés : les autres n'ont aucune ligne ici,
-- plutôt qu'une ligne à zéro qui se lirait comme « aucune alerte ».
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_alertes_jour
(
    jour             Date,
    service_code     LowCardinality(String),
    service          String,
    nb_releves       UInt32,
    nb_en_alerte     UInt32,
    taux_pct         Float64,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (jour, service_code);


-- ── « Toute autre vue d'activité pertinente » (§ 4) ──────────────────────
--
-- Le sujet nomme quatre indicateurs de pilotage puis laisse une cinquième
-- ligne ouverte. Trois vues la remplissent, chacune répondant à une question
-- qu'aucune des quatre premières ne couvre.

-- OCCUPATION — combien de patients sont présents, un jour donné, dans un
-- service. C'est la vue que regarde une direction en premier, et la seule
-- qui croise le FLUX (admissions) et la DURÉE : à activité égale, un service
-- dont la DMS double occupe deux fois plus de lits.
--
-- Un séjour est déroulé sur tout son intervalle : il compte présent chaque
-- jour entre son admission et sa sortie. La série s'arrête au dernier jour
-- de dépôt — au-delà, seuls les séjours déjà admis subsisteraient, et la
-- courbe descendrait pour une raison qui n'a rien de métier.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_occupation_jour
(
    jour             Date,
    service_code     LowCardinality(String),
    service          String,
    nb_presents      UInt32,
    nb_admissions    UInt32,
    nb_sorties       UInt32,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (jour, service_code);

-- MORTALITÉ HOSPITALIÈRE — le pendant de la réadmission. Le sujet range
-- celle-ci sous « qualité des soins » ; la mortalité en est l'autre moitié,
-- et elle se lit sur `discharge_mode`.
--
-- RÉSERVE À ÉNONCER AVEC LE CHIFFRE : sur les données fournies, les modes de
-- sortie sont uniformément distribués, ce qui produit des taux sans aucune
-- plausibilité clinique (19,6 % en pédiatrie). L'indicateur est correct, la
-- donnée qui l'alimente ne l'est pas. Il est publié pour la complétude de la
-- vue métier, jamais pour être interprété tel quel.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_mortalite_service
(
    service_code     LowCardinality(String),
    service          String,
    nb_sejours_clos  UInt32,
    nb_deces         UInt32,
    taux_pct         Float64,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code);

-- CASE-MIX — de quoi un service soigne-t-il ses patients. Répond à une
-- question que ni la DMS ni l'occupation ne posent : deux services de même
-- activité et de même durée peuvent traiter des pathologies sans rapport.
--
-- Au grain du diagnostic PRINCIPAL, comme les cohortes de recherche : c'est
-- le motif d'hospitalisation, pas la comorbidité.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_casemix_service
(
    service_code     LowCardinality(String),
    service          String,
    code_cim10       LowCardinality(String),
    pathologie       String,
    nb_sejours       UInt32,
    part_pct         Float64,             -- part dans l'activité du service
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code, code_cim10);
