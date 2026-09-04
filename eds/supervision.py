"""Superviseur du pipeline — ce que `cron` déclenche réellement.

`cron` sait déclencher, et rien d'autre : il ignore qu'une exécution
précédente court encore, il ne relance pas ce qui a échoué, et il ne prévient
personne. Ce module comble les trois, sans ordonnanceur ni dépendance
supplémentaire — le choix de `cron` reste ainsi défendable.

    python -m eds.supervision              # ce que lance la ligne de crontab
    python -m eds.supervision --tout       # les options d'`eds.run` sont transmises
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime

from eds import config, journal
from eds.journal import FICHIER_LOG, REPERTOIRE_LOGS

LOG = logging.getLogger("eds.supervision")

VERROU = REPERTOIRE_LOGS / ".verrou"
ALERTE = REPERTOIRE_LOGS / "ALERTE.txt"
JOURNAL = FICHIER_LOG

# Codes de sortie d'`eds.run`, et ce qu'ils autorisent.
CODE_SUCCES = 0
CODE_METIER = 1  # la relance à l'identique ne changerait rien
CODE_INATTENDU = 2  # la cause peut disparaître d'elle-même

# Au-delà, la commande d'alerte est abandonnée : prévenir ne doit pas
# immobiliser le verrou.
DELAI_ALERTE_S = 30

# Les dernières lignes du journal suffisent à retrouver le `run_id` : le
# fichier grossit à chaque nuit, le relire en entier serait inutile.
LIGNES_JOURNAL_RELUES = 200


def _processus_vivant(pid: int) -> bool:
    """Le processus existe-t-il encore ? Signal 0 : on teste sans envoyer."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Le processus existe mais appartient à quelqu'un d'autre.
        return True
    return True


def _verrou_perime() -> bool:
    """Un verrou ne vaut que par le processus qu'il désigne.

    Une machine qui redémarre en pleine exécution laisse un fichier orphelin :
    le tenir pour valide bloquerait toutes les nuits suivantes.
    """
    try:
        pid = int(VERROU.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return not _processus_vivant(pid)


@contextmanager
def verrou():
    """Prend le verrou, ou cède la place. Rendu dans tous les cas.

    La création est atomique (`O_EXCL`) : deux exécutions lancées à la même
    seconde ne peuvent pas conclure toutes les deux qu'elles l'ont pris.
    """
    for tentative in (1, 2):
        try:
            descripteur = os.open(VERROU, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if tentative == 2 or not _verrou_perime():
                yield False
                return
            LOG.warning("verrou orphelin repris : %s", VERROU)
            VERROU.unlink(missing_ok=True)
            continue
        with os.fdopen(descripteur, "w", encoding="utf-8") as fichier:
            fichier.write(str(os.getpid()))
        try:
            yield True
        finally:
            VERROU.unlink(missing_ok=True)
        return


def _executer(argv, lancer, dormir) -> tuple[int, int]:
    """Lance le pipeline, relance les seuls échecs qu'une relance peut résoudre.

    Renvoie le dernier code de sortie et le nombre de tentatives consommées.
    """
    tentatives, attente = config.bornes_relance()

    for tentative in range(1, tentatives + 1):
        code = lancer(argv)
        if code != CODE_INATTENDU or tentative == tentatives:
            return code, tentative
        LOG.warning(
            "échec inattendu (code %s), relance dans %ss — tentative %s sur %s",
            code, attente, tentative + 1, tentatives,
        )
        dormir(attente)
    return code, tentatives


def _run_id_courant() -> str:
    """Le `run_id` de la dernière exécution journalisée.

    C'est lui qui joint l'alerte à `ops.executions` et aux colonnes `_run_id`
    des tables : sans lui, on saurait qu'une nuit a échoué, pas laquelle
    interroger.
    """
    try:
        lignes = JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "inconnu"
    for ligne in reversed(lignes[-LIGNES_JOURNAL_RELUES:]):
        try:
            run_id = json.loads(ligne).get("run_id")
        except json.JSONDecodeError:
            continue
        if run_id:
            return run_id
    return "inconnu"


def _resume(code: int, tentatives: int) -> str:
    """Ce qu'un humain doit savoir sans ouvrir un seul fichier."""
    cause = {
        CODE_METIER: "erreur métier — la relance à l'identique ne changera rien",
        CODE_INATTENDU: "échec inattendu — relancé sans succès",
    }.get(code, "échec")
    return "\n".join(
        (
            f"ÉCHEC DU PIPELINE EDS — {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"  code de sortie : {code} ({cause})",
            f"  tentatives     : {tentatives}",
            f"  run_id         : {_run_id_courant()}",
            "",
            "Reprise :",
            f"  1. lire {JOURNAL} et {REPERTOIRE_LOGS / 'cron.log'}",
            "  2. SELECT * FROM ops.executions WHERE statut = 'echec' ORDER BY demarre_a DESC",
            "  3. corriger la cause, puis relancer : .venv/bin/python -m eds.run",
            "     (l'exécution est idempotente : rien à restaurer, rien à purger)",
            "",
        )
    )


def alerter(resume: str) -> None:
    """Dépose l'alerte, et la pousse si le site a déclaré par quel moyen.

    Le fichier est la trace certaine ; `EDS_ALERTE_CMD` — webhook, courriel,
    notification — reste hors du dépôt, parce que le canal est une décision
    d'exploitation. Son échec ne devient jamais celui du pipeline.
    """
    ALERTE.write_text(resume, encoding="utf-8")
    LOG.error("alerte déposée : %s", ALERTE)

    commande = config.commande_alerte()
    if not commande:
        return
    try:
        subprocess.run(
            commande,
            shell=True,
            input=resume,
            text=True,
            timeout=DELAI_ALERTE_S,
            check=True,
        )
    except Exception:
        LOG.warning("commande d'alerte en échec : %s", commande, exc_info=True)


def superviser(argv, lancer, dormir=time.sleep) -> int:
    with verrou() as pris:
        if not pris:
            LOG.warning("exécution déjà en cours, rien n'est lancé")
            return CODE_SUCCES
        code, tentatives = _executer(argv, lancer, dormir)

    if code == CODE_SUCCES:
        # L'alerte est un état, pas un historique : une nuit réussie
        # l'éteint, le journal garde la trace de l'incident.
        ALERTE.unlink(missing_ok=True)
    else:
        alerter(_resume(code, tentatives))
    return code


def bandeau_alerte() -> str:
    """L'alerte en cours, telle que `eds.run --etat` doit l'afficher.

    C'est ce qui referme la boucle : le fichier ne suppose plus qu'un humain
    pense à aller le lire, il remonte dans la première commande qu'on tape.
    """
    try:
        contenu = ALERTE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not contenu:
        return ""
    return "\n".join(("", "  !! ALERTE EN COURS", *(f"  {l}" for l in contenu.splitlines()), ""))


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée de la ligne de crontab. Les options vont à `eds.run`."""
    from eds import run

    journal.configurer()
    return superviser(
        list(argv) if argv is not None else sys.argv[1:],
        lancer=run.main,
    )


if __name__ == "__main__":
    sys.exit(main())
