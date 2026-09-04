"""Réglages communs aux tests unitaires.

CE QUE CES TESTS COUVRENT, ET CE QU'ILS NE COUVRENT PAS. Ils s'exercent sur
les fonctions PURES du pipeline — celles qui transforment une valeur en une
autre sans toucher ni au disque partagé ni à ClickHouse : pseudonymisation,
généralisation des dates, liste blanche des colonnes, découpage du SQL,
résolution des référentiels, validation des entrées interpolées.

Une exception assumée : `test_supervision.py` écrit sur le disque, puisque le
verrou et l'alerte SONT des fichiers. Il reste dans cette suite parce qu'il
n'en écrit aucun ailleurs que dans un répertoire jetable, n'ouvre aucune
connexion, et injecte un faux pipeline à la place d'`eds.run` : hors ligne et
instantané, comme le reste.

Ils ne remplacent donc PAS `tests.verifier` ni `tests.demontrer`, qui
s'exécutent contre un entrepôt vivant et prouvent des propriétés qu'aucun
test unitaire ne peut atteindre : l'équation de conservation sur les vraies
volumétries, le refus prononcé par le moteur, la remise en état après
injection. Les deux niveaux sont complémentaires — celui-ci tourne en une
seconde et sans Docker, l'autre prouve que le système fonctionne vraiment.

LE SEL EST FIXÉ ICI, ET C'EST LE POINT DÉLICAT. `eds.lake.pseudonymiser` lit
`EDS_PSEUDO_SALT` à travers un `lru_cache` : sans purge, le premier test qui
hache fige le sel pour toute la session, et un test suivant qui en pose un
autre observerait le premier. Chaque test qui touche au sel passe donc par la
fixture `sel`, qui purge les deux caches avant ET après.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

# Sel de test, jamais celui du projet : un pseudonyme calculé ici ne doit
# correspondre à aucun pseudonyme réel de l'entrepôt.
SEL_DE_TEST = "sel-de-test-uniquement-jamais-en-production"


@pytest.fixture
def sel(monkeypatch):
    """Pose un sel connu et purge les caches de pseudonymisation.

    La purge a lieu AVANT (pour ne pas hériter d'un sel posé par un test
    précédent) et APRÈS (pour ne pas en imposer un au suivant).
    """
    from eds import lake

    lake._sel.cache_clear()
    lake.pseudonymiser.cache_clear()
    monkeypatch.setenv("EDS_PSEUDO_SALT", SEL_DE_TEST)
    yield SEL_DE_TEST
    lake._sel.cache_clear()
    lake.pseudonymiser.cache_clear()


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Un dépôt CHU factice, à la place de `source-filestorage`.

    Renvoie la racine du faux dépôt. Les modules lisent `eds.config.SOURCE`
    au moment de l'appel, jamais à l'import : le remplacer suffit.
    """
    from eds import config, lake

    racine = tmp_path / "source"
    racine.mkdir()
    monkeypatch.setattr(config, "SOURCE", racine)
    monkeypatch.setattr(lake, "SOURCE", racine)
    return racine


@pytest.fixture
def lake_factice(tmp_path, monkeypatch):
    """Un lake vide et jetable, à la place de `lake/`."""
    from eds import config, lake, warehouse

    racine = tmp_path / "lake"
    racine.mkdir()
    monkeypatch.setattr(config, "LAKE", racine)
    monkeypatch.setattr(lake, "LAKE", racine)
    monkeypatch.setattr(warehouse, "LAKE", racine)
    return racine


def deposer(racine: Path, chemin: str, contenu: str) -> Path:
    """Écrit un fichier dans le dépôt factice, répertoires compris."""
    cible = racine / chemin
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(contenu, encoding="utf-8")
    return cible


@pytest.fixture
def env_vierge(monkeypatch):
    """Neutralise `.env` et les variables déjà posées dans l'environnement.

    `eds.config._charger_env` lit le `.env` du dépôt : sans cette fixture, un
    test des seuils par défaut passerait ou échouerait selon ce que le poste
    de développement a dans son fichier.
    """
    from eds import config

    monkeypatch.setattr(config, "_charger_env", lambda *a, **k: None)
    for nom in list(os.environ):
        if nom.startswith("EDS_") or nom.startswith("CH_") or nom.startswith("MB_"):
            monkeypatch.delenv(nom, raising=False)
