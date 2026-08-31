-- ─────────────────────────────────────────────────────────────────────────
-- Journal d'exécution du pipeline — critère TRAÇABILITÉ du §5.
--
-- « Savoir d'où vient chaque donnée et quand elle a été traitée. »
--
-- Une ligne par étape et par exécution. Couplée aux colonnes _run_id des
-- tables bronze et silver, elle permet de répondre en SQL à :
--   « cette ligne, quel run l'a produite, quand, et en combien de temps ? »
--
-- Aucune donnée de santé ni identifiant patient n'entre dans ce journal :
-- il ne contient que des métadonnées d'exécution.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ops.executions
(
    run_id       String,
    etape        LowCardinality(String),   -- lake | bronze | silver | gold
    jour         Nullable(Date),           -- NULL pour les étapes non journalières
    statut       Enum8('succes' = 1, 'echec' = 2),
    lignes       UInt64,
    duree_s      Float64,
    message      String,                   -- vide si succès, cause si échec
    demarre_a    DateTime,
    termine_a    DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(demarre_a)
ORDER BY (demarre_a, run_id, etape);
