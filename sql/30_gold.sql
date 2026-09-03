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
-- séjour porte 1 à 3 diagnostics et 0 à n relevés ; les fusionner
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
-- `fact_diagnostic` ET `fact_releve` NE LISENT PLUS `silver.sejours`, NI
-- PAR INNER NI PAR LEFT JOIN. Depuis la décision de l'intervenant (cf.
-- l'en-tête de 20_silver.sql et 21_silver_transform.sql), un diagnostic ou
-- un relevé reste valide même quand son séjour porteur est temporellement
-- incohérent : un `INNER JOIN silver.sejours` les aurait fait disparaître de
-- gold pour une anomalie qui ne les concerne pas. `silver.diagnostics` et
-- `silver.monitoring` portent désormais eux-mêmes le patient, le service,
-- l'admission ET le drapeau `sejour_coherent` (enrichis contre
-- `bronze.sejours` dès silver) : gold les lit tels quels, et se contente de
-- recopier `sejour_coherent` dans le fait, sans le filtrer.
--
-- CLOISONNEMENT : ces tables sont au grain de l'événement et portent le
-- pseudonyme patient. Elles vivent donc dans gold_pilotage, dont l'accès est
-- restreint. La base gold_recherche n'expose que des agrégats (§ ci-dessous).
-- ─────────────────────────────────────────────────────────────────────────

-- ══ DIMENSIONS CONFORMES ════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS gold_pilotage.dim_patient
(
    patient_pseudo   String,          -- clé de dimension (pseudonyme, non réversible)
    -- NULL si la date de naissance était illisible en source (tracé en
    -- quarantaine dès silver). age_au_sejour et tranche_age en tirent les
    -- conséquences : NULL et 'inconnu' respectivement, voir 31_gold_transform.sql.
    birth_year       Nullable(UInt16),
    sexe             LowCardinality(String),
    region           LowCardinality(String),
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (patient_pseudo);

-- Une HIÉRARCHIE à trois niveaux d'agrégation croissants, pas une
-- redondance : service (le plus fin, 1 par service) -> categorie -> pole.
-- Elle sert à analyser à trois échelles, pas à répéter la même information.
--
-- LE RÉFÉRENTIEL DE DESCRIPTION EST INCOMPLET : il décrit 7 des 8 services,
-- NEURO est absent — alors qu'il porte 18 % des actes. Deux réponses
-- distinctes, parce que les deux colonnes n'ont pas le même rôle :
--
--   · `categorie` et `pole` sont des AXES D'AGRÉGATION. Un service sans
--     valeur disparaîtrait de tout regroupement, et les totaux par catégorie
--     ne sommeraient plus au total de l'hôpital — une perte silencieuse.
--     Ils valent donc 'non renseigné' : le service reste compté, et la
--     lacune du référentiel se LIT dans le résultat au lieu de s'y cacher.
--
--   · `capacite_lits` est un DÉNOMINATEUR. Lui donner 0 ferait une division
--     par zéro ; lui inventer une valeur fabriquerait un taux. Elle reste
--     NULL, et l'indicateur de densité n'a simplement PAS DE LIGNE pour ce
--     service (cf. kpi_densite_actes_lit) — un ratio indéfini est absent,
--     jamais nul.
CREATE TABLE IF NOT EXISTS gold_pilotage.dim_service
(
    service_code     LowCardinality(String),
    service          String,
    categorie        LowCardinality(String),  -- 'non renseigné' si non décrit
    capacite_lits    Nullable(UInt16),        -- NULL si non décrit : c'est un dénominateur
    pole             LowCardinality(String),  -- 'non renseigné' si non décrit
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

-- Nomenclature des actes techniques. Le TARIF vit ici, dans la dimension, et
-- non sur chaque ligne de `fact_acte` : c'est une donnée de facturation, qui
-- change dans le temps sans que les actes déjà réalisés changent. Figée sur
-- le fait, une révision tarifaire obligerait à réécrire l'historique.
--
-- Nullable parce que lu en mode tolérant depuis bronze — et parce qu'un code
-- absent de la nomenclature n'écarte pas l'acte (cf. 21_silver_transform.sql).
CREATE TABLE IF NOT EXISTS gold_pilotage.dim_ccam
(
    code_ccam        LowCardinality(String),
    acte             String,
    tarif_euros      Nullable(Decimal(10, 2)),
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (code_ccam);


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
    est_sejour_index       UInt8,   -- AJUSTÉ : clos ET patient non décédé — dénominateur
    suivi_readmission_30j  UInt8,   -- AJUSTÉ : réadmis dans les 30 j — numérateur
    -- BRUT : définition de référence de l'intervenant (valeurs de référence fournies).
    -- Numérateur = tout séjour CLOS, décès compris, suivi d'une réadmission
    -- du même patient sous 30 j ; le dénominateur est TOUT silver.sejours
    -- (voir kpi_readmission_service.nb_sejours). Un patient réadmis après un
    -- décès enregistré est une incohérence de saisie — mais la référence ne
    -- l'exclut pas, et c'est elle qui fait foi sur cette définition : le
    -- taux publié au § 4 est le brut, l'ajusté ci-dessus reste le
    -- complément justifié pour qui veut exclure ce cas.
    readmission_30j_brute  UInt8,

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

    -- Nullable : NULL si le séjour porteur a une date d'admission illisible
    -- en source (0 cas sur ce dépôt — cf. tests.demontrer qualite). Une clé
    -- de PARTITION ne peut, elle, jamais être Nullable : voir plus bas
    -- comment ce NULL est traité SANS rendre la colonne elle-même non-NULL.
    date_admission   Nullable(Date),
    type_diag        LowCardinality(String),  -- principal | associe
    est_principal    UInt8,
    -- Recopié depuis silver.diagnostics : 1 si le séjour porteur est présent
    -- dans silver.sejours (donc dans fact_sejour). Un diagnostic reste dans
    -- le fait même à 0 — cf. l'en-tête de ce fichier.
    sejour_coherent  UInt8,

    age_au_sejour    Nullable(Int16),
    tranche_age      LowCardinality(String),
    sexe             LowCardinality(String),

    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
-- `coalesce(date_admission, toDate('1970-01-01'))` : le résultat d'un
-- coalesce dont le repli est NON-NULL n'est lui-même jamais NULL, donc
-- valide comme clé de partition, quelle que soit la nullité de la colonne
-- qu'il enveloppe. 1970-01-01 est une sentinelle choisie loin de toute date
-- réelle du dépôt (2026) : un diagnostic sans date n'est donc jamais
-- confondu avec un mois d'activité, et se retrouve seul dans sa partition,
-- trivial à isoler. ORDER BY ne porte plus `date_admission` (elle peut être
-- NULL) : la partition suffit au filtrage temporel usuel, et `(code_cim10,
-- stay_id)` reste unique.
PARTITION BY toYYYYMM(coalesce(date_admission, toDate('1970-01-01')))
ORDER BY (code_cim10, stay_id);


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
    -- Recopié depuis silver.monitoring : 1 si le séjour porteur est présent
    -- dans silver.sejours. `ts` n'est jamais NULL (contrairement à
    -- fact_diagnostic.date_admission) : la mesure elle-même est toujours
    -- horodatée, seul le séjour qui l'entoure peut être temporellement
    -- incohérent — d'où l'absence de tout traitement Nullable ici.
    sejour_coherent  UInt8,

    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date_mesure)
ORDER BY (service_code, date_mesure, stay_id, ts);


-- ══ FAIT 4 — ACTE ═══════════════════════════════════════════════════════
-- Grain : UN ACTE TECHNIQUE RÉALISÉ PENDANT UN SÉJOUR. 0 à n par séjour.
--
-- `service_code` EST DÉNORMALISÉ DEPUIS LE SÉJOUR, et c'est le point central
-- de ce fait. Le sujet d'évolution le formule ainsi : « le service est porté
-- par le séjour, pas par l'acte — récupérez-le sans relier deux tables de
-- faits entre elles ». Joindre `fact_acte` à `fact_sejour` ligne à ligne
-- serait un fan trap : un séjour portant trois actes verrait sa durée, son
-- mode d'admission et sa réadmission comptés trois fois. Le service est donc
-- recopié dès silver (cf. 20_silver.sql), et gold le lit tel quel.
--
-- Les indicateurs qui croisent actes ET séjours — « nombre moyen d'actes par
-- séjour » — agrègent CHAQUE FAIT SÉPARÉMENT puis joignent les deux
-- résultats sur `service_code`, une clé de DIMENSION. Une jointure entre
-- deux agrégats à la même maille ne multiplie aucune ligne ; c'est la
-- jointure ligne à ligne qui est interdite, pas la comparaison.
--
-- CE QUI N'EST PAS ICI : `patient_pseudo`. Les trois autres faits le portent
-- parce qu'un usage nommé l'exige — cohortes de patients par pathologie,
-- réadmission, origine géographique. Aucun des cinq indicateurs demandés sur
-- les actes ne dénombre de patients : ils comptent des actes, des séjours,
-- des lits et des euros. Ajouter un pseudonyme « au cas où » contredirait la
-- minimisation appliquée partout ailleurs dans cet entrepôt (§ 3.4 du
-- rapport). Le séjour reste joignable par `stay_id` si le besoin apparaît,
-- et c'est alors une décision à prendre, pas un droit déjà distribué.
CREATE TABLE IF NOT EXISTS gold_pilotage.fact_acte
(
    -- Clés
    stay_id          String,                  -- dimension dégénérée
    code_ccam        LowCardinality(String),  -- -> dim_ccam
    service_code     LowCardinality(String),  -- -> dim_service, VIA LE SÉJOUR

    -- Axes
    date_acte        Date,
    -- Nullable : NULL si l'admission du séjour porteur était illisible.
    -- Situe l'acte dans son séjour sans relire fact_sejour.
    date_admission   Nullable(Date),

    -- Qualité, recopiée de silver : 1 si le séjour porteur est cohérent.
    -- Jamais un filtre — un acte reste un acte réalisé.
    sejour_coherent  UInt8,

    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date_acte)
ORDER BY (service_code, date_acte, stay_id);


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

-- `nb_patients` compte sur TOUS les types de diagnostic (principal et
-- associé), séjours incohérents compris — c'est la définition de référence
-- de l'intervenant, et c'est ELLE qui porte le filtre k >= 5. `nb_patients_
-- principal` reste exposée à côté : c'est l'ANCIENNE définition (motif
-- d'hospitalisation seul), toujours pertinente pour qui veut la prévalence
-- au sens strict, mais qui ne détermine plus la diffusion.
CREATE TABLE IF NOT EXISTS gold_recherche.coh_prevalence
(
    code_cim10           String,
    pathologie           String,
    nb_patients          UInt32,
    nb_patients_principal UInt32,
    nb_sejours           UInt32,   -- tous types de diagnostic confondus
    _run_id              String,
    _built_at            DateTime
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
-- Le § 6 du sujet décrit gold comme des « indicateurs par usage ». Ces HUIT
-- tables sont exactement cela : `kpi_dms_service`, `kpi_urgences_jour`,
-- `kpi_readmission_service` et `kpi_alertes_jour` pour les quatre indicateurs
-- nommés au § 4 ; `kpi_occupation_jour`, `kpi_mortalite_service`,
-- `kpi_casemix_service` et `kpi_origine_service` pour sa cinquième ligne
-- ouverte (« toute autre vue d'activité pertinente », détaillée plus bas).
-- Un agrégat par indicateur ou vue, prêt à être lu sans jointure ni calcul.
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
    dms_heures       Float64,             -- même moyenne, en heures (référence de l'intervenant)
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
-- `nb_passages_urgences` retient la lecture SERVICE (`service_code =
-- 'URGENCES'`) : c'est la définition de référence de l'intervenant pour
-- l'indicateur nommé au § 4. `nb_admissions_en_urgence` (mode d'admission,
-- tous services) reste exposée à côté, en mesure complémentaire — voir
-- l'ambiguïté tranchée au § 2.10 du rapport.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_urgences_jour
(
    jour                     Date,
    nb_passages_urgences     UInt32,   -- service_code = 'URGENCES'
    nb_encore_presents       UInt32,   -- parmi eux, sans date de sortie
    duree_moy_heures         Float64,  -- durée moyenne des séjours CLOS parmi eux, en heures
    nb_admissions_en_urgence UInt32,   -- admission_mode = 'urgence', tous services : mesure complémentaire
    nb_sejours               UInt32,   -- total du jour, tous services confondus
    _run_id                  String,
    _built_at                DateTime
)
ENGINE = MergeTree ORDER BY (jour);

-- Réadmission à 30 jours. Deux définitions, l'une à côté de l'autre :
--   · BRUTE (colonnes nb_sejours / nb_readmis_30j_brut / taux_brut_pct) —
--     dénominateur = TOUS les séjours, numérateur = tout séjour clos (décès
--     compris) suivi d'une réadmission sous 30 j. C'est la définition de
--     référence de l'intervenant, celle qui fait foi au § 4.
--   · AJUSTÉE (nb_sejours_index / nb_readmis_30j / taux_pct) — dénominateur
--     restreint aux séjours clos et non décédés. Un patient réadmis après un
--     décès enregistré est une incohérence de saisie, mais la référence ne
--     l'exclut pas : cette version reste le complément justifié, pas le
--     chiffre à publier.
-- Numérateur et dénominateur sont exposés à côté de chaque taux : sans eux,
-- impossible de savoir si 12 % porte sur 50 ou 5 000 séjours, ni de
-- recomposer l'indicateur sur un autre périmètre.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_readmission_service
(
    service_code        LowCardinality(String),
    service              String,
    nb_sejours           UInt32,      -- dénominateur brut : TOUS les séjours
    nb_readmis_30j_brut  UInt32,      -- numérateur brut
    taux_brut_pct        Float64,     -- taux de RÉFÉRENCE (§4)
    nb_sejours_index     UInt32,      -- dénominateur ajusté : clos et non décédés
    nb_readmis_30j       UInt32,      -- numérateur ajusté
    taux_pct             Float64,     -- taux ajusté
    _run_id              String,
    _built_at            DateTime
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
-- ligne ouverte. Quatre vues la remplissent, chacune répondant à une
-- question qu'aucune des quatre premières ne couvre.

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

-- ORIGINE GÉOGRAPHIQUE — d'où viennent les patients de chaque service, en
-- département de résidence. C'est CETTE TABLE qui justifie de conserver
-- `region_code` jusqu'en gold : le sujet impose de « ne conserver que ce qui
-- est utile à l'usage » (minimisation, §3), et une donnée retenue sans usage
-- n'est pas défendable. Avant cette table, `region_code` traversait bronze,
-- silver et `dim_patient` sans qu'aucun indicateur ne le lise — exactement
-- le cas que la minimisation proscrit. L'attractivité territoriale du CHU
-- est la question de pilotage que cet attribut sert, au titre de « toute
-- autre vue d'activité pertinente » (§4).
--
-- GRAIN : un couple (service, département de résidence). Comme le case-mix,
-- la part est calculée SUR LE SERVICE — la question posée est « d'où
-- viennent les patients de CE service », pas le poids d'un département dans
-- l'hôpital entier. Les parts d'un même service somment donc à 100.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_origine_service
(
    service_code     LowCardinality(String),
    service          String,
    region_code      LowCardinality(String),  -- département de résidence
    nb_sejours       UInt32,
    nb_patients      UInt32,
    part_pct         Float64,   -- part des séjours du service venant de ce département
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code, region_code);


-- ── Les cinq indicateurs du sujet d'évolution ───────────────────────────
-- Même doctrine que les huit précédents : numérateur ET dénominateur sont
-- exposés à côté de chaque taux. Sans eux, impossible de savoir si un ratio
-- porte sur 20 actes ou sur 2 000, ni de le recomposer sur un autre
-- périmètre.

-- ① Activité et DMS par CATÉGORIE de service.
--
-- Pas de découpage mensuel ici, contrairement à `kpi_dms_service` : le sujet
-- demande un regroupement par catégorie, et la vue temporelle existe déjà à
-- la maille du service. Les deux tables restent réconciliables — la somme des
-- `nb_sejours_clos` par catégorie égale celle de kpi_dms_service.
--
-- Les séjours EN COURS sont exclus de la DMS (ils n'ont pas de durée) mais
-- comptés dans `nb_sejours` : l'activité d'un service, ce sont tous ses
-- séjours, pas seulement ceux qui sont clos.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_activite_categorie
(
    categorie        LowCardinality(String),
    nb_sejours       UInt32,   -- tous séjours, en cours compris
    nb_sejours_clos  UInt32,   -- dénominateur de la DMS
    dms_jours        Float64,
    dms_heures       Float64,
    mediane_jours    Float64,
    p90_jours        Float64,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (categorie);

-- ② Nombre d'actes par SERVICE, et nombre moyen d'actes par séjour.
--
-- « Actes par séjour » est AMBIGU, et l'ambiguïté est tranchée en exposant
-- les deux lectures plutôt qu'en en choisissant une en silence :
--   · `actes_par_sejour`            — sur TOUS les séjours du service. C'est
--     l'intensité du plateau technique rapportée à l'activité totale.
--   · `actes_par_sejour_avec_acte`  — sur les seuls séjours qui ont reçu au
--     moins un acte. C'est l'intensité de la prise en charge technique quand
--     elle a lieu.
-- Un service où un séjour sur dix reçoit dix actes et un service où tous les
-- séjours en reçoivent un donnent la MÊME première mesure et deux secondes
-- très différentes.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_actes_service
(
    service_code               LowCardinality(String),
    service                    String,
    categorie                  LowCardinality(String),
    nb_actes                   UInt32,
    nb_sejours                 UInt32,   -- dénominateur 1 : tous les séjours du service
    nb_sejours_avec_acte       UInt32,   -- dénominateur 2 : ceux qui ont reçu un acte
    actes_par_sejour           Float64,
    actes_par_sejour_avec_acte Float64,
    _run_id                    String,
    _built_at                  DateTime
)
ENGINE = MergeTree ORDER BY (service_code);

-- ③ Répartition des actes par TYPE d'acte.
--
-- `part_pct` est la part de ce code dans l'ensemble des actes : elle rend la
-- table lisible sans avoir à en resommer le total. `nb_sejours_concernes`
-- distingue un acte fréquent RÉPÉTÉ sur peu de séjours d'un acte fréquent
-- RÉPANDU sur beaucoup.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_actes_type
(
    code_ccam            LowCardinality(String),
    acte                 String,
    nb_actes             UInt32,
    part_pct             Float64,
    nb_sejours_concernes UInt32,
    tarif_euros          Nullable(Decimal(10, 2)),
    _run_id              String,
    _built_at            DateTime
)
ENGINE = MergeTree ORDER BY (code_ccam);

-- ④ Densité d'actes par LIT — intensité du plateau technique.
--
-- CETTE TABLE N'A PAS DE LIGNE POUR UN SERVICE SANS CAPACITÉ CONNUE. C'est
-- la conséquence directe du référentiel incomplet (cf. dim_service) : NEURO
-- porte 1 471 actes, mais son nombre de lits est inconnu. Publier 0, ou
-- l'omettre du dénominateur en gardant le numérateur, produirait un chiffre
-- faux. Un ratio indéfini est ABSENT.
--
-- La lacune reste donc visible par différence : la somme des `nb_actes` de
-- cette table est INFÉRIEURE au total de `kpi_actes_service`, et l'écart est
-- exactement l'activité des services non décrits. C'est ce que vérifie
-- `tests.verifier indicateurs`.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_densite_actes_lit
(
    service_code     LowCardinality(String),
    service          String,
    categorie        LowCardinality(String),
    capacite_lits    UInt16,    -- NON nullable : une ligne n'existe que si la capacité est connue
    nb_actes         UInt32,
    actes_par_lit    Float64,
    _run_id          String,
    _built_at        DateTime
)
ENGINE = MergeTree ORDER BY (service_code);

-- ⑤ Montant facturé par service (T2A).
--
-- Le tarif vient de `dim_ccam`, jamais du fait. Un acte dont le code est
-- absent de la nomenclature n'a PAS de tarif : il est compté dans
-- `nb_actes_sans_tarif` et exclu du montant. Sans cette colonne, un total
-- sous-évalué serait indiscernable d'une activité plus faible.
CREATE TABLE IF NOT EXISTS gold_pilotage.kpi_facturation_service
(
    service_code         LowCardinality(String),
    service              String,
    categorie            LowCardinality(String),
    nb_actes             UInt32,
    nb_actes_sans_tarif  UInt32,   -- code absent de la nomenclature : hors montant
    montant_euros        Decimal(14, 2),
    montant_moyen_euros  Float64,  -- rapporté aux seuls actes tarifés
    _run_id              String,
    _built_at            DateTime
)
ENGINE = MergeTree ORDER BY (service_code);
