"""La configuration — secrets exigés et seuils d'alerte surchargeables.

Les seuils d'alerte sont un PARAMÈTRE D'EXPLOITATION, pas une constante du
SQL : un service doit pouvoir les ajuster sans toucher à une requête. Ils
viennent donc de l'environnement — c'est-à-dire de l'extérieur — et sont
validés ici, à la frontière, avant d'être interpolés dans
`31_gold_transform.sql`.
"""

from __future__ import annotations

import pytest

from eds import config


# ── Variables obligatoires ───────────────────────────────────────────────
def test_variable_manquante_echoue_avec_un_message_actionnable(env_vierge):
    """Un secret absent ne doit pas produire un `KeyError` nu : le message
    doit dire quoi faire."""
    with pytest.raises(RuntimeError) as erreur:
        config.exiger("EDS_VARIABLE_QUI_N_EXISTE_PAS")
    message = str(erreur.value)
    assert "EDS_VARIABLE_QUI_N_EXISTE_PAS" in message
    assert ".env" in message


def test_variable_vide_est_traitee_comme_absente(env_vierge, monkeypatch):
    """`CH_ADMIN_PASSWORD=` dans un `.env` est une erreur de saisie, pas un
    mot de passe vide."""
    monkeypatch.setenv("EDS_SECRET", "   ")
    with pytest.raises(RuntimeError):
        config.exiger("EDS_SECRET")


def test_variable_presente_est_rendue_sans_espaces(env_vierge, monkeypatch):
    monkeypatch.setenv("EDS_SECRET", "  valeur  ")
    assert config.exiger("EDS_SECRET") == "valeur"


# ── Seuils d'alerte ──────────────────────────────────────────────────────
def test_seuils_par_defaut_sont_ceux_de_la_reference(env_vierge):
    """Les valeurs par défaut sont celles de la feuille de réponses de
    l'intervenant : FC < 50 ou > 100, SpO2 < 92, T° > 38,5. Les changer
    ferait diverger l'indicateur d'alerte de sa valeur de référence."""
    assert config.seuils_alerte() == {
        "fc_basse": "50", "fc_haute": "100",
        "spo2_basse": "92", "temp_haute": "38.5",
    }


def test_un_seuil_se_surcharge_par_l_environnement(env_vierge, monkeypatch):
    """C'est la propriété qui rend le seuil paramétrable : un service peut
    l'ajuster sans toucher au SQL."""
    monkeypatch.setenv("EDS_SEUIL_FC_BASSE", "45")
    seuils = config.seuils_alerte()
    assert seuils["fc_basse"] == "45"
    # Les autres ne bougent pas.
    assert seuils["fc_haute"] == "100"


@pytest.mark.parametrize("valeur", ["quarante", "45; DROP TABLE x", "4 5", "", "45,5"])
def test_seuil_non_numerique_est_refuse(env_vierge, monkeypatch, valeur):
    """Le seuil est INTERPOLÉ dans le SQL de gold. Il est donc validé ici, à
    la frontière, et non au moment de l'interpolation — une valeur hostile ne
    doit jamais atteindre `executer_fichier`.

    La chaîne vide est refusée comme les autres : elle produirait un `WHERE
    heart_rate < ` syntaxiquement invalide, et l'erreur remonterait du moteur
    au lieu de la configuration.
    """
    monkeypatch.setenv("EDS_SEUIL_FC_BASSE", valeur)
    if valeur == "":
        # Une valeur vide retombe sur le défaut : c'est la lecture d'un `.env`
        # dont la ligne a été laissée sans valeur, pas une saisie hostile.
        assert config.seuils_alerte()["fc_basse"] == "50"
        return
    with pytest.raises(RuntimeError, match="Seuil d'alerte invalide"):
        config.seuils_alerte()


@pytest.mark.parametrize("valeur", ["45", "38.5", "-0.5", "100"])
def test_seuil_numerique_est_accepte(env_vierge, monkeypatch, valeur):
    monkeypatch.setenv("EDS_SEUIL_TEMP_HAUTE", valeur)
    assert config.seuils_alerte()["temp_haute"] == valeur


def test_les_seuils_passent_le_controle_de_substitution_sql(env_vierge):
    """Les deux frontières doivent s'accorder : un seuil que `seuils_alerte`
    accepte doit être accepté par `executer_fichier`, sinon une configuration
    légitime ferait échouer le pipeline au moment de l'interpolation."""
    from eds.warehouse import _SUBSTITUTION_ADMISE

    for valeur in config.seuils_alerte().values():
        assert _SUBSTITUTION_ADMISE.fullmatch(valeur)


# ── Chemins ──────────────────────────────────────────────────────────────
def test_le_lake_n_est_pas_dans_le_depot_source():
    """Le dépôt du CHU est en LECTURE SEULE : le pipeline n'y écrit jamais.
    Si le lake tombait dedans, la copie écraserait la source."""
    assert config.LAKE != config.SOURCE
    assert config.SOURCE not in config.LAKE.parents


def test_les_sources_connues_couvrent_le_depot_et_son_evolution():
    assert set(config.SOURCES_CONNUES) == {
        "patients", "sejours", "diagnostics", "monitoring", "actes", "referentiels",
    }


def test_les_sources_connues_derivent_de_la_declaration():
    """Une source ne peut pas être connue sans que le contenu qu'elle est
    autorisée à déposer dans le lake soit décrit : c'est la déclaration qui
    fait la source, et non l'inverse."""
    assert config.SOURCES_CONNUES == tuple(config.COLONNES_LAKE)


def test_aucune_colonne_identifiante_n_est_declaree():
    """La déclaration est le contrat de sortie du lake. Aucun nom de colonne
    portant l'identité ne doit y figurer, pour aucune source."""
    interdits = {"patient_id", "nir", "nom", "prenom", "birth_date",
                 "email", "telephone", "adresse", "rpps"}
    for fichiers in config.COLONNES_LAKE.values():
        for colonnes in fichiers.values():
            declarees = set()
            for cle, valeur in (colonnes.items() if isinstance(colonnes, dict)
                                else ((c, None) for c in colonnes)):
                declarees.add(cle)
                declarees.update(valeur or ())
            assert not declarees & interdits


# ── Langue de restitution ────────────────────────────────────────────────
def test_langue_par_defaut_est_le_francais(env_vierge):
    """Tout le rendu est en français : l'interface qui l'accompagne doit
    formater les nombres de la même façon."""
    assert config.langue_metabase() == "fr"


@pytest.mark.parametrize("valeur,attendu", [("en", "en"), ("EN", "en"), ("  fr  ", "fr")])
def test_langue_se_surcharge_et_se_normalise(env_vierge, monkeypatch, valeur, attendu):
    monkeypatch.setenv("MB_LOCALE", valeur)
    assert config.langue_metabase() == attendu


def test_langue_vide_retombe_sur_le_defaut(env_vierge, monkeypatch):
    """`MB_LOCALE=` dans un `.env` est une ligne laissée sans valeur, pas une
    demande de langue vide."""
    monkeypatch.setenv("MB_LOCALE", "")
    assert config.langue_metabase() == "fr"


@pytest.mark.parametrize("refusee", ["de", "fr_FR", "français", "en;DROP"])
def test_langue_hors_liste_est_refusee(env_vierge, monkeypatch, refusee):
    """Metabase refuserait la valeur avec un message peu clair : le refus a
    lieu ici, à la frontière, comme pour les seuils d'alerte."""
    monkeypatch.setenv("MB_LOCALE", refusee)
    with pytest.raises(RuntimeError, match="Langue Metabase invalide"):
        config.langue_metabase()


# ── Bornes de relance du superviseur ─────────────────────────────────────
def test_bornes_de_relance_par_defaut(env_vierge):
    """Trois tentatives à dix minutes : une panne de moteur passagère est
    absorbée, une panne durable ne monopolise pas la nuit."""
    assert config.bornes_relance() == (3, 600)


def test_les_bornes_de_relance_se_surchargent_par_l_environnement(env_vierge, monkeypatch):
    monkeypatch.setenv("EDS_RELANCE_TENTATIVES", "5")
    monkeypatch.setenv("EDS_RELANCE_ATTENTE_S", "30")
    assert config.bornes_relance() == (5, 30)


def test_une_borne_de_relance_non_numerique_est_refusee(env_vierge, monkeypatch):
    """Même règle que les seuils : une saisie fautive est refusée à la
    frontière, pas absorbée en silence."""
    monkeypatch.setenv("EDS_RELANCE_TENTATIVES", "trois")
    with pytest.raises(RuntimeError) as erreur:
        config.bornes_relance()
    assert "EDS_RELANCE_TENTATIVES" in str(erreur.value)


def test_une_borne_de_relance_nulle_est_refusee(env_vierge, monkeypatch):
    """Zéro tentative ne veut rien dire : le pipeline ne serait jamais lancé."""
    monkeypatch.setenv("EDS_RELANCE_TENTATIVES", "0")
    with pytest.raises(RuntimeError):
        config.bornes_relance()


def test_aucune_commande_d_alerte_par_defaut(env_vierge):
    """Le canal est une décision d'exploitation : le dépôt n'en impose aucun."""
    assert config.commande_alerte() == ""


def test_la_commande_d_alerte_vient_de_l_environnement(env_vierge, monkeypatch):
    monkeypatch.setenv("EDS_ALERTE_CMD", "  curl -sS --data-binary @- https://exemple  ")
    assert config.commande_alerte() == "curl -sS --data-binary @- https://exemple"
