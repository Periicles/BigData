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


def _appel(
    chemin: str, methode: str = "GET", corps: Any = None, session: str | None = None
) -> Any:
    requete = urllib.request.Request(
        f"{BASE}{chemin}",
        method=methode,
        data=json.dumps(corps).encode("utf-8") if corps is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"X-Metabase-Session": session} if session else {}),
        },
    )
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            charge = reponse.read()
            return json.loads(charge) if charge else None
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "ignore")[:400]
        raise ErreurMetabase(
            f"{methode} {chemin} -> {erreur.code} : {detail}"
        ) from erreur


def ouvrir_session() -> str:
    """Crée le compte admin au premier lancement, puis ouvre une session."""
    proprietes = _appel("/session/properties")
    courriel = exiger("MB_ADMIN_EMAIL")
    mot_de_passe = exiger("MB_ADMIN_PASSWORD")

    if not proprietes.get("has-user-setup"):
        LOG.info("premier lancement : création du compte administrateur")
        reponse = _appel(
            "/setup",
            "POST",
            {
                "token": proprietes["setup-token"],
                "user": {
                    "first_name": "Admin",
                    "last_name": "EDS",
                    "email": courriel,
                    "password": mot_de_passe,
                    "site_name": "EDS CHU",
                },
                "prefs": {"site_name": "EDS CHU", "allow_tracking": False},
            },
        )
        return reponse["id"] if isinstance(reponse, dict) else reponse

    return _appel("/session", "POST", {"username": courriel, "password": mot_de_passe})[
        "id"
    ]


def declarer_base(session: str, nom: str, base_ch: str, utilisateur: str,
                  mot_de_passe: str, toutes_bases: bool = False) -> int:
    """Déclare une base ClickHouse dans Metabase, ou retourne l'existante."""
    for base in _appel("/database", session=session)["data"]:
        if base["name"] == nom:
            return base["id"]

    cree = _appel(
        "/database",
        "POST",
        {
            "engine": "clickhouse",
            "name": nom,
            "details": {
                "host": HOTE_CLICKHOUSE,
                "port": 8123,
                "user": utilisateur,
                "password": mot_de_passe,
                "dbname": base_ch,
                "ssl": False,
                # Le compte d'exploitation couvre trois bases : on laisse
                # Metabase les découvrir toutes plutôt que d'en figer une.
                "scan-all-databases": toutes_bases,
            },
            "is_full_sync": True,
        },
        session=session,
    )
    LOG.info("base déclarée", extra={"source": nom})
    return cree["id"]


def configurer() -> dict[str, int]:
    """Crée le compte et les deux connexions cloisonnées."""
    session = ouvrir_session()
    return {
        "pilotage": declarer_base(
            session,
            "EDS — Pilotage hospitalier",
            "gold_pilotage",
            "eds_pilotage",
            exiger("CH_PILOTAGE_PASSWORD"),
        ),
        "recherche": declarer_base(
            session,
            "EDS — Recherche clinique",
            "gold_recherche",
            "eds_recherche",
            exiger("CH_RECHERCHE_PASSWORD"),
        ),
        # Réservée à l'administration : les couches techniques, en lecture
        # seule. Elle permet d'investiguer un incident ou d'instruire une
        # demande d'effacement sans quitter l'outil, et sans utiliser le
        # compte du pipeline, qui peut écrire.
        "exploitation": declarer_base(
            session,
            "EDS — Exploitation (bronze, silver, journal)",
            "silver",
            "eds_exploitation",
            exiger("CH_EXPLOITATION_PASSWORD"),
            toutes_bases=True,
        ),
    }, session


# ── Questions et tableaux de bord ────────────────────────────────────────


def _carte_existante(session: str, nom: str) -> int | None:
    for carte in _appel("/card", session=session):
        if carte["name"] == nom and not carte.get("archived"):
            return carte["id"]
    return None


def creer_question(
    session: str, base_id: int, question: dict, collection_id: int | None = None
) -> int:
    """Crée ou met à jour une question. Idempotent : rejouable sans doublon."""
    charge = {
        "name": question["nom"],
        "dataset_query": {
            "type": "native",
            "native": {"query": question["sql"].strip()},
            "database": base_id,
        },
        "display": question["affichage"],
        "visualization_settings": question["vis"],
        "collection_id": collection_id,
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


def creer_tableau(
    session: str, base_id: int, definition: dict, collection_id: int
) -> int:
    """Crée le tableau de bord, ses questions et sa mise en page.

    Tableau et questions sont rangés dans une collection dédiée : c'est
    l'unité sur laquelle portent les permissions de lecture.
    """
    nom = definition["nom"]
    tableau_id = _dashboard_existant(session, nom)
    if tableau_id is None:
        tableau_id = _appel(
            "/dashboard",
            "POST",
            {
                "name": nom,
                "description": definition["description"],
                "collection_id": collection_id,
            },
            session=session,
        )["id"]
    else:
        _appel(
            f"/dashboard/{tableau_id}",
            "PUT",
            {"collection_id": collection_id},
            session=session,
        )

    # L'encart de limites occupe toute la largeur, en tête.
    cartes = [
        {
            "id": -1,
            "card_id": None,
            "row": 0,
            "col": 0,
            "size_x": GRILLE,
            "size_y": 4,
            "parameter_mappings": [],
            "visualization_settings": {
                "virtual_card": {
                    "name": None,
                    "display": "text",
                    "visualization_settings": {},
                    "dataset_query": {},
                    "archived": False,
                },
                "text": definition["encart"],
                "dashcard.background": False,
            },
        }
    ]

    for index, question in enumerate(definition["questions"], start=2):
        carte_id = creer_question(session, base_id, question, collection_id)
        col, ligne, largeur, hauteur = question["pos"]
        cartes.append(
            {
                "id": -index,
                "card_id": carte_id,
                "row": ligne + 4,
                "col": col,  # +4 : sous l'encart
                "size_x": largeur,
                "size_y": hauteur,
                "parameter_mappings": [],
                "visualization_settings": {},
            }
        )

    _appel(f"/dashboard/{tableau_id}", "PUT", {"dashcards": cartes}, session=session)
    LOG.info(
        "tableau de bord publié",
        extra={"source": nom, "lignes": len(definition["questions"])},
    )
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
            _appel(
                f"/collection/{collection['id']}",
                "PUT",
                {"archived": True},
                session=session,
            )
            LOG.info(
                "collection de démonstration archivée",
                extra={"source": collection.get("name")},
            )


def installer() -> dict[str, str]:
    """Configure Metabase de bout en bout. Idempotent."""
    from eds.dashboards import GRILLE as _, TABLEAUX

    bases, session = configurer()
    retirer_contenu_exemple(session)

    collections = {d["base"]: _collection(session, d["nom"]) for d in TABLEAUX}
    groupes = {cle: _groupe(session, c["groupe"]) for cle, c in COMPTES.items()}

    liens = {}
    for definition in TABLEAUX:
        cle = definition["base"]
        tableau_id = creer_tableau(session, bases[cle], definition, collections[cle])
        liens[definition["nom"]] = f"http://localhost:3000/dashboard/{tableau_id}"

    appliquer_permissions_donnees(session, bases, groupes)
    appliquer_permissions_collections(session, collections, groupes)
    for cle, compte in COMPTES.items():
        creer_utilisateur(session, compte, groupes[cle])

    exporter(session)
    return liens


# ── Comptes et permissions ───────────────────────────────────────────────
#
# Trois comptes, pas un seul. Un administrateur qui verrait les deux
# tableaux de bord ne démontrerait rien : le cloisonnement RGPD doit être
# visible jusque dans l'outil de restitution.
#
#   admin      — configure la plateforme, voit tout
#   pilotage   — le tableau de bord hospitalier et la base gold_pilotage
#   recherche  — le tableau de bord de recherche et la base gold_recherche
#
# La séparation est appliquée à deux niveaux : les permissions de DONNÉES
# (quelle base le groupe peut interroger) et les permissions de COLLECTION
# (quel tableau de bord le groupe peut ouvrir).

COMPTES = {
    "pilotage": {
        "groupe": "Pilotage hospitalier",
        "courriel": "pilotage@eds-chu.local",
        "prenom": "Direction",
        "nom": "Pilotage",
        "variable_mdp": "MB_PILOTAGE_PASSWORD",
    },
    "recherche": {
        "groupe": "Recherche clinique",
        "courriel": "recherche@eds-chu.local",
        "prenom": "Equipe",
        "nom": "Recherche",
        "variable_mdp": "MB_RECHERCHE_PASSWORD",
    },
}


def _groupe(session: str, nom: str) -> int:
    for g in _appel("/permissions/group", session=session):
        if g["name"] == nom:
            return g["id"]
    return _appel("/permissions/group", "POST", {"name": nom}, session=session)["id"]


def _collection(session: str, nom: str) -> int:
    for c in _appel("/collection", session=session):
        if c.get("name") == nom and not c.get("archived") and c.get("id") != "root":
            return c["id"]
    return _appel(
        "/collection", "POST", {"name": nom, "parent_id": None}, session=session
    )["id"]


# Metabase OSS ne permet pas de BLOQUER la vue des données par groupe :
# « view-data: blocked » est réservé à l'édition payante. On restreint donc
# ce qui l'est en OSS — la possibilité de composer ses propres requêtes —
# et la séparation effective repose sur deux autres barrières :
#
#   1. les permissions de COLLECTION, qui déterminent quel tableau de bord
#      un groupe peut ouvrir (pleinement supportées en OSS) ;
#   2. surtout, les DROITS CLICKHOUSE : chaque connexion Metabase utilise un
#      compte distinct (eds_pilotage / eds_recherche) qui n'a de GRANT que
#      sur sa propre base. C'est le moteur qui refuse, et aucun réglage de
#      l'outil de restitution ne peut contourner cela.
#
# La restriction Metabase est donc une défense en profondeur, pas la
# garantie principale.
# Les comptes métier CONSULTENT des tableaux de bord ; ils n'interrogent pas
# l'entrepôt. « create-queries: no » leur retire l'éditeur SQL et le
# générateur de requêtes. Sans cela, un utilisateur du pilotage pourrait lire
# fact_sejour ligne par ligne, avec le pseudonyme patient — bien au-delà de
# son besoin, qui est de consulter des indicateurs.
#
# Ils conservent l'accès en lecture aux questions enregistrées de leur tableau
# de bord : c'est l'objet des permissions de collection.
ACCES_RESTITUTION = {"view-data": "unrestricted", "create-queries": "no"}


def appliquer_permissions_donnees(
    session: str, bases: dict[str, int], groupes: dict[str, int]
) -> None:
    """Aucun groupe métier ne peut composer de requêtes.

    Les comptes de pilotage et de recherche consultent leurs tableaux de
    bord ; ils n'ont ni éditeur SQL ni générateur de requêtes. Seule
    l'administration, membre du groupe Administrators, conserve ce droit.

    « All Users » est restreint de la même façon : tout compte créé en fait
    partie d'office et hériterait sinon d'un accès plus large.
    """
    graphe = _appel("/permissions/graph", session=session)
    groupe_tous = next(
        g["id"]
        for g in _appel("/permissions/group", session=session)
        if g["name"] == "All Users"
    )

    # Aucun groupe métier ne compose de requêtes, sur aucune base. Seul le
    # groupe Administrators conserve ce droit, hors de ce graphe.
    restriction = {str(i): dict(ACCES_RESTITUTION) for i in bases.values()}
    nouveau = {str(groupe_tous): dict(restriction)}
    for groupe_id in groupes.values():
        nouveau[str(groupe_id)] = dict(restriction)

    graphe["groups"].update(nouveau)
    _appel(
        "/permissions/graph",
        "PUT",
        {"revision": graphe["revision"], "groups": graphe["groups"]},
        session=session,
    )
    LOG.info("permissions de données appliquées", extra={"lignes": len(nouveau)})


def appliquer_permissions_collections(
    session: str, collections: dict[str, int], groupes: dict[str, int]
) -> None:
    """Chaque groupe ne voit que la collection de son tableau de bord.

    Il faut RETIRER « All Users » des deux collections : ce groupe y a
    l'accès en écriture par défaut, et comme tout compte en est membre
    d'office, cet héritage annulerait les permissions des groupes métier.
    """
    graphe = _appel("/collection/graph", session=session)

    groupe_tous = str(
        next(
            g["id"]
            for g in _appel("/permissions/group", session=session)
            if g["name"] == "All Users"
        )
    )
    graphe["groups"].setdefault(groupe_tous, {})
    for collection_id in collections.values():
        graphe["groups"][groupe_tous][str(collection_id)] = "none"

    for cle, groupe_id in groupes.items():
        graphe["groups"].setdefault(str(groupe_id), {})
        for autre, collection_id in collections.items():
            graphe["groups"][str(groupe_id)][str(collection_id)] = (
                "read" if cle == autre else "none"
            )
    _appel(
        "/collection/graph",
        "PUT",
        {"revision": graphe["revision"], "groups": graphe["groups"]},
        session=session,
    )
    LOG.info("permissions de collections appliquées")


def creer_utilisateur(session: str, compte: dict, groupe_id: int) -> None:
    """Crée le compte s'il n'existe pas, en l'affectant à son seul groupe."""
    existants = _appel("/user", session=session)
    liste = existants["data"] if isinstance(existants, dict) else existants
    if any(u["email"] == compte["courriel"] for u in liste):
        return
    # Metabase impose que tout compte reste membre de « All Users » : ce
    # groupe ne se quitte pas. On l'ajoute donc au groupe métier, sans le
    # retirer du groupe par défaut — d'où l'intérêt d'avoir restreint
    # « All Users » sur les deux bases.
    groupe_tous = next(
        g["id"]
        for g in _appel("/permissions/group", session=session)
        if g["name"] == "All Users"
    )
    _appel(
        "/user",
        "POST",
        {
            "first_name": compte["prenom"],
            "last_name": compte["nom"],
            "email": compte["courriel"],
            "password": exiger(compte["variable_mdp"]),
            "user_group_memberships": [{"id": groupe_tous}, {"id": groupe_id}],
        },
        session=session,
    )
    LOG.info("compte créé", extra={"source": compte["courriel"]})


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
        export.append(
            {
                "nom": detail["name"],
                "description": detail.get("description"),
                "questions": [
                    {
                        "nom": c["card"]["name"],
                        "affichage": c["card"].get("display"),
                        "sql": c["card"]
                        .get("dataset_query", {})
                        .get("native", {})
                        .get("query"),
                    }
                    for c in detail.get("dashcards", [])
                    if c.get("card_id")
                ],
            }
        )

    fichier = destination / "dashboards.json"
    fichier.write_text(
        json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    LOG.info(
        "dashboards exportés",
        extra={"source": str(fichier.name), "lignes": len(export)},
    )
    return fichier


if __name__ == "__main__":
    from eds import journal

    journal.configurer()
    for nom, lien in installer().items():
        print(f"  {nom:26} {lien}")
