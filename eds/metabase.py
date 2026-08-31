"""Configuration automatisée de Metabase : compte, connexions, dashboards.

Aucune étape manuelle n'est requise. Le rendu demande « comment lancer &
rejouer » : une interface qu'il faudrait reconfigurer à la souris après
chaque remise à zéro ne satisferait pas cette exigence.

Point de conception important : Metabase se connecte à ClickHouse avec les
comptes CLOISONNÉS `eds_pilotage` et `eds_recherche`, jamais avec le compte
d'administration. Le cloisonnement RGPD est donc appliqué jusque dans
l'outil de restitution — un utilisateur du dashboard de pilotage ne peut
pas atteindre les cohortes de recherche, même en écrivant sa propre requête.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from eds.config import exiger
from eds.dashboards import GRILLE

LOG = logging.getLogger("eds.metabase")

BASE = "http://localhost:3000/api"
# Vu depuis le conteneur Metabase, ClickHouse porte le nom du service Docker.
HOTE_CLICKHOUSE = "clickhouse"


class ErreurMetabase(Exception):
    pass


def _appel(chemin: str, methode: str = "GET", corps: Any = None,
           session: str | None = None) -> Any:
    requete = urllib.request.Request(
        f"{BASE}{chemin}", method=methode,
        data=json.dumps(corps).encode("utf-8") if corps is not None else None,
        headers={"Content-Type": "application/json",
                 **({"X-Metabase-Session": session} if session else {})},
    )
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            charge = reponse.read()
            return json.loads(charge) if charge else None
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "ignore")[:400]
        raise ErreurMetabase(f"{methode} {chemin} -> {erreur.code} : {detail}") from erreur


def ouvrir_session() -> str:
    """Crée le compte admin au premier lancement, puis ouvre une session."""
    proprietes = _appel("/session/properties")
    courriel = exiger("MB_ADMIN_EMAIL")
    mot_de_passe = exiger("MB_ADMIN_PASSWORD")

    if not proprietes.get("has-user-setup"):
        LOG.info("premier lancement : création du compte administrateur")
        reponse = _appel("/setup", "POST", {
            "token": proprietes["setup-token"],
            "user": {"first_name": "Admin", "last_name": "EDS",
                     "email": courriel, "password": mot_de_passe,
                     "site_name": "EDS CHU"},
            "prefs": {"site_name": "EDS CHU", "allow_tracking": False},
        })
        return reponse["id"] if isinstance(reponse, dict) else reponse

    return _appel("/session", "POST",
                  {"username": courriel, "password": mot_de_passe})["id"]


def declarer_base(session: str, nom: str, base_ch: str,
                  utilisateur: str, mot_de_passe: str) -> int:
    """Déclare une base ClickHouse dans Metabase, ou retourne l'existante."""
    for base in _appel("/database", session=session)["data"]:
        if base["name"] == nom:
            return base["id"]

    cree = _appel("/database", "POST", {
        "engine": "clickhouse",
        "name": nom,
        "details": {
            "host": HOTE_CLICKHOUSE, "port": 8123,
            "user": utilisateur, "password": mot_de_passe,
            "dbname": base_ch, "ssl": False,
            "scan-all-databases": False,
        },
        "is_full_sync": True,
    }, session=session)
    LOG.info("base déclarée", extra={"source": nom})
    return cree["id"]


def configurer() -> dict[str, int]:
    """Crée le compte et les deux connexions cloisonnées."""
    session = ouvrir_session()
    return {
        "pilotage": declarer_base(session, "EDS — Pilotage hospitalier",
                                  "gold_pilotage", "eds_pilotage",
                                  exiger("CH_PILOTAGE_PASSWORD")),
        "recherche": declarer_base(session, "EDS — Recherche clinique",
                                   "gold_recherche", "eds_recherche",
                                   exiger("CH_RECHERCHE_PASSWORD")),
    }, session


# ── Questions et tableaux de bord ────────────────────────────────────────

def _carte_existante(session: str, nom: str) -> int | None:
    for carte in _appel("/card", session=session):
        if carte["name"] == nom and not carte.get("archived"):
            return carte["id"]
    return None


def creer_question(session: str, base_id: int, question: dict) -> int:
    """Crée ou met à jour une question. Idempotent : rejouable sans doublon."""
    charge = {
        "name": question["nom"],
        "dataset_query": {"type": "native",
                          "native": {"query": question["sql"].strip()},
                          "database": base_id},
        "display": question["affichage"],
        "visualization_settings": question["vis"],
        "collection_id": None,
    }
    existante = _carte_existante(session, question["nom"])
    if existante:
        _appel(f"/card/{existante}", "PUT", charge, session=session)
        return existante
    return _appel("/card", "POST", charge, session=session)["id"]


def _dashboard_existant(session: str, nom: str) -> int | None:
    for tableau in _appel("/dashboard", session=session):
        if tableau["name"] == nom and not tableau.get("archived"):
            return tableau["id"]
    return None


def creer_tableau(session: str, base_id: int, definition: dict) -> int:
    """Crée le tableau de bord, ses questions et sa mise en page."""
    nom = definition["nom"]
    tableau_id = _dashboard_existant(session, nom)
    if tableau_id is None:
        tableau_id = _appel("/dashboard", "POST",
                            {"name": nom, "description": definition["description"]},
                            session=session)["id"]

    # L'encart de limites occupe toute la largeur, en tête.
    cartes = [{
        "id": -1, "card_id": None,
        "row": 0, "col": 0, "size_x": GRILLE, "size_y": 4,
        "parameter_mappings": [],
        "visualization_settings": {
            "virtual_card": {"name": None, "display": "text",
                             "visualization_settings": {}, "dataset_query": {},
                             "archived": False},
            "text": definition["encart"],
            "dashcard.background": False,
        },
    }]

    for index, question in enumerate(definition["questions"], start=2):
        carte_id = creer_question(session, base_id, question)
        col, ligne, largeur, hauteur = question["pos"]
        cartes.append({
            "id": -index, "card_id": carte_id,
            "row": ligne + 4, "col": col,          # +4 : sous l'encart
            "size_x": largeur, "size_y": hauteur,
            "parameter_mappings": [], "visualization_settings": {},
        })

    _appel(f"/dashboard/{tableau_id}", "PUT", {"dashcards": cartes}, session=session)
    LOG.info("tableau de bord publié",
             extra={"source": nom, "lignes": len(definition["questions"])})
    return tableau_id


def retirer_contenu_exemple(session: str) -> None:
    """Supprime la base de démonstration livrée avec Metabase.

    Une installation neuve embarque une base H2 « Sample Database » et une
    collection « Examples » remplies de tableaux de bord e-commerce. Les
    laisser noierait les deux tableaux de l'EDS au milieu d'un contenu sans
    rapport, et donnerait une interface qui n'est pas celle qu'on livre.
    """
    for base in _appel("/database", session=session)["data"]:
        if base.get("is_sample"):
            _appel(f"/database/{base['id']}", "DELETE", session=session)
            LOG.info("base de démonstration retirée", extra={"source": base["name"]})

    for collection in _appel("/collection", session=session):
        if collection.get("is_sample") and collection.get("id") != "root":
            _appel(f"/collection/{collection['id']}", "PUT",
                   {"archived": True}, session=session)
            LOG.info("collection de démonstration archivée",
                     extra={"source": collection.get("name")})


def installer() -> dict[str, str]:
    """Configure Metabase de bout en bout. Idempotent."""
    from eds.dashboards import GRILLE as _, TABLEAUX

    bases, session = configurer()
    retirer_contenu_exemple(session)
    liens = {}
    for definition in TABLEAUX:
        tableau_id = creer_tableau(session, bases[definition["base"]], definition)
        liens[definition["nom"]] = f"http://localhost:3000/dashboard/{tableau_id}"
    exporter(session)
    return liens


def exporter(session: str | None = None) -> Path:
    """Écrit la définition des tableaux de bord dans le dépôt.

    Le rendu demande « les dashboards (ou leur export) ». Le SQL source vit
    déjà dans `eds/dashboards.py` ; cet export capture en plus l'état publié
    (identifiants, mise en page), ce qui permet de constater ce qui tourne
    sans avoir à démarrer Metabase.
    """
    from eds.config import RACINE

    session = session or ouvrir_session()
    destination = RACINE / "metabase"
    destination.mkdir(exist_ok=True)

    export = []
    for tableau in _appel("/dashboard", session=session):
        if tableau.get("archived"):
            continue
        detail = _appel(f"/dashboard/{tableau['id']}", session=session)
        export.append({
            "nom": detail["name"],
            "description": detail.get("description"),
            "questions": [
                {
                    "nom": c["card"]["name"],
                    "affichage": c["card"].get("display"),
                    "sql": c["card"].get("dataset_query", {})
                                    .get("native", {}).get("query"),
                }
                for c in detail.get("dashcards", []) if c.get("card_id")
            ],
        })

    fichier = destination / "dashboards.json"
    fichier.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("dashboards exportés", extra={"source": str(fichier.name),
                                           "lignes": len(export)})
    return fichier


if __name__ == "__main__":
    from eds import journal
    journal.configurer()
    for nom, lien in installer().items():
        print(f"  {nom:26} {lien}")
