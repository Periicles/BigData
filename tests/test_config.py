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
