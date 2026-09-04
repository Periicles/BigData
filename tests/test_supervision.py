"""Le superviseur — ce que `cron` seul ne sait pas faire.

`cron` déclenche, rien de plus : il ne sait pas qu'une exécution précédente
court encore, il ne relance pas ce qui a échoué, et il ne prévient personne.
Ces trois manques sont comblés ici, et c'est ce fichier qui le prouve — hors
ligne, sans Docker, en injectant un faux pipeline à la place d'`eds.run`.
"""

from __future__ import annotations

import os

import pytest

from eds import supervision


@pytest.fixture
def logs(tmp_path, monkeypatch, env_vierge):
    """Un répertoire de logs jetable, à la place de `logs/`.

    Dépend d'`env_vierge` : les bornes de relance sont surchargeables par
    l'environnement, donc un `.env` de poste ferait autrement passer ou
    échouer les tests des valeurs par défaut."""
    racine = tmp_path / "logs"
    racine.mkdir()
    monkeypatch.setattr(supervision, "VERROU", racine / ".verrou")
    monkeypatch.setattr(supervision, "ALERTE", racine / "ALERTE.txt")
    monkeypatch.setattr(supervision, "JOURNAL", racine / "pipeline.log")
    return racine


def lanceur(*codes):
    """Un faux pipeline qui rend les codes de sortie donnés, dans l'ordre."""
    codes = list(codes)
    appels = []

    def lancer(argv):
        appels.append(argv)
        return codes.pop(0) if codes else 0

    lancer.appels = appels
    return lancer


# ── Verrou : deux exécutions ne doivent jamais se recouvrir ──────────────
def test_une_execution_deja_en_cours_ne_relance_rien(logs):
    """Le cas réel : un `--tout` manuel à 03h09 et le cron de 03h10. Sans
    verrou, les deux écrivent bronze en même temps."""
    supervision.VERROU.write_text(str(os.getpid()), encoding="utf-8")
    lancer = lanceur(0)

    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 0
    assert lancer.appels == []


def test_un_verrou_dont_le_processus_est_mort_est_repris(logs):
    """Une machine qui redémarre en pleine exécution laisse un verrou
    orphelin. Le refuser bloquerait le pipeline jusqu'à intervention."""
    supervision.VERROU.write_text("999999", encoding="utf-8")
    lancer = lanceur(0)

    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 0
    assert lancer.appels == [[]]


def test_un_verrou_illisible_est_repris(logs):
    """Un verrou tronqué par un disque plein ne doit pas être un blocage
    définitif : sans PID exploitable, il ne prouve rien."""
    supervision.VERROU.write_text("", encoding="utf-8")
    lancer = lanceur(0)

    supervision.superviser([], lancer=lancer, dormir=lambda _: None)
    assert lancer.appels == [[]]


def test_le_verrou_est_rendu_meme_apres_un_echec(logs):
    """Sinon le premier échec bloque toutes les nuits suivantes."""
    supervision.superviser([], lancer=lanceur(1), dormir=lambda _: None)
    assert not supervision.VERROU.exists()


# ── Relance : seuls les échecs que relancer peut résoudre ────────────────
def test_une_execution_reussie_ne_tourne_qu_une_fois(logs):
    lancer = lanceur(0)
    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 0
    assert len(lancer.appels) == 1


def test_une_erreur_metier_n_est_jamais_relancee(logs):
    """Code 1 : jour absent du dépôt, SQL invalide. Relancer à l'identique
    ne ferait que perdre du temps et bruiter le journal."""
    lancer = lanceur(1, 0)
    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 1
    assert len(lancer.appels) == 1


def test_un_echec_inattendu_est_relance_jusqu_a_reussir(logs):
    """Code 2 : ClickHouse arrêté, disque momentanément plein. La cause peut
    disparaître d'elle-même."""
    lancer = lanceur(2, 0)
    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 0
    assert len(lancer.appels) == 2


def test_les_relances_sont_bornees(logs, monkeypatch):
    """Sans borne, une panne durable relancerait le pipeline jusqu'au matin."""
    monkeypatch.setenv("EDS_RELANCE_TENTATIVES", "3")
    lancer = lanceur(2, 2, 2, 2, 2)
    assert supervision.superviser([], lancer=lancer, dormir=lambda _: None) == 2
    assert len(lancer.appels) == 3


def test_l_attente_entre_deux_relances_est_celle_configuree(logs, monkeypatch):
    """C'est cette surcharge qui rend la relance testable en une seconde —
    et ajustable en exploitation sans toucher au code."""
    monkeypatch.setenv("EDS_RELANCE_ATTENTE_S", "42")
    attentes = []
    supervision.superviser([], lancer=lanceur(2, 0), dormir=attentes.append)
    assert attentes == [42]


# ── Alerte : un échec ne doit pas attendre qu'on aille lire un log ───────
def test_un_echec_ecrit_une_alerte_avec_son_action_de_reprise(logs):
    supervision.superviser([], lancer=lanceur(1), dormir=lambda _: None)

    alerte = supervision.ALERTE.read_text(encoding="utf-8")
    assert "1" in alerte
    assert "eds.run" in alerte  # la commande à relancer, pas seulement le constat


def test_l_alerte_porte_le_run_id_de_l_execution_fautive(logs):
    """Sans lui, l'alerte ne se joint pas à `ops.executions` : on saurait
    qu'une nuit a échoué, pas laquelle interroger."""
    supervision.JOURNAL.write_text(
        '{"horodatage": "2026-09-04T03:10:00", "message": "démarré"}\n'
        '{"horodatage": "2026-09-04T03:10:02", "message": "étape en échec",'
        ' "run_id": "e53eaf958a6c"}\n',
        encoding="utf-8",
    )
    supervision.superviser([], lancer=lanceur(2, 2, 2), dormir=lambda _: None)

    assert "e53eaf958a6c" in supervision.ALERTE.read_text(encoding="utf-8")


def test_une_nuit_reussie_eteint_l_alerte_de_la_veille(logs):
    """L'alerte est un état, pas un historique : le journal garde la trace."""
    supervision.ALERTE.write_text("échec de la veille", encoding="utf-8")

    supervision.superviser([], lancer=lanceur(0), dormir=lambda _: None)
    assert not supervision.ALERTE.exists()


def test_la_commande_d_alerte_recoit_le_resume_sur_son_entree(logs, monkeypatch, tmp_path):
    """Le canal reste hors du dépôt : webhook, courriel ou notification,
    c'est au site de le décider."""
    recu = tmp_path / "recu.txt"
    monkeypatch.setenv("EDS_ALERTE_CMD", f"cat > {recu}")

    supervision.superviser([], lancer=lanceur(1), dormir=lambda _: None)
    assert "1" in recu.read_text(encoding="utf-8")


def test_aucune_commande_d_alerte_n_est_appelee_quand_tout_va_bien(logs, monkeypatch, tmp_path):
    temoin = tmp_path / "temoin.txt"
    monkeypatch.setenv("EDS_ALERTE_CMD", f"touch {temoin}")

    supervision.superviser([], lancer=lanceur(0), dormir=lambda _: None)
    assert not temoin.exists()


def test_une_commande_d_alerte_fautive_ne_masque_pas_l_echec(logs, monkeypatch):
    """Même règle que le journal ClickHouse : le moyen de prévenir ne doit
    jamais devenir la cause d'un échec, ni changer le code de sortie."""
    monkeypatch.setenv("EDS_ALERTE_CMD", "commande-qui-n-existe-pas")

    code = supervision.superviser([], lancer=lanceur(1), dormir=lambda _: None)
    assert code == 1
    assert supervision.ALERTE.exists()


# ── Restitution de l'état : l'alerte doit se voir sans ouvrir un fichier ─
def test_aucun_bandeau_quand_aucune_alerte_n_est_en_cours(logs):
    assert supervision.bandeau_alerte() == ""


def test_le_bandeau_reprend_l_alerte_en_cours(logs):
    """`--etat` est la commande qu'on tape en premier : c'est là que l'échec
    de la nuit doit sauter aux yeux."""
    supervision.ALERTE.write_text("ÉCHEC DU PIPELINE EDS — run_id : abc123\n", encoding="utf-8")

    bandeau = supervision.bandeau_alerte()
    assert "abc123" in bandeau
    assert "ALERTE" in bandeau


def test_le_point_d_entree_transmet_ses_options_au_pipeline(logs, monkeypatch):
    """`python -m eds.supervision --tout` doit valoir `eds.run --tout` : le
    superviseur encadre le pipeline, il ne redéfinit pas son interface."""
    from eds import run

    recus = []
    monkeypatch.setattr(run, "main", lambda argv: recus.append(argv) or 0)

    assert supervision.main(["--tout"]) == 0
    assert recus == [["--tout"]]
