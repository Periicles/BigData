"""Provisionnement de Metabase par son API REST — la restitution du § 1 du sujet.

    python -m eds.restitution           # provisionne tout : connexions, comptes, droits,
                                         # questions et les deux tableaux de bord
    python -m eds.restitution --etat    # état de Metabase, sans rien modifier

POURQUOI un module et pas une configuration manuelle dans l'interface :
le reste de l'entrepôt (lake -> bronze -> silver -> gold, droits ClickHouse)
est entièrement scripté et rejouable — une restitution posée à la souris
romprait cette propriété et ne serait démontrable qu'en capture d'écran. Ce
module pilote Metabase exactement comme `eds.run` pilote ClickHouse : les
tableaux de bord « Pilotage hospitalier » et « Recherche clinique », leurs
questions SQL natives et leur mise en page sont tous posés par ce module,
jamais construits à la souris.

Propriétés garanties :

  IDEMPOTENCE   chaque création est précédée d'une recherche par NOM (base,
                groupe, utilisateur, collection, question, tableau de bord) ;
                un objet déjà là est mis à jour, pas dupliqué. Rejouer le
                module deux fois ne change pas les compteurs d'objets
                Metabase, y compris pour la mise en page d'un tableau de
                bord (voir `poser_tableau_de_bord`, qui remplace la totalité
                de la disposition à chaque passage plutôt que de l'accumuler).
  CLOISONNEMENT le graphe de permissions n'accorde à un groupe métier QUE la
                base ClickHouse — et la collection Metabase — de son usage,
                mais c'est déjà le compte ClickHouse borné (`eds_pilotage` /
                `eds_recherche`, posé par sql/50_droits.sql) qui prononce le
                refus final. Le réglage ici est une défense en profondeur,
                pas la frontière elle-même (cf. le commentaire du service
                `metabase` dans docker-compose.yml).
  VÉRIFICATION  chaque question est interrogée après coup via
                `/api/card/:id/query` (`verifier_cartes`) : une question en
                erreur, ou qui ne renvoie aucune ligne, interrompt le
                provisionnement — jamais un tableau de bord silencieusement
                cassé. Le recoupement avec des CHIFFRES de référence (le
                nombre de séjours, le taux de réadmission…) n'a pas sa place
                ici : le jeu de données source n'est pas versionné (voir
                `eds.run --tout`) et un rechargement légitime avec d'autres
                volumes ferait alors échouer ce module sans que rien n'y soit
                cassé. Ce recoupement chiffré existe : c'est
                `tests.demontrer restitution`, qui le calcule EN DIRECT contre
                l'entrepôt plutôt que contre une constante figée.

Ce qui a été VÉRIFIÉ EMPIRIQUEMENT contre Metabase 0.58.32 (l'API de cette
version n'est pas documentée dans ce contexte, donc rien ci-dessous n'est
deviné — chaque point a été rejoué en pratique, à l'exception signalée
ci-dessous, y compris sur une instance jetable pour ne rien risquer sur celle
qui tourne) :

  - POST /api/setup renvoie DIRECTEMENT {"id": <session>} : pas de login
    séparé après le tout premier démarrage.
  - Le champ "setup-token" de GET /api/session/properties reste NON NUL même
    après le setup — il ne dit donc rien sur l'état de configuration,
    contrairement à ce qu'on pourrait attendre. Le signal fiable est
    l'échec (ou le succès) d'une connexion normale via POST /api/session.
  - "view-data": "blocked" (blocage total d'une base pour un groupe) exige un
    jeton premium : l'API répond "The blocked permissions functionality is
    only enabled if you have a premium token with the advanced-permissions
    feature." Relevé en 0.56.13 et NON rejoué en 0.58.32 — le code n'emprunte
    pas ce chemin. Celui qu'il emprunte, "legacy-no-self-service", reste lui
    vérifié de bout en bout par `tests.demontrer restitution` : la base n'est
    plus parcourable ni interrogeable par le groupe, sans fonction payante.
  - Un groupe nouvellement créé hérite par défaut d'un accès "unrestricted" à
    TOUTES les bases existantes — il faut le restreindre explicitement,
    jamais supposer qu'il part fermé.
  - PUT /api/user/:id avec un champ "password" ne renvoie aucune erreur mais
    ne change RIEN : l'ancien mot de passe continue de fonctionner. Metabase
    n'expose pas de réinitialisation admin par cette voie ; le mot de passe
    n'est donc posé qu'à la création de l'utilisateur.
  - Les noms de groupe sont uniques côté serveur (un doublon est refusé en
    400 : "A group with that name already exists."). Les bases, elles,
    n'ont AUCUNE contrainte d'unicité sur le nom : deux bases homonymes se
    créent sans erreur (rejoué en 0.58.32, et déjà observé sur l'instance de
    ce projet, résidu d'essais manuels antérieurs). C'est pourquoi la purge
    des doublons ne vise que cet objet-là.
  - DELETE /api/permissions/group/:id EXISTE en 0.58.32 et répond 204 — ce
    n'était pas le cas en 0.56.13 ("API endpoint does not exist"). Rien n'en
    dépend ici : les groupes sont recherchés avant création, jamais
    supprimés. Le noter évite qu'un lecteur croie la contrainte toujours en
    vigueur.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request

from eds import journal as mod_journal
from eds.config import exiger

LOG = logging.getLogger("eds.restitution")

# Exposé par docker-compose (`ports: 3000:3000`) : ce script tourne sur
# l'hôte, jamais dans le réseau Docker — à ne pas confondre avec le nom
# `clickhouse` utilisé dans les connexions ClickHouse posées plus bas, qui
# lui doit rester le nom du service (c'est Metabase, DANS le conteneur, qui
# doit joindre ClickHouse).
MB_URL = "http://localhost:3000"

TENTATIVES_SANTE_MAX = 60
INTERVALLE_SANTE_S = 2

TENTATIVES_SYNC_MAX = 30
INTERVALLE_SYNC_S = 2

# Noms des objets Metabase — stables d'une exécution à l'autre : c'est sur
# eux que repose la recherche-avant-création qui rend le module idempotent.
NOM_BASE_PILOTAGE = "Pilotage hospitalier"
NOM_BASE_RECHERCHE = "Recherche clinique"
NOM_GROUPE_PILOTAGE = "Pilotage"
NOM_GROUPE_RECHERCHE = "Recherche"

# Le contenu de démonstration se retire en plusieurs passes (cf.
# purger_contenu_exemple) ; cette borne évite une boucle infinie si une
# version future de Metabase refusait de supprimer l'un de ses objets.
PASSES_PURGE_MAX = 5

HOTE_CLICKHOUSE = "clickhouse"  # nom du service Docker, pas "localhost"
PORT_CLICKHOUSE = 8123

# Les DEUX tableaux de bord portent le même nom que leur base ClickHouse
# (NOM_BASE_PILOTAGE / NOM_BASE_RECHERCHE) — ainsi que leur collection
# Metabase : ce sont trois types d'objets distincts pour l'API, donc aucun
# risque de collision, et un seul nom pour désigner une seule et même chose
# évite de multiplier les constantes.
COULEUR_COLLECTION = "#509EE3"  # bleu par défaut de Metabase, sans portée métier


class ErreurRestitution(Exception):
    """Échec métier : la relance à l'identique ne changera rien sans correction."""


class ErreurMetabase(Exception):
    """Échec d'un appel à l'API Metabase (transport, ou refus du serveur)."""


class ClientMetabase:
    """Client HTTP minimal pour l'API Metabase, fondé sur `urllib` (stdlib).

    Aucune dépendance nouvelle : l'API REST de Metabase est simple (JSON en
    entrée/sortie, un seul en-tête d'authentification), `requests` n'aurait
    rien apporté ici. Le jeton de session voyage dans `X-Metabase-Session` et
    n'est JAMAIS journalisé — `_requete` ne trace que la méthode et le
    chemin, jamais les en-têtes ni le corps.
    """

    def __init__(self, base_url: str = MB_URL) -> None:
        self.base_url = base_url
        self.session_id: str | None = None

    def _requete(self, methode: str, chemin: str, corps: dict | None = None):
        entetes = {"Content-Type": "application/json"}
        if self.session_id:
            entetes["X-Metabase-Session"] = self.session_id
        donnees = json.dumps(corps).encode("utf-8") if corps is not None else None
        requete = urllib.request.Request(
            f"{self.base_url}{chemin}", data=donnees, method=methode, headers=entetes
        )
        try:
            with urllib.request.urlopen(requete, timeout=30) as reponse:
                brut = reponse.read()
        except urllib.error.HTTPError as erreur:
            detail = erreur.read().decode("utf-8", "replace")[:300]
            raise ErreurMetabase(f"{methode} {chemin} -> HTTP {erreur.code} : {detail}") from None
        except urllib.error.URLError as erreur:
            raise ErreurMetabase(f"{methode} {chemin} injoignable : {erreur.reason}") from erreur
        return json.loads(brut) if brut else None

    def get(self, chemin: str):
        return self._requete("GET", chemin)

    def post(self, chemin: str, corps: dict | None = None):
        return self._requete("POST", chemin, corps)

    def put(self, chemin: str, corps: dict | None = None):
        return self._requete("PUT", chemin, corps)

    def delete(self, chemin: str):
        return self._requete("DELETE", chemin)


# ── démarrage ────────────────────────────────────────────────────────────
def attendre_sante(client: ClientMetabase) -> None:
    """Attend que /api/health réponde {"status": "ok"} — Metabase (JVM) met
    plusieurs secondes à démarrer, contrairement à ClickHouse."""
    for _ in range(TENTATIVES_SANTE_MAX):
        try:
            etat = client.get("/api/health")
        except ErreurMetabase:
            etat = None
        if etat and etat.get("status") == "ok":
            return
        time.sleep(INTERVALLE_SANTE_S)
    raise ErreurRestitution(
        "Metabase ne répond pas sur /api/health après "
        f"{TENTATIVES_SANTE_MAX * INTERVALLE_SANTE_S}s. "
        "Correction : `docker compose up -d metabase`, puis "
        "`docker compose logs metabase` pour la cause."
    )


def authentifier(client: ClientMetabase) -> None:
    """Ouvre une session administrateur, en posant l'admin au tout premier passage.

    Voir la note d'en-tête : "setup-token" n'indique PAS de façon fiable si
    Metabase est déjà configuré (il reste non nul après coup). On tente donc
    d'abord une connexion normale ; ce n'est qu'à son échec qu'on regarde si
    le setup reste à faire.
    """
    email = exiger("MB_ADMIN_EMAIL")
    mdp = exiger("MB_ADMIN_PASSWORD")
    try:
        reponse = client.post("/api/session", {"username": email, "password": mdp})
        client.session_id = reponse["id"]
        LOG.info("session administrateur ouverte")
        return
    except ErreurMetabase:
        pass

    proprietes = client.get("/api/session/properties")
    if proprietes.get("has-user-setup"):
        raise ErreurRestitution(
            "Metabase est déjà configuré mais la connexion admin a été "
            "refusée. Vérifiez MB_ADMIN_EMAIL / MB_ADMIN_PASSWORD dans .env."
        )
    jeton = proprietes.get("setup-token")
    if not jeton:
        raise ErreurRestitution("jeton de configuration introuvable (setup-token nul).")

    reponse = client.post(
        "/api/setup",
        {
            "token": jeton,
            "user": {"email": email, "password": mdp, "first_name": "Admin", "last_name": "EDS"},
            "prefs": {"site_name": "EDS CHU"},
        },
    )
    client.session_id = reponse["id"]
    LOG.info("administrateur Metabase créé (premier démarrage)")


def client_authentifie() -> ClientMetabase:
    """Point d'entrée réutilisable pour les modules suivants (tableaux de bord) :
    un client prêt à l'emploi, sans qu'ils aient à connaître l'attente de
    santé ni la bascule setup/connexion."""
    client = ClientMetabase()
    attendre_sante(client)
    authentifier(client)
    return client


# ── connexions ClickHouse ───────────────────────────────────────────────
def provisionner_connexion(
    client: ClientMetabase, nom: str, utilisateur_ch: str, mdp_ch: str, base_ch: str
) -> int:
    """Trouve ou crée la connexion `nom`, la met à jour si elle existe déjà,
    puis attend que son schéma soit synchronisé (sinon les questions créées
    ensuite ne trouveraient aucun champ).

    Contrairement aux groupes (noms uniques côté serveur), les bases n'ont
    aucune contrainte d'unicité sur le nom : un doublon EST possible — il en
    existait un sur cette instance avant l'écriture de ce module, résidu
    d'essais manuels antérieurs. Il est purgé ici (le plus ancien devient la
    référence) : c'est ce qui rend la fonction idempotente même en partant
    d'un état déjà incohérent.
    """
    details = {
        "host": HOTE_CLICKHOUSE,
        "port": PORT_CLICKHOUSE,
        "user": utilisateur_ch,
        "password": mdp_ch,
        "dbname": base_ch,
        "ssl": False,
    }
    correspondances = sorted(
        (b for b in client.get("/api/database")["data"] if b["name"] == nom),
        key=lambda b: b["id"],
    )
    if len(correspondances) > 1:
        LOG.warning(
            "%d connexions en double pour %r — purge des surnuméraires (id %s conservé)",
            len(correspondances),
            nom,
            correspondances[0]["id"],
        )
        for doublon in correspondances[1:]:
            client.delete(f"/api/database/{doublon['id']}")

    if correspondances:
        base_id = correspondances[0]["id"]
        client.put(f"/api/database/{base_id}", {"engine": "clickhouse", "name": nom, "details": details})
        LOG.info("connexion mise à jour : %s (id=%s)", nom, base_id)
    else:
        cree = client.post("/api/database", {"engine": "clickhouse", "name": nom, "details": details})
        base_id = cree["id"]
        LOG.info("connexion créée : %s (id=%s)", nom, base_id)

    client.post(f"/api/database/{base_id}/sync_schema")
    _attendre_synchronisation(client, base_id, nom)
    return base_id


def _attendre_synchronisation(client: ClientMetabase, base_id: int, nom: str) -> None:
    for _ in range(TENTATIVES_SYNC_MAX):
        metadonnees = client.get(f"/api/database/{base_id}/metadata")
        if metadonnees.get("tables"):
            return
        time.sleep(INTERVALLE_SYNC_S)
    raise ErreurRestitution(
        f"la connexion {nom!r} (id {base_id}) n'expose aucune table après "
        f"{TENTATIVES_SYNC_MAX * INTERVALLE_SYNC_S}s. Vérifiez les identifiants "
        "ClickHouse et que le compte a bien SELECT sur au moins une table."
    )


# ── groupes et utilisateurs ─────────────────────────────────────────────
def groupe_magique(client: ClientMetabase, type_magique: str) -> int:
    """Identifiant d'un groupe interne à Metabase ("all-internal-users",
    "admin"...) — jamais supposé fixe (id 1 / 2), toujours relu."""
    groupes = client.get("/api/permissions/group")
    trouve = next((g for g in groupes if g.get("magic_group_type") == type_magique), None)
    if trouve is None:
        raise ErreurRestitution(f"groupe magique introuvable : {type_magique!r}")
    return trouve["id"]


def trouver_ou_creer_groupe(client: ClientMetabase, nom: str) -> int:
    """Les noms de groupe sont uniques côté serveur (vérifié : une création
    en double est refusée en HTTP 400) — la recherche préalable suffit donc
    à garantir l'idempotence, sans purge nécessaire."""
    groupes = client.get("/api/permissions/group")
    trouve = next((g for g in groupes if g["name"] == nom), None)
    if trouve is not None:
        return trouve["id"]
    cree = client.post("/api/permissions/group", {"name": nom})
    LOG.info("groupe créé : %s (id=%s)", nom, cree["id"])
    return cree["id"]


def trouver_ou_creer_utilisateur(
    client: ClientMetabase, email: str, prenom: str, nom_famille: str, mdp: str, groupe_id: int, tous_id: int
) -> int:
    """Trouve ou crée l'utilisateur, puis repose systématiquement son
    appartenance à exactement {All Users, `groupe_id`} — même après une
    relance, même si elle a été modifiée à la main entre deux exécutions.

    Le mot de passe n'est posé qu'À LA CRÉATION : vérifié empiriquement,
    PUT /api/user/:id ignore silencieusement un champ "password" (voir la
    note d'en-tête). Le rejouer n'aurait donc aucun effet, à part suggérer à
    tort qu'il resynchronise le mot de passe avec .env.
    """
    utilisateurs = client.get("/api/user")["data"]
    trouve = next((u for u in utilisateurs if u["email"] == email), None)
    if trouve is not None:
        utilisateur_id = trouve["id"]
    else:
        cree = client.post(
            "/api/user",
            {"first_name": prenom, "last_name": nom_famille, "email": email, "password": mdp},
        )
        utilisateur_id = cree["id"]
        LOG.info("utilisateur créé : %s (id=%s)", email, utilisateur_id)

    client.put(
        f"/api/user/{utilisateur_id}",
        {"user_group_memberships": [{"id": tous_id}, {"id": groupe_id}]},
    )
    return utilisateur_id


# ── graphe de permissions ───────────────────────────────────────────────
def poser_permissions(
    client: ClientMetabase,
    tous_id: int,
    pilotage_gid: int,
    recherche_gid: int,
    pilotage_db_id: int,
    recherche_db_id: int,
) -> None:
    """Cloisonne l'accès aux deux bases gold dans le graphe de permissions.

    "blocked" (blocage total) est réservé à l'édition payante — vérifié en
    l'expérimentant contre l'instance (voir la note d'en-tête). La valeur
    libre la plus proche pour un refus est "legacy-no-self-service" : le
    groupe ne peut ni parcourir la base, ni y construire de requête. Le
    cloisonnement réel reste porté par le compte ClickHouse borné ; ce réglage
    évite en plus qu'un utilisateur de Pilotage se voie proposer la connexion
    Recherche dans l'interface.

    Le format du graphe (`groups.<id_groupe>.<id_base>.<clés>`) a été lu sur
    la réponse de GET, pas deviné : on part de cette réponse et on ne
    modifie QUE les couples (groupe, base) qui nous concernent, pour ne rien
    perturber d'éventuels réglages déjà posés ailleurs (base d'exemple,
    groupe Administrators).
    """
    acces = {
        "view-data": "unrestricted",
        "create-queries": "query-builder-and-native",
        "download": {"schemas": "full"},
    }
    refus = {"view-data": "legacy-no-self-service"}

    plan = {
        tous_id: {pilotage_db_id: refus, recherche_db_id: refus},
        pilotage_gid: {pilotage_db_id: acces, recherche_db_id: refus},
        recherche_gid: {recherche_db_id: acces, pilotage_db_id: refus},
    }

    graphe = client.get("/api/permissions/graph")
    for groupe_id, par_base in plan.items():
        cible = graphe["groups"].setdefault(str(groupe_id), {})
        for base_id, regle in par_base.items():
            cible[str(base_id)] = dict(regle)

    reponse = client.put("/api/permissions/graph", graphe)
    LOG.info("graphe de permissions posé (révision %s)", reponse.get("revision"))


# ── collections ──────────────────────────────────────────────────────────
def trouver_ou_creer_collection(client: ClientMetabase, nom: str) -> int:
    """Trouve ou crée la collection `nom`, en purgeant les doublons éventuels.

    Comme les bases (et contrairement aux groupes), les collections n'ont
    aucune contrainte d'unicité de nom côté serveur — la purge est donc
    nécessaire à l'idempotence, sur le même principe que `provisionner_connexion`.
    """
    correspondances = sorted(
        (
            c for c in client.get("/api/collection")
            if c["id"] != "root" and not c.get("archived") and c["name"] == nom
        ),
        key=lambda c: c["id"],
    )
    if len(correspondances) > 1:
        LOG.warning(
            "%d collections en double pour %r — purge des surnuméraires (id %s conservé)",
            len(correspondances), nom, correspondances[0]["id"],
        )
        for doublon in correspondances[1:]:
            client.put(f"/api/collection/{doublon['id']}", {"archived": True})
    if correspondances:
        return correspondances[0]["id"]
    cree = client.post("/api/collection", {"name": nom, "color": COULEUR_COLLECTION})
    LOG.info("collection créée : %s (id=%s)", nom, cree["id"])
    return cree["id"]


def poser_permissions_collections(
    client: ClientMetabase,
    tous_id: int,
    pilotage_gid: int,
    recherche_gid: int,
    pilotage_col_id: int,
    recherche_col_id: int,
) -> None:
    """Cloisonne la VISIBILITÉ des tableaux de bord dans le navigateur de
    collections Metabase — un compte de Pilotage ne doit pas même apercevoir
    le tableau de bord Recherche dans sa liste, pas seulement se voir
    refuser l'exécution de ses questions.

    Défense en profondeur, exactement comme `poser_permissions` pour les
    bases (même structure de graphe, même endpoint en paire GET/PUT) : la
    frontière réelle reste le compte ClickHouse borné et le graphe
    `/api/permissions/graph` déjà posé, qui refusent de toute façon
    l'exécution d'une requête sur la mauvaise base, quelle que soit la
    collection qui l'affiche.
    """
    graphe = client.get("/api/collection/graph")
    plan = {
        tous_id: {pilotage_col_id: "none", recherche_col_id: "none"},
        pilotage_gid: {pilotage_col_id: "write", recherche_col_id: "none"},
        recherche_gid: {recherche_col_id: "write", pilotage_col_id: "none"},
    }
    for groupe_id, par_collection in plan.items():
        cible = graphe["groups"].setdefault(str(groupe_id), {})
        for collection_id, droit in par_collection.items():
            cible[str(collection_id)] = droit
    reponse = client.put("/api/collection/graph", graphe)
    LOG.info("graphe de permissions (collections) posé (révision %s)", reponse.get("revision"))


# ── questions et tableaux de bord ───────────────────────────────────────
#
# Chaque question est une requête SQL NATIVE (dataset_query.type = "native")
# sur la base correspondante — plus lisible et plus vérifiable qu'un
# query-builder, et cela garantit que le chiffre affiché est celui de la
# table gold, pas une agrégation recomposée côté Metabase.
#
# Disposition (grille Metabase, 24 colonnes de large) : cartes de synthèse
# en haut, séries temporelles ensuite, tables de détail en bas. Les couples
# DMS et réadmission opposent chacun un graphique de synthèse (barres
# horizontales) à une table de détail plutôt que d'entasser moyenne,
# médiane, P90 et maximum dans un seul graphique à barres groupées : à 8
# services, ce dernier deviendrait illisible, alors que la table affiche les
# quatre valeurs sans perte et reste facile à lire.
TEXTE_ENTETE_PILOTAGE = """## Pilotage hospitalier

Ce tableau de bord répond aux quatre indicateurs nommés du §4 du sujet — durée
moyenne de séjour, passages aux urgences, réadmission à 30 jours, alertes de
monitoring — et aux vues complémentaires d'occupation, de case-mix et
d'origine géographique des séjours.

**Source** : couche `gold_pilotage` de l'entrepôt — des agrégats déjà
construits, jamais les faits bruts ni une identité patient. Le compte
Metabase qui l'interroge (`eds_pilotage`) ne voit que cette base : consulter
ce tableau de bord n'ouvre aucun accès à la recherche clinique."""

def _viz_pourcentage(colonne: str) -> dict:
    """Réglage de formatage pour une carte scalaire dont la colonne SQL
    exprime déjà un pourcentage en valeur 0-100 (ex. `round(100 * a / b, 2)`).

    "number_style": "percent" ferait le contraire de ce qu'on veut ici : ce
    style attend une FRACTION (0-1) et la multiplie par 100 à l'affichage —
    vérifié en pratique contre cette instance, l'appliquer à une colonne déjà
    en 0-100 afficherait ainsi « 1159 % » au lieu de « 11,59 % ». La valeur
    est donc laissée telle quelle et seul un suffixe « % » est ajouté, sur la
    colonne nommée (clé `["name", colonne]`, la seule adressable pour une
    question SQL native — il n'existe pas d'identifiant de champ ici).
    """
    return {"column_settings": {json.dumps(["name", colonne]): {"suffix": " %"}}}


TEXTE_ALERTES_MENTION = """**Seuls deux services sont équipés d'un monitoring
continu : Cardiologie et Réanimation.** Le reste de l'hôpital n'apparaît dans
aucun relevé — l'absence de courbe pour un autre service n'est donc pas une
panne de collecte, mais l'absence de l'équipement lui-même. Le taux affiché
ci-contre ne porte que sur les deux services suivis."""

TEXTE_MORTALITE_RESERVE = """**Réserve méthodologique.** Dans ce jeu de
données synthétique, le mode de sortie de chaque séjour est tiré
uniformément au hasard, indépendamment du service et de la pathologie. Le
taux affiché ci-contre — y compris un chiffre élevé en pédiatrie — **n'a donc
aucune portée clinique** : il ne mesure ni la qualité des soins ni la
gravité des patients pris en charge. Il est publié tel quel, avec cette
réserve, par fidélité à ce que contient réellement l'entrepôt."""

TEXTE_RGPD_RECHERCHE = """## Règles de diffusion (RGPD)

**Seuil de petit effectif.** Aucune cohorte de moins de 5 patients n'est
diffusée dans cette base : le filtre `k ≥ 5` est appliqué à l'ÉCRITURE de
`coh_prevalence` et de `coh_description`, il n'y a donc rien à masquer à la
lecture — une requête ne peut pas contourner une ligne qui n'existe pas. Le
seuil agit à deux granularités différentes, visibles séparément ici :

- **Par pathologie entière.** La mucoviscidose (E84) et la trisomie 21 (Q90)
  ont un effectif total sous le seuil : elles n'apparaissent dans AUCUNE des
  deux tables, ni en prévalence ni en description.
- **Par cellule.** L'amyotrophie spinale (G12) franchit le seuil globalement
  — elle figure donc dans « Prévalence par pathologie » — mais chacune de
  ses sept cellules pathologie × tranche d'âge × sexe reste individuellement
  sous 5 patients : elle est donc totalement absente de la description de
  cohorte, sans qu'aucune ligne isolée ne le laisse deviner.

Au total, **treize cellules** sont retenues par le seuil : les sept de
l'amyotrophie spinale, plus les trois de chacune des deux pathologies
ci-dessus — dont l'effectif global, déjà sous le seuil, ne pouvait de toute
façon fournir aucune cellule diffusable.

**Granularité de l'âge.** L'âge n'est jamais exposé de façon fine : seule la
tranche décennale (`tranche_age`) est diffusée, jamais l'année de naissance
ni un âge exact."""


def _cartes_pilotage() -> tuple[dict, ...]:
    """Spécification des cartes du tableau de bord « Pilotage hospitalier ».

    Deux types d'entrées : une QUESTION (SQL natif sur `gold_pilotage`, posée
    comme une carte `/api/card` à part entière, vérifiable seule) ou un TEXTE
    (une carte virtuelle qui n'existe que dans la mise en page du tableau de
    bord — sans `/api/card` associé, donc rien à interroger pour elle).
    """
    return (
        dict(type="texte", texte=TEXTE_ENTETE_PILOTAGE, row=0, col=0, size_x=16, size_y=4),
        dict(
            type="question",
            nom="Dernière construction de l'entrepôt (couche gold)",
            description=(
                "Horodatage de la dernière exécution du pipeline ayant reconstruit "
                "les tables d'indicateurs — les huit tables kpi_* sont bâties dans "
                "la même passe, une seule suffit à dater l'ensemble."
            ),
            sql="SELECT max(_built_at) AS derniere_construction FROM kpi_dms_service",
            display="scalar", viz={},
            row=0, col=16, size_x=8, size_y=4,
        ),
        dict(
            type="question", nom="Nombre de séjours",
            description=(
                "Total des séjours enregistrés, tous services confondus — le "
                "dénominateur du taux de réadmission ci-dessous."
            ),
            sql="SELECT sum(nb_sejours) AS nombre_de_sejours FROM kpi_readmission_service",
            display="scalar", viz={},
            row=4, col=0, size_x=6, size_y=4,
        ),
        dict(
            type="question", nom="DMS globale (toutes durées confondues)",
            description=(
                "Durée moyenne de séjour pondérée par le nombre de séjours clos "
                "de chaque service, tous services confondus."
            ),
            sql=(
                "SELECT round(sum(dms_jours * nb_sejours_clos) / sum(nb_sejours_clos), 2) "
                "AS dms_jours FROM kpi_dms_service"
            ),
            display="scalar", viz={},
            row=4, col=6, size_x=6, size_y=4,
        ),
        dict(
            type="question", nom="Taux de réadmission à 30 jours (brut)",
            description=(
                "Part des séjours suivis d'une réadmission dans les 30 jours, tous "
                "motifs confondus — l'indicateur de référence du §4, sans ajustement."
            ),
            sql=(
                "SELECT round(100 * sum(nb_readmis_30j_brut) / sum(nb_sejours), 2) "
                "AS taux_readmission_pct FROM kpi_readmission_service"
            ),
            display="scalar", viz=_viz_pourcentage("taux_readmission_pct"),
            row=4, col=12, size_x=6, size_y=4,
        ),
        dict(
            type="question", nom="Taux de relevés en alerte",
            description=(
                "Part des relevés de monitoring qualifiés en alerte, calculée sur "
                "les deux seuls services équipés (Cardiologie, Réanimation)."
            ),
            sql=(
                "SELECT round(100 * sum(nb_en_alerte) / sum(nb_releves), 2) "
                "AS taux_alerte_pct FROM kpi_alertes_jour"
            ),
            display="scalar", viz=_viz_pourcentage("taux_alerte_pct"),
            row=4, col=18, size_x=6, size_y=4,
        ),
        dict(
            type="question", nom="DMS par service",
            description=(
                "Durée moyenne de séjour, en jours, par service — triée du plus "
                "long séjour moyen au plus court."
            ),
            sql="SELECT service, dms_jours FROM kpi_dms_service ORDER BY dms_jours DESC",
            display="row",
            viz={
                "graph.dimensions": ["service"], "graph.metrics": ["dms_jours"],
                "graph.x_axis.title_text": "DMS (jours)", "graph.y_axis.title_text": "Service",
            },
            row=8, col=0, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Dispersion des durées de séjour par service",
            description=(
                "Médiane, 90ᵉ centile et durée maximale par service — la moyenne "
                "seule masque l'étalement des durées de séjour."
            ),
            sql=(
                "SELECT service, dms_jours AS moyenne_jours, mediane_jours, p90_jours, "
                "max_jours, nb_sejours_clos FROM kpi_dms_service ORDER BY dms_jours DESC"
            ),
            display="table", viz={},
            row=8, col=12, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Passages aux urgences (service des urgences), par jour",
            description=(
                "Nombre quotidien de passages dans le SERVICE des urgences — à ne "
                "pas confondre avec le mode d'admission compté séparément ci-après."
            ),
            sql="SELECT jour, nb_passages_urgences FROM kpi_urgences_jour ORDER BY jour",
            display="line",
            viz={
                "graph.dimensions": ["jour"], "graph.metrics": ["nb_passages_urgences"],
                "graph.x_axis.title_text": "Jour", "graph.y_axis.title_text": "Passages aux urgences",
            },
            row=16, col=0, size_x=12, size_y=7,
        ),
        dict(
            type="question", nom="Admissions en urgence (mode d'admission), tous services, par jour",
            description=(
                "Nombre quotidien de séjours dont le MODE D'ADMISSION est « urgence », "
                "tous services confondus — un décompte distinct du précédent."
            ),
            sql="SELECT jour, nb_admissions_en_urgence FROM kpi_urgences_jour ORDER BY jour",
            display="line",
            viz={
                "graph.dimensions": ["jour"], "graph.metrics": ["nb_admissions_en_urgence"],
                "graph.x_axis.title_text": "Jour", "graph.y_axis.title_text": "Admissions en urgence",
            },
            row=16, col=12, size_x=12, size_y=7,
        ),
        dict(
            type="question", nom="Réadmission à 30 jours par service (taux brut)",
            description="Taux de réadmission brut par service, du plus élevé au plus faible.",
            sql="SELECT service, taux_brut_pct FROM kpi_readmission_service ORDER BY taux_brut_pct DESC",
            display="row",
            viz={
                "graph.dimensions": ["service"], "graph.metrics": ["taux_brut_pct"],
                "graph.x_axis.title_text": "Taux de réadmission (%)", "graph.y_axis.title_text": "Service",
            },
            row=23, col=0, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Réadmission à 30 jours par service — numérateur et dénominateur",
            description=(
                "Nombre de réadmissions à 30 jours et nombre total de séjours, par "
                "service — le détail derrière le taux brut."
            ),
            sql=(
                "SELECT service, nb_readmis_30j_brut, nb_sejours, taux_brut_pct "
                "FROM kpi_readmission_service ORDER BY taux_brut_pct DESC"
            ),
            display="table", viz={},
            row=23, col=12, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Relevés en alerte par jour",
            description=(
                "Taux quotidien de relevés de monitoring en alerte, calculé sur les "
                "deux services équipés d'un monitoring continu."
            ),
            sql=(
                "SELECT jour, round(100 * sum(nb_en_alerte) / sum(nb_releves), 2) AS taux_pct "
                "FROM kpi_alertes_jour GROUP BY jour ORDER BY jour"
            ),
            display="line",
            viz={
                "graph.dimensions": ["jour"], "graph.metrics": ["taux_pct"],
                "graph.x_axis.title_text": "Jour", "graph.y_axis.title_text": "Relevés en alerte (%)",
            },
            row=31, col=0, size_x=16, size_y=7,
        ),
        dict(type="texte", texte=TEXTE_ALERTES_MENTION, row=31, col=16, size_x=8, size_y=7),
        dict(
            type="question", nom="Occupation par jour et par service",
            description=(
                "Nombre de patients présents chaque jour, par service — vue "
                "empilée de l'occupation globale de l'hôpital."
            ),
            sql="SELECT jour, service, nb_presents FROM kpi_occupation_jour ORDER BY jour",
            display="area",
            viz={
                "graph.dimensions": ["jour", "service"], "graph.metrics": ["nb_presents"],
                "stackable.stack_type": "stacked",
                "graph.x_axis.title_text": "Jour", "graph.y_axis.title_text": "Patients présents",
            },
            row=39, col=0, size_x=24, size_y=8,
        ),
        dict(
            type="question", nom="Case-mix par service",
            description=(
                "Part de chaque pathologie dans les séjours du service, en % — la "
                "somme fait 100 pour chaque service."
            ),
            sql=(
                "SELECT service, pathologie, nb_sejours, part_pct FROM kpi_casemix_service "
                "ORDER BY service, part_pct DESC"
            ),
            display="table", viz={},
            row=47, col=0, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Origine géographique des séjours, par service",
            description=(
                "Part des séjours de chaque service selon le département de "
                "résidence du patient, en %."
            ),
            sql=(
                "SELECT service, region_code, nb_sejours, nb_patients, part_pct "
                "FROM kpi_origine_service ORDER BY service, part_pct DESC"
            ),
            display="table", viz={},
            row=47, col=12, size_x=12, size_y=8,
        ),
        dict(
            type="question", nom="Mortalité par service",
            description=(
                "Taux de sorties par décès, par service — voir la réserve "
                "méthodologique ci-contre avant toute lecture clinique."
            ),
            sql=(
                "SELECT service, nb_deces, nb_sejours_clos, taux_pct "
                "FROM kpi_mortalite_service ORDER BY taux_pct DESC"
            ),
            display="row",
            viz={
                "graph.dimensions": ["service"], "graph.metrics": ["taux_pct"],
                "graph.x_axis.title_text": "Taux de sorties par décès (%)", "graph.y_axis.title_text": "Service",
            },
            row=55, col=0, size_x=12, size_y=8,
        ),
        dict(type="texte", texte=TEXTE_MORTALITE_RESERVE, row=55, col=12, size_x=12, size_y=8),
    )


def _cartes_recherche() -> tuple[dict, ...]:
    """Spécification des cartes du tableau de bord « Recherche clinique »."""
    return (
        dict(
            type="question", nom="Nombre de pathologies diffusables",
            description=(
                "Nombre de pathologies dont la cohorte franchit le seuil RGPD de "
                "5 patients et figure donc dans cette base."
            ),
            sql="SELECT count() AS nombre_de_pathologies FROM coh_prevalence",
            display="scalar", viz={},
            row=0, col=0, size_x=12, size_y=4,
        ),
        dict(
            type="question", nom="Effectif total décrit",
            description=(
                "Somme des effectifs de la description de cohorte (âge × sexe), "
                "après application du seuil de 5 patients par cellule."
            ),
            sql="SELECT sum(nb_patients) AS effectif_total FROM coh_description",
            display="scalar", viz={},
            row=0, col=12, size_x=12, size_y=4,
        ),
        dict(
            type="question", nom="Prévalence par pathologie",
            description=(
                "Nombre de patients par pathologie, tous types de diagnostic "
                "confondus (motif principal, associé ou relié) — l'effectif de référence."
            ),
            sql="SELECT pathologie, nb_patients FROM coh_prevalence ORDER BY nb_patients DESC",
            display="row",
            viz={
                "graph.dimensions": ["pathologie"], "graph.metrics": ["nb_patients"],
                "graph.x_axis.title_text": "Patients", "graph.y_axis.title_text": "Pathologie",
            },
            row=4, col=0, size_x=12, size_y=9,
        ),
        dict(
            type="question", nom="Prévalence par pathologie — référence et motif principal",
            description=(
                "nb_patients compte tous les diagnostics (référence) ; "
                "nb_patients_principal ne compte que les séjours où la pathologie est "
                "le motif principal — les deux ne se confondent pas."
            ),
            sql=(
                "SELECT pathologie, nb_patients, nb_patients_principal, nb_sejours "
                "FROM coh_prevalence ORDER BY nb_patients DESC"
            ),
            display="table", viz={},
            row=4, col=12, size_x=12, size_y=9,
        ),
        dict(
            type="question", nom="Description de cohorte — pyramide des âges",
            description=(
                "Effectif par tranche d'âge décennale et par sexe, toutes pathologies "
                "confondues — hommes en négatif, femmes en positif, pour une lecture "
                "en pyramide."
            ),
            sql=(
                "SELECT tranche_age, sumIf(nb_patients, sexe = 'F') AS femmes, "
                "-sumIf(nb_patients, sexe = 'M') AS hommes FROM coh_description "
                "GROUP BY tranche_age ORDER BY tranche_age"
            ),
            display="row",
            viz={
                "graph.dimensions": ["tranche_age"], "graph.metrics": ["hommes", "femmes"],
                "graph.x_axis.title_text": "Effectif (hommes en négatif)",
                "graph.y_axis.title_text": "Tranche d'âge",
            },
            row=13, col=0, size_x=14, size_y=10,
        ),
        dict(
            type="question", nom="Description de cohorte — détail par pathologie, âge et sexe",
            description=(
                "Effectif de chaque cellule pathologie × tranche d'âge × sexe telle "
                "qu'exposée par la base recherche."
            ),
            sql=(
                "SELECT pathologie, tranche_age, sexe, nb_patients FROM coh_description "
                "ORDER BY pathologie, tranche_age, sexe"
            ),
            display="table", viz={},
            row=13, col=14, size_x=10, size_y=10,
        ),
        dict(type="texte", texte=TEXTE_RGPD_RECHERCHE, row=23, col=0, size_x=24, size_y=7),
    )


def trouver_ou_creer_carte(
    client: ClientMetabase,
    nom: str,
    description: str,
    database_id: int,
    sql: str,
    display: str,
    viz: dict,
    collection_id: int,
) -> int:
    """Trouve ou crée la question `nom`, la met à jour si elle existe déjà.

    Comme les bases et les collections, les cartes n'ont aucune contrainte
    d'unicité de nom côté serveur ; mais chaque nom utilisé dans ce module
    est unique dans tout le projet, donc la recherche par nom suffit à
    l'idempotence, sans purge de doublons — comme pour les groupes.
    """
    corps = {
        "name": nom,
        "description": description,
        "display": display,
        "collection_id": collection_id,
        "dataset_query": {"type": "native", "native": {"query": sql}, "database": database_id},
        "visualization_settings": viz,
    }
    trouvee = next((c for c in client.get("/api/card") if c["name"] == nom), None)
    if trouvee is not None:
        client.put(f"/api/card/{trouvee['id']}", corps)
        return trouvee["id"]
    cree = client.post("/api/card", corps)
    LOG.info("question créée : %s (id=%s)", nom, cree["id"])
    return cree["id"]


def poser_tableau_de_bord(
    client: ClientMetabase,
    nom: str,
    description: str,
    collection_id: int,
    database_id: int,
    cartes: tuple[dict, ...],
) -> tuple[int, dict[str, int]]:
    """Crée ou met à jour le tableau de bord `nom` et l'intégralité de sa
    mise en page, à partir de `cartes` (voir `_cartes_pilotage` /
    `_cartes_recherche`).

    Chaque question SQL est d'abord posée comme une carte indépendante
    (`trouver_ou_creer_carte`), vérifiable seule via `/api/card/:id/query`.
    Le tableau de bord n'en est ensuite que la mise en page : un seul PUT sur
    `/api/dashboard/:id` avec la liste COMPLÈTE des `dashcards` — vérifié en
    pratique contre cette instance : Metabase remplace tout ce qui n'est pas
    dans cette liste, une dashcard omise disparaît purement et simplement. La
    correspondance ancien/nouveau doit donc porter sur la mise en page
    ENTIÈRE à chaque relance, pas seulement sur les cartes ajoutées.

    L'identifiant d'une dashcard existante est retrouvé par sa POSITION
    (row, col), pas par son `card_id` : la mise en page est entièrement
    déterministe ici (calculée par `_cartes_pilotage` / `_cartes_recherche`,
    jamais posée à la souris), donc deux dashcards ne partagent jamais la
    même position — et une carte texte, qui n'a pas de `card_id` propre,
    n'a pas d'autre identité stable à retrouver.
    """
    trouve = next((d for d in client.get("/api/dashboard") if d["name"] == nom), None)
    if trouve is not None:
        dashboard_id = trouve["id"]
        detail = client.get(f"/api/dashboard/{dashboard_id}")
        existantes_par_position = {(dc["row"], dc["col"]): dc["id"] for dc in detail["dashcards"]}
        client.put(f"/api/dashboard/{dashboard_id}", {"description": description, "collection_id": collection_id})
    else:
        cree = client.post(
            "/api/dashboard", {"name": nom, "description": description, "collection_id": collection_id}
        )
        dashboard_id = cree["id"]
        existantes_par_position = {}
        LOG.info("tableau de bord créé : %s (id=%s)", nom, dashboard_id)

    identifiants_cartes: dict[str, int] = {}
    dashcards = []
    prochain_id_temporaire = -1
    for carte in cartes:
        position = (carte["row"], carte["col"])
        if carte["type"] == "question":
            carte_id = trouver_ou_creer_carte(
                client, carte["nom"], carte["description"], database_id,
                carte["sql"], carte["display"], carte["viz"], collection_id,
            )
            identifiants_cartes[carte["nom"]] = carte_id
            viz_dashcard: dict = {}
        else:
            carte_id = None
            viz_dashcard = {"virtual_card": {"display": "text"}, "text": carte["texte"]}

        dashcard_id = existantes_par_position.get(position, prochain_id_temporaire)
        if dashcard_id == prochain_id_temporaire:
            prochain_id_temporaire -= 1
        dashcards.append({
            "id": dashcard_id,
            "card_id": carte_id,
            "row": carte["row"], "col": carte["col"],
            "size_x": carte["size_x"], "size_y": carte["size_y"],
            "series": [], "parameter_mappings": [],
            "visualization_settings": viz_dashcard,
        })

    client.put(f"/api/dashboard/{dashboard_id}", {"dashcards": dashcards})
    LOG.info("mise en page posée : %s (%d cartes)", nom, len(dashcards))
    return dashboard_id, identifiants_cartes


def verifier_cartes(client: ClientMetabase, identifiants: dict[str, int]) -> list[str]:
    """Interroge CHAQUE question via l'API (`/api/card/:id/query`) et vérifie
    qu'elle renvoie des lignes — pas une confiance aveugle dans le
    provisionnement, une vérification de ce que Metabase renvoie vraiment.

    Une question en erreur ne lève PAS d'exception réseau : vérifié en
    pratique, `/api/card/:id/query` répond HTTP 200 avec un champ "error"
    (jamais de "data") quand le SQL sous-jacent échoue — c'est ce champ,
    et l'absence de lignes, qu'il faut lire, pas un code HTTP.

    Volontairement PAS de recoupement contre des chiffres de référence figés
    ici : le jeu de données source n'est pas versionné, et un rechargement
    légitime (`eds.run --tout`) change les agrégats gold sans que le
    provisionnement soit en cause. Ce recoupement chiffré existe ailleurs,
    calculé EN DIRECT contre l'entrepôt : `tests.demontrer restitution`.

    Retourne la liste des non-conformités (vide si tout est conforme).
    """
    echecs: list[str] = []
    for nom, carte_id in identifiants.items():
        try:
            reponse = client.post(f"/api/card/{carte_id}/query", {})
        except ErreurMetabase as erreur:
            echecs.append(f"{nom} (id={carte_id}) : appel en échec — {erreur}")
            continue
        if reponse.get("error"):
            echecs.append(f"{nom} (id={carte_id}) : {reponse['error']}")
            continue
        lignes = (reponse.get("data") or {}).get("rows") or []
        if not lignes:
            echecs.append(f"{nom} (id={carte_id}) : aucune ligne renvoyée")
            continue
    return echecs


def poser_dashboards(
    client: ClientMetabase,
    pilotage_db_id: int,
    recherche_db_id: int,
    tous_id: int,
    pilotage_gid: int,
    recherche_gid: int,
) -> dict[str, int]:
    """Construit les deux tableaux de bord de la Partie 1 du sujet, de façon
    idempotente, puis vérifie chaque question par l'API avant de rendre la
    main : une question qui échoue interrompt le provisionnement plutôt que
    de laisser un tableau de bord silencieusement cassé."""
    pilotage_col_id = trouver_ou_creer_collection(client, NOM_BASE_PILOTAGE)
    recherche_col_id = trouver_ou_creer_collection(client, NOM_BASE_RECHERCHE)
    poser_permissions_collections(
        client, tous_id, pilotage_gid, recherche_gid, pilotage_col_id, recherche_col_id
    )

    pilotage_dash_id, cartes_pilotage = poser_tableau_de_bord(
        client, NOM_BASE_PILOTAGE,
        "Indicateurs d'activité et de qualité des soins, construits sur la couche gold_pilotage.",
        pilotage_col_id, pilotage_db_id, _cartes_pilotage(),
    )
    recherche_dash_id, cartes_recherche = poser_tableau_de_bord(
        client, NOM_BASE_RECHERCHE,
        "Cohortes de recherche anonymisées, filtrées au seuil RGPD de 5 patients — couche gold_recherche.",
        recherche_col_id, recherche_db_id, _cartes_recherche(),
    )

    toutes_cartes = {**cartes_pilotage, **cartes_recherche}
    echecs = verifier_cartes(client, toutes_cartes)
    if echecs:
        for echec in echecs:
            LOG.error("vérification en échec : %s", echec)
        raise ErreurRestitution(
            f"{len(echecs)} question(s) non conforme(s) après provisionnement des tableaux de bord."
        )
    LOG.info(
        "tableaux de bord posés et vérifiés : %d questions contrôlées via l'API, toutes conformes",
        len(toutes_cartes),
    )
    return {
        "pilotage_dashboard_id": pilotage_dash_id,
        "recherche_dashboard_id": recherche_dash_id,
        "pilotage_collection_id": pilotage_col_id,
        "recherche_collection_id": recherche_col_id,
    }


# ── orchestration ────────────────────────────────────────────────────────
# ── contenu d'exemple de Metabase ───────────────────────────────────────
def purger_contenu_exemple(client: ClientMetabase) -> int:
    """Retire la base de démonstration livrée avec Metabase, et elle seule.

    L'image embarque une base H2 « Sample Database », une collection
    « Examples » et un tableau de bord « E-commerce Insights ». Ce contenu
    n'a aucun rapport avec l'entrepôt : le laisser à côté des deux tableaux
    de bord du CHU brouille la lecture, et un lecteur pressé peut prendre une
    courbe de ventes pour un indicateur hospitalier.

    LE DISCRIMINANT EST CELUI DE METABASE, PAS UN NOM. Les objets de
    démonstration portent le drapeau `is_sample`, posé par le serveur.
    S'appuyer dessus plutôt que sur les libellés garantit qu'aucun objet du
    projet ne peut être atteint, même si quelqu'un baptisait un jour une
    collection « Examples » : le filtre ne les verrait pas.

    L'ordre suit les dépendances — tableaux de bord, puis questions, puis
    collection, puis base — pour ne jamais laisser une carte orpheline
    pointant sur une base disparue.

    Idempotent : sur une instance déjà purgée, il n'y a rien à faire.
    """
    supprimes = 0
    # Plusieurs passes : supprimer le tableau de bord de démonstration rend
    # supprimables des questions qui ne l'étaient pas tant qu'il les portait.
    # Un seul passage laissait un reliquat — constaté sur cette instance.
    for _ in range(PASSES_PURGE_MAX):
        supprimes += _purger_une_passe(client)
        if not _reste_du_contenu_exemple(client):
            break
    else:
        raise ErreurRestitution(
            "le contenu d'exemple de Metabase résiste à la purge après "
            f"{PASSES_PURGE_MAX} passes — supprimez-le depuis l'interface "
            "d'administration, ou repartez d'un volume metabase-data neuf."
        )
    if supprimes:
        LOG.info("contenu d'exemple retiré (%d objets)", supprimes)
    return supprimes


def _reste_du_contenu_exemple(client: ClientMetabase) -> bool:
    """Un objet portant `is_sample` subsiste-t-il ?"""
    if any(b.get("is_sample") for b in client.get("/api/database")["data"]):
        return True
    return any(c.get("is_sample") for c in (client.get("/api/collection") or []))


def _purger_une_passe(client: ClientMetabase) -> int:
    """Une passe de suppression, dans l'ordre des dépendances."""
    bases_exemple = {b["id"] for b in client.get("/api/database")["data"] if b.get("is_sample")}
    collections_exemple = {
        c["id"] for c in (client.get("/api/collection") or []) if c.get("is_sample")
    }
    if not bases_exemple and not collections_exemple:
        return 0

    supprimes = 0
    for tableau in client.get("/api/dashboard") or []:
        detail = client.get(f"/api/dashboard/{tableau['id']}")
        if detail.get("collection_id") in collections_exemple:
            client.delete(f"/api/dashboard/{tableau['id']}")
            supprimes += 1

    for carte in client.get("/api/card") or []:
        if carte.get("database_id") in bases_exemple:
            client.delete(f"/api/card/{carte['id']}")
            supprimes += 1

    # Une collection ne se supprime pas : elle s'archive.
    for collection_id in collections_exemple:
        client.put(f"/api/collection/{collection_id}", {"archived": True})
        supprimes += 1

    for base_id in bases_exemple:
        client.delete(f"/api/database/{base_id}")
        supprimes += 1

    return supprimes


def provisionner(client: ClientMetabase) -> dict[str, int]:
    """Exécute l'intégralité du provisionnement, de façon idempotente."""
    LOG.info("provisionnement démarré")

    purger_contenu_exemple(client)

    pilotage_db_id = provisionner_connexion(
        client, NOM_BASE_PILOTAGE, "eds_pilotage", exiger("CH_PILOTAGE_PASSWORD"), "gold_pilotage"
    )
    recherche_db_id = provisionner_connexion(
        client, NOM_BASE_RECHERCHE, "eds_recherche", exiger("CH_RECHERCHE_PASSWORD"), "gold_recherche"
    )

    tous_id = groupe_magique(client, "all-internal-users")
    pilotage_gid = trouver_ou_creer_groupe(client, NOM_GROUPE_PILOTAGE)
    recherche_gid = trouver_ou_creer_groupe(client, NOM_GROUPE_RECHERCHE)

    trouver_ou_creer_utilisateur(
        client, exiger("MB_PILOTAGE_EMAIL"), "Pilotage", "EDS",
        exiger("MB_PILOTAGE_PASSWORD"), pilotage_gid, tous_id,
    )
    trouver_ou_creer_utilisateur(
        client, exiger("MB_RECHERCHE_EMAIL"), "Recherche", "EDS",
        exiger("MB_RECHERCHE_PASSWORD"), recherche_gid, tous_id,
    )

    poser_permissions(client, tous_id, pilotage_gid, recherche_gid, pilotage_db_id, recherche_db_id)

    resultat_dashboards = poser_dashboards(
        client, pilotage_db_id, recherche_db_id, tous_id, pilotage_gid, recherche_gid
    )

    LOG.info("provisionnement terminé")
    return {
        "pilotage_db_id": pilotage_db_id,
        "recherche_db_id": recherche_db_id,
        "pilotage_gid": pilotage_gid,
        "recherche_gid": recherche_gid,
        **resultat_dashboards,
    }


# ── état et récapitulatif ───────────────────────────────────────────────
def afficher_etat(client: ClientMetabase) -> int:
    """Affiche l'état de Metabase sans rien créer ni modifier — y compris
    l'administrateur : une connexion en échec est un simple constat, jamais
    une invite à lancer le setup."""
    try:
        reponse = client.post(
            "/api/session", {"username": exiger("MB_ADMIN_EMAIL"), "password": exiger("MB_ADMIN_PASSWORD")}
        )
        client.session_id = reponse["id"]
    except ErreurMetabase:
        print("\nMetabase n'est pas encore configuré (ou les identifiants admin sont invalides).")
        print("Lancez `python -m eds.restitution` pour provisionner.\n")
        return 0

    print("\nCONNEXIONS")
    for b in sorted(client.get("/api/database")["data"], key=lambda x: x["id"]):
        if b["engine"] == "h2":
            continue  # la base applicative interne de Metabase, hors périmètre
        print(f"   {b['id']:>3}  {b['name']:28} {b['engine']:12} {b.get('initial_sync_status')}")

    print("\nGROUPES")
    for g in sorted(client.get("/api/permissions/group"), key=lambda x: x["id"]):
        print(f"   {g['id']:>3}  {g['name']:20} {g['member_count']} membre(s)")

    print("\nUTILISATEURS")
    for u in sorted(client.get("/api/user")["data"], key=lambda x: x["id"]):
        # La liste expose "group_ids" (une liste d'entiers) ; c'est seulement
        # la fiche individuelle (GET/POST/PUT /api/user/:id) qui expose
        # "user_group_memberships" (une liste de {"id": ...}) — vérifié en
        # pratique, les deux formes coexistent selon l'endpoint.
        groupes_u = ", ".join(str(g) for g in u["group_ids"])
        print(f"   {u['id']:>3}  {u['email']:32} groupes=[{groupes_u}]")

    print()
    return 0


def afficher_recapitulatif(client: ClientMetabase) -> None:
    """Récapitulatif lisible en fin de provisionnement : URL, comptes,
    tableaux de bord (posés par la tâche suivante — listés ici s'ils
    existent déjà, faute de quoi le manque est dit explicitement)."""
    # Les deux tableaux du projet sont les seuls à exister : le contenu de
    # démonstration de Metabase n'est plus chargé (MB_LOAD_SAMPLE_CONTENT) et
    # `purger_contenu_exemple` retire celui d'une instance plus ancienne. Un
    # filtre par nom serait de toute façon fragile — c'est le drapeau
    # `is_sample` qui fait foi, jamais un libellé.
    dashboards = client.get("/api/dashboard") or []

    print("\n" + "=" * 72)
    print("RESTITUTION METABASE — RÉCAPITULATIF")
    print("=" * 72)
    print(f"\nURL              {MB_URL}")
    print("\nCOMPTES (mots de passe dans .env, jamais journalisés) :")
    print(f"   administrateur  {exiger('MB_ADMIN_EMAIL'):32} MB_ADMIN_PASSWORD")
    print(f"   pilotage        {exiger('MB_PILOTAGE_EMAIL'):32} MB_PILOTAGE_PASSWORD")
    print(f"   recherche       {exiger('MB_RECHERCHE_EMAIL'):32} MB_RECHERCHE_PASSWORD")
    print("\nTABLEAUX DE BORD :")
    if dashboards:
        for d in dashboards:
            print(f"   {d['id']:>3}  {d['name']}")
    else:
        print("   (aucun encore créé — objet de la tâche suivante)")
    print()


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        prog="python -m eds.restitution",
        description="Provisionne Metabase (connexions, comptes, droits) par son API.",
    )
    analyseur.add_argument(
        "--etat", action="store_true", help="affiche l'état de Metabase sans rien modifier"
    )
    args = analyseur.parse_args(argv)

    mod_journal.configurer()
    client = ClientMetabase()

    try:
        attendre_sante(client)
        if args.etat:
            return afficher_etat(client)

        authentifier(client)
        provisionner(client)
        afficher_recapitulatif(client)
    except ErreurRestitution as erreur:
        LOG.error("provisionnement interrompu : %s", erreur)
        LOG.error("reprise : corriger la cause puis relancer — l'exécution est idempotente")
        return 1
    except ErreurMetabase as erreur:
        LOG.error("appel Metabase en échec : %s", erreur)
        return 1
    except Exception:
        LOG.critical("échec inattendu", exc_info=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
