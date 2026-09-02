-- ─────────────────────────────────────────────────────────────────────────
-- SILVER — nettoyé, dédupliqué, cohérent, enrichi.
--
-- Recalculé intégralement à chaque exécution depuis bronze. À ce volume
-- (6 800 séjours, 42 000 relevés) c'est instantané, et cela garantit qu'un
-- rejeu produit exactement le même état. La limite de ce choix est documentée
-- dans le rapport : il ne tiendrait pas sur plusieurs années d'historique.
--
-- Créées si absentes, JAMAIS détruites ici : l'étape de schéma s'exécute au
-- début de chaque run, y compris ceux qui échouent ensuite. Un DROP y
-- laisserait l'entrepôt vide après un incident, alors que la reprise doit le
-- laisser cohérent — c'est ce que vérifie `tests.demontrer reprise`.
--
-- Silver étant dérivé, faire évoluer son schéma reste sans coût : supprimer
-- les tables une fois suffit, l'exécution suivante les reconstruit depuis
-- bronze, sans perte ni migration.
--
-- CE QUI N'EST PAS ICI, ET POURQUOI. La frontière tient en une règle :
--   · une règle de VALIDITÉ de la donnée, fournie par le sujet, est ici —
--     plages physiologiques, cohérence temporelle, déduplication ;
--   · une règle MÉTIER, que le sujet ne fournit pas et qui se paramètre,
--     est en gold — seuils d'alerte clinique, âge à l'événement.
-- D'où l'absence, dans `monitoring`, des drapeaux d'alerte (seuils
-- configurables, cf. 31_gold_transform.sql) et, dans `sejours`, de l'âge au
-- séjour : c'est le croisement d'un attribut de `dim_patient` et d'un axe du
-- fait, il se calcule à la construction du fait, contre la dimension.
--
-- POURQUOI `diagnostics` ET `monitoring` PORTENT `sejour_coherent`, ET
-- SURTOUT POURQUOI CE N'EST PAS UN FILTRE. La cohérence temporelle d'un
-- séjour (sortie postérieure à l'admission) est une règle de VALIDITÉ... du
-- SÉJOUR. Un diagnostic posé et un relevé pris pendant ce séjour sont des
-- faits médicaux réels, indépendants de la qualité de saisie des deux dates
-- qui l'encadrent : un code CIM-10 correctement codé reste un code
-- correctement codé même si `discharge_ts` a été inversé avec
-- `admission_ts` à la ressaisie. Les écarter reviendrait à faire porter à
-- une donnée clinique valide l'anomalie d'une autre colonne, sur une autre
-- table. Seule l'ABSENCE de patient identifié (séjour introuvable, ou
-- rattaché à un pseudonyme vide) écarte réellement un diagnostic ou un
-- relevé — cf. l'en-tête MONITORING / DIAGNOSTICS de 21_silver_transform.sql
-- pour le détail des deux motifs. Le doute sur la cohérence temporelle est
-- donc SIGNALÉ (le drapeau), jamais SILENCIEUSEMENT PERDU (un filtre).
--
-- Conséquence utile : `silver.sejours` ne dépend plus de `silver.patients`,
-- les quatre tables se construisent indépendamment les unes des autres —
-- `diagnostics` et `monitoring` restent l'exception assumée : ils lisent
-- `bronze.sejours` (jamais `silver.sejours`) pour s'enrichir du patient, du
-- service et de la date d'admission porteurs, afin que gold n'ait plus
-- jamais besoin de lire `silver.sejours` pour ces deux faits.
--
-- Les lignes écartées ne sont pas ici non plus : elles vivent dans la base
-- `quarantaine`, qui a son propre cycle de vie (cf. 15_quarantaine.sql).
--
-- TRAÇABILITÉ — chaque ligne porte le jour de dépôt et le fichier dont elle
-- provient, recopiés depuis bronze. On répond donc à « d'où vient cette
-- ligne ? » sans jointure, y compris pour les lignes écartées.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.patients (
    patient_pseudo String,
    -- NULL si la date de naissance était illisible en source : le patient
    -- est conservé (attribut descriptif), la ligne est tracée en
    -- quarantaine (motif 'date_naissance_illisible'). Un pseudonyme VIDE, en
    -- revanche, n'entre jamais ici : motif 'patient_manquant', écarté.
    birth_year Nullable(UInt16),
    sex LowCardinality(String),
    region_code LowCardinality(String),
    -- Snapshot cumulatif : la ligne est une réduction de plusieurs lignes
    -- bronze. Sa provenance est celle de la VERSION RETENUE, d'où le suffixe.
    _jour_depot_retenu Date,
    _fichier_source_retenu String,
    _run_id String,
    _built_at DateTime
) ENGINE = MergeTree
ORDER BY
    (patient_pseudo);

CREATE TABLE IF NOT EXISTS silver.sejours (
    stay_id String,
    patient_pseudo String,
    service_code LowCardinality(String),
    service_label String,
    -- enrichi depuis le référentiel
    admission_ts DateTime,
    discharge_ts Nullable(DateTime),
    admission_mode LowCardinality(String),
    discharge_mode LowCardinality(String),
    -- '' normalisé en 'inconnu'
    duree_jours Nullable(Float64),
    -- NULL si séjour en cours
    est_en_cours UInt8,
    _jour_depot Date,
    _fichier_source String,
    _run_id String,
    _built_at DateTime
) ENGINE = MergeTree
ORDER BY
    (stay_id);

CREATE TABLE IF NOT EXISTS silver.diagnostics (
    stay_id String,
    code_cim10 LowCardinality(String),
    type_diag LowCardinality(String),
    libelle String,
    -- Attributs du séjour PORTEUR, lus dans bronze.sejours (version retenue
    -- après déduplication) et NON dans silver.sejours : voir l'en-tête de ce
    -- fichier pour pourquoi la validité d'un diagnostic ne dépend pas de la
    -- cohérence temporelle du séjour.
    patient_pseudo String,
    service_code LowCardinality(String),
    -- Nullable : NULL si la date d'admission du séjour porteur était
    -- illisible en source (0 cas sur ce dépôt, cf. tests.demontrer qualite).
    admission_ts Nullable(DateTime),
    -- 1 si le séjour porteur est présent dans silver.sejours (cohérent), 0
    -- sinon. Un diagnostic reste conservé dans les deux cas.
    sejour_coherent UInt8,
    -- enrichi depuis le référentiel
    _jour_depot Date,
    _fichier_source String,
    _run_id String,
    _built_at DateTime
) ENGINE = MergeTree
ORDER BY
    (stay_id, code_cim10);

-- Les mesures validées, et rien d'autre : ce qui sort d'ici est
-- physiologiquement plausible. Qualifier un relevé d'« en alerte » est une
-- décision clinique paramétrable, elle appartient à gold.
CREATE TABLE IF NOT EXISTS silver.monitoring (
    stay_id String,
    ts DateTime,
    heart_rate Int16,
    spo2 Int16,
    temp_c Decimal(4, 1),
    -- Mêmes attributs du séjour porteur, et même provenance (bronze.sejours,
    -- version retenue) que dans `diagnostics` ci-dessus — voir l'en-tête.
    patient_pseudo String,
    service_code LowCardinality(String),
    admission_ts Nullable(DateTime),
    sejour_coherent UInt8,
    -- _jour_depot est le jour du FICHIER, ts celui de la MESURE. Les deux
    -- diffèrent ici : le monitoring déborde de son jour de dépôt.
    _jour_depot Date,
    _fichier_source String,
    _run_id String,
    _built_at DateTime
) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) -- la source volumineuse : partitionnée sur la
ORDER BY
    (stay_id, ts);

-- date de la MESURE, jamais du dépôt
