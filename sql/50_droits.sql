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
