-- ─────────────────────────────────────────────────────────────────────────
-- QUARANTAINE — registre des lignes écartées par les contrôles qualité.
--
-- POURQUOI UNE BASE À PART, et non `silver.rejets`.
--
-- 1. Le contrat de la couche. Silver signifie « nettoyé, cohérent ». Y loger
--    la table des lignes sales brouille exactement ce que la couche promet :
--    un `GRANT SELECT ON silver.*` livrait le registre avec les données
--    propres, et un consommateur découvrant le schéma y voyait une table qui
--    n'appartient pas au modèle.
--
-- 2. Le cycle de vie. Ces lignes portent `stay_id` et, dans `detail`, les
--    valeurs brutes fautives : c'est de la donnée de santé pseudonymisée,
--    mais dont la durée de conservation n'est pas celle de l'entrepôt. On
--    purge une quarantaine quand l'incident qualité est instruit, pas quand
--    la donnée expire. Une base distincte permet de lui appliquer sa propre
--    rétention et ses propres droits.
--
-- Elle rend les exclusions comptables et interrogeables au lieu de
-- silencieuses — critère « qualité des traitements » du sujet — et porte la
-- même traçabilité que les autres couches : on sait de quel fichier venait
-- chaque ligne écartée, et quelle exécution l'a écartée.
--
-- L'équation de conservation devient :
--     bronze = silver + quarantaine(action = 'ecarte')
--
-- CE QUI ENTRE ICI : toute ligne qui viole un contrôle qualité du §3 du sujet
-- — doublon patient, cohérence temporelle, plage physiologique, format de
-- date, sexe non normalisé. Pas les décisions métier documentées : la
-- normalisation de `discharge_mode` vide en 'inconnu' est un choix de
-- modélisation, pas une anomalie détectée, et n'a rien à faire dans un
-- registre d'incidents qualité.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS quarantaine.rejets (
    source LowCardinality(String),
    cle String,
    motif LowCardinality(String),
    -- Deux issues possibles pour une ligne fautive, et il faut les distinguer :
    --   'ecarte'  la ligne ne passe pas en silver — elle entre dans
    --             l'équation  bronze = silver + quarantaine('ecarte')
    --   'corrige' la ligne passe, une valeur inutilisable ayant été remplacée
    --             (sexe hors M/F -> 'inconnu'). Elle est signalée, pas soustraite.
    -- Sans cette colonne, une correction fausserait l'équation de conservation
    -- ou resterait invisible ; les deux sont inacceptables.
    action LowCardinality(String),
    detail String,
    _jour_depot Date,
    _fichier_source String,
    _run_id String,
    _rejected_at DateTime
) ENGINE = MergeTree
ORDER BY
    (source, motif, cle);
