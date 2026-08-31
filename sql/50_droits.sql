-- ─────────────────────────────────────────────────────────────────────────
-- CLOISONNEMENT DES DROITS — contrainte RGPD du §5.
--
-- « Pilotage et recherche ne voient pas les mêmes données
--   -> droits d'accès distincts. »
--
-- Deux comptes, chacun avec le droit de lecture sur UNE SEULE base gold.
-- Ni l'un ni l'autre n'a accès à bronze ni à silver : ils ne peuvent donc
-- pas remonter aux données de détail, ni recomposer une cohorte sous le
-- seuil des 5 patients à partir des lignes brutes.
--
-- Le refus est prononcé par le MOTEUR. Ce n'est pas une convention
-- applicative que l'on pourrait contourner en écrivant une autre requête.
-- ─────────────────────────────────────────────────────────────────────────

CREATE USER IF NOT EXISTS eds_pilotage
    IDENTIFIED WITH sha256_password BY '{mdp_pilotage}';

CREATE USER IF NOT EXISTS eds_recherche
    IDENTIFIED WITH sha256_password BY '{mdp_recherche}';

-- Le compte pilotage : les indicateurs hospitaliers, rien d'autre.
GRANT SELECT ON gold_pilotage.* TO eds_pilotage;

-- Le compte recherche : les cohortes agrégées, rien d'autre.
-- Rappel : cette base ne contient ni birth_year, ni cohorte de moins de
-- 5 patients — le filtrage a eu lieu à l'écriture.
GRANT SELECT ON gold_recherche.* TO eds_recherche;


-- ── Compte d'exploitation ───────────────────────────────────────────────
-- L'administrateur a besoin d'investiguer sur les couches techniques —
-- retrouver la ligne d'origine d'un incident, préparer une piste d'audit,
-- instruire une demande d'effacement.
--
-- Il n'utilise PAS pour cela le compte du pipeline : celui-ci peut créer et
-- supprimer des bases, ce qui n'a pas sa place derrière une interface web.
-- Un compte distinct, en LECTURE SEULE sur les couches techniques, applique
-- le principe de moindre privilège : depuis Metabase, aucune écriture n'est
-- possible, quelle que soit la requête saisie.
CREATE USER IF NOT EXISTS eds_exploitation
    IDENTIFIED WITH sha256_password BY '{mdp_exploitation}';

GRANT SELECT ON bronze.* TO eds_exploitation;
GRANT SELECT ON silver.* TO eds_exploitation;
GRANT SELECT ON ops.*    TO eds_exploitation;
