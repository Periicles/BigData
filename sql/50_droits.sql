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

-- ── Compte de pilotage : droits AU NIVEAU COLONNE ───────────────────────
--
-- Un GRANT sur la base entière donnerait accès à `patient_pseudo` et au
-- grain du séjour, très au-delà du besoin : la direction consulte des
-- indicateurs d'activité, elle n'a jamais à désigner un patient.
--
-- Les droits sont donc limités aux seules colonnes que les tableaux de bord
-- utilisent. Ni `patient_pseudo`, ni `stay_id`, ni les horodatages précis,
-- ni les constantes brutes n'y figurent. `dim_patient` et `fact_diagnostic`
-- ne sont pas accordées du tout.
--
-- Conséquence : même si ce compte parvenait à composer une requête, il ne
-- pourrait pas dénombrer de patients ni relier deux séjours entre eux.
GRANT SELECT(
    service_code, date_admission, tranche_age,
    duree_jours, est_en_cours, est_urgence,
    est_sejour_index, suivi_readmission_30j
) ON gold_pilotage.fact_sejour TO eds_pilotage;

GRANT SELECT(
    service_code, date_mesure,
    alerte_fc, alerte_spo2, alerte_temp, en_alerte
) ON gold_pilotage.fact_releve TO eds_pilotage;

GRANT SELECT(service_code, service) ON gold_pilotage.dim_service TO eds_pilotage;

-- ── Compte recherche ────────────────────────────────────────────────────
-- Les deux tables de cohortes sont déjà agrégées, filtrées à k >= 5, et ne
-- contiennent aucun pseudonyme. Les droits restent néanmoins limités aux
-- colonnes exposées, par cohérence.
GRANT SELECT(code_cim10, pathologie, nb_patients, nb_sejours)
    ON gold_recherche.coh_prevalence TO eds_recherche;

GRANT SELECT(code_cim10, pathologie, tranche_age, sexe, nb_patients)
    ON gold_recherche.coh_description TO eds_recherche;


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
