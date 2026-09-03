"""Démonstrations exécutables — cinq sections indépendantes.

  cloisonnement  chaque compte accède à sa base et se voit refuser les autres,
                 par le moteur ; droits posés au niveau colonne
  restitution    le même cloisonnement, vu cette fois depuis l'INTERFACE
                 (Metabase, Partie 1 du sujet) : bases et tableaux de bord
                 disjoints par compte, la preuve que la borne tient à la
                 connexion ClickHouse — même un administrateur Metabase ne la
                 franchit pas — et un recoupement chiffré, calculé EN DIRECT
                 contre l'entrepôt, entre ce que Metabase affiche et ce que
                 vaut réellement gold
  reprise        des pannes sont provoquées volontairement, puis on vérifie
                 que l'échec est tracé, l'entrepôt cohérent, la relance suffisante
  qualite        des lignes fautives sont injectées en bronze, puis on vérifie
                 que silver les écarte ou les corrige, et les trace
  effectifs      deux cohortes fabriquées, de part et d'autre du seuil de 5
                 patients, pour vérifier que le filtre RGPD coupe au bon endroit

Le refus est prononcé par ClickHouse lui-même : ce n'est pas une règle
applicative que l'on pourrait contourner en écrivant une autre requête.

    python -m tests.demontrer                  # les cinq sections
    python -m tests.demontrer cloisonnement    # une seule
"""

from __future__ import annotations

import sys

import clickhouse_connect

from eds import choisir_sections, journal as mod_journal
from eds.config import exiger, seuils_alerte
from eds.restitution import (
    MB_URL,
    NOM_BASE_PILOTAGE,
    NOM_BASE_RECHERCHE,
    ClientMetabase,
    ErreurMetabase,
    _cartes_pilotage,
    _cartes_recherche,
)
from eds.run import main as executer_pipeline
from eds.warehouse import client, executer_fichier

VERT, ROUGE, GRIS, RAZ = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def _client(utilisateur: str, mot_de_passe: str):
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username=utilisateur, password=mot_de_passe
    )


def _entete(texte: str) -> None:
    print(f"\n  {texte}")
    print(f"  {'─' * 66}")


# ── Cloisonnement ────────────────────────────────────────────────────────
CIBLES = [
    ("gold_pilotage.fact_sejour", "faits de séjour (pilotage)"),
    ("gold_recherche.coh_prevalence", "cohortes de recherche"),
    ("silver.patients", "détail patient (silver)"),
    ("bronze.sejours", "données brutes (bronze)"),
]

CIBLES_EXPLOITATION = ("bronze.sejours", "quarantaine.rejets", "ops.executions")

INTERDITS_PILOTAGE = (
    ("SELECT patient_pseudo FROM gold_pilotage.fact_sejour LIMIT 1", "lire le pseudonyme patient"),
    ("SELECT uniqExact(patient_pseudo) FROM gold_pilotage.fact_sejour", "dénombrer des patients"),
    ("SELECT stay_id FROM gold_pilotage.fact_sejour LIMIT 1", "identifier un séjour"),
    ("SELECT * FROM gold_pilotage.dim_patient LIMIT 1", "accéder à la dimension patient"),
    ("SELECT * FROM gold_pilotage.fact_diagnostic LIMIT 1", "accéder aux diagnostics"),
    ("SELECT * FROM gold_pilotage.fact_sejour LIMIT 1", "faire un SELECT *"),
)

REQUIS_PILOTAGE = (
    ("SELECT round(avg(duree_jours), 2) FROM gold_pilotage.fact_sejour WHERE est_en_cours = 0",
     "calculer la DMS"),
    ("SELECT countIf(est_urgence) FROM gold_pilotage.fact_sejour",
     "compter les passages aux urgences"),
    ("SELECT countIf(en_alerte) FROM gold_pilotage.fact_releve",
     "compter les relevés en alerte"),
)


def _acces_par_compte(compte: str, mot_de_passe: str, autorise: str) -> list[str]:
    """Vérifie qu'un compte accède à sa base et à AUCUNE autre."""
    _entete(f"Compte {compte}")
    ch = _client(compte, mot_de_passe)
    echecs = []
    for table, libelle in CIBLES:
        attendu_ok = table.startswith(autorise)
        try:
            n = int(ch.command(f"SELECT count() FROM {table}"))
            obtenu, detail = True, f"{n} lignes lues"
        except Exception as e:
            obtenu = False
            detail = "ACCESS_DENIED" if "ACCESS_DENIED" in str(e) else str(e)[:40]
        conforme = obtenu == attendu_ok
        if not conforme:
            echecs.append(f"{compte} -> {table}")
        marque = f"{VERT}✓{RAZ}" if conforme else f"{ROUGE}✗{RAZ}"
        verdict = "AUTORISÉ" if obtenu else "REFUSÉ  "
        print(f"   {marque} {verdict}  {table:32} {GRIS}{libelle:26} {detail}{RAZ}")
    return echecs


def _contenu_recherche() -> list[str]:
    """La base recherche ne doit contenir ni âge fin, ni petit effectif."""
    _entete("Contenu de la base recherche")
    ch = _client("eds_recherche", exiger("CH_RECHERCHE_PASSWORD"))
    echecs = []

    colonnes = {c for (c,) in ch.query(
        "SELECT name FROM system.columns WHERE database = 'gold_recherche'").result_rows}
    for interdite in ("birth_year", "patient_pseudo", "region_code"):
        present = interdite in colonnes
        if present:
            echecs.append(f"colonne {interdite} exposée en recherche")
        marque = f"{ROUGE}✗{RAZ}" if present else f"{VERT}✓{RAZ}"
        print(f"   {marque} colonne '{interdite}' {'PRÉSENTE' if present else 'absente'}")

    for table in ("coh_prevalence", "coh_description"):
        mini = ch.command(f"SELECT min(nb_patients) FROM gold_recherche.{table}")
        conforme = int(mini) >= 5
        if not conforme:
            echecs.append(f"{table} contient une cohorte de {mini} patients")
        marque = f"{VERT}✓{RAZ}" if conforme else f"{ROUGE}✗{RAZ}"
        print(f"   {marque} {table:20} plus petite cohorte = {mini} patients (seuil : 5)")

    print(f"\n   {GRIS}Exemple de ce que voit un chercheur — l'âge n'est jamais fin :{RAZ}")
    for r in ch.query("""SELECT pathologie, tranche_age, sexe, nb_patients
                         FROM gold_recherche.coh_description
                         ORDER BY nb_patients DESC LIMIT 3""").result_rows:
        print(f"     {r[0][:34]:34} {r[1]:>7}  {r[2]}  {r[3]:>4} patients")
    return echecs


def _droits_colonnes() -> list[str]:
    """Un GRANT sur la base entière donnerait `patient_pseudo` et le grain du
    séjour. Les droits sont donc posés colonne par colonne : la direction
    consulte des indicateurs, elle n'a jamais à désigner un patient.

    Le refus ne dépend pas de qui interroge, mais du compte de SERVICE
    employé : quiconque passe par `eds_pilotage` — administrateur compris —
    se voit opposer la même borne, par le moteur.
    """
    _entete("Droits au niveau colonne — compte de pilotage")
    ch = _client("eds_pilotage", exiger("CH_PILOTAGE_PASSWORD"))
    echecs = []

    for requete, libelle in INTERDITS_PILOTAGE:
        try:
            ch.command(requete)
            echecs.append(f"pilotage peut {libelle}")
            print(f"   {ROUGE}✗{RAZ} AUTORISÉ  {libelle}")
        except Exception:
            print(f"   {VERT}✓{RAZ} REFUSÉ    {libelle}")

    for requete, libelle in REQUIS_PILOTAGE:
        try:
            valeur = ch.command(requete)
            print(f"   {VERT}✓{RAZ} POSSIBLE  {libelle} {GRIS}= {valeur}{RAZ}")
        except Exception as erreur:
            echecs.append(f"pilotage ne peut pas {libelle}")
            print(f"   {ROUGE}✗{RAZ} IMPOSSIBLE {libelle} : {str(erreur)[:40]}")
    return echecs


def _compte_exploitation() -> list[str]:
    """L'administration doit remonter à la ligne d'origine — incident, piste
    d'audit, demande d'effacement — sans utiliser le compte du pipeline, qui
    peut créer et supprimer des bases. Un compte distinct, en lecture seule.
    """
    _entete("Compte d'exploitation (investigation technique)")
    ch = _client("eds_exploitation", exiger("CH_EXPLOITATION_PASSWORD"))
    echecs = []

    for table in CIBLES_EXPLOITATION:
        try:
            n = int(ch.command(f"SELECT count() FROM {table}"))
            print(f"   {VERT}✓{RAZ} LECTURE   {table:24} {GRIS}{n} lignes{RAZ}")
        except Exception as erreur:
            echecs.append(f"exploitation ne peut pas lire {table}")
            print(f"   {ROUGE}✗{RAZ} {table} illisible : {str(erreur)[:50]}")

    try:  # moindre privilège : aucune écriture, quelle que soit la requête
        ch.command("TRUNCATE TABLE bronze.sejours")
        echecs.append("exploitation a pu ÉCRIRE — moindre privilège non respecté")
        print(f"   {ROUGE}✗{RAZ} ÉCRITURE  autorisée — moindre privilège non respecté")
    except Exception:
        print(f"   {VERT}✓{RAZ} ÉCRITURE  refusée par le moteur {GRIS}(lecture seule){RAZ}")
    return echecs


def cloisonnement() -> list[str]:
    return (_acces_par_compte("eds_pilotage", exiger("CH_PILOTAGE_PASSWORD"), "gold_pilotage")
            + _acces_par_compte("eds_recherche", exiger("CH_RECHERCHE_PASSWORD"), "gold_recherche")
            + _contenu_recherche()
            + _droits_colonnes()
            + _compte_exploitation())


# ── Restitution (cloisonnement vu depuis l'interface) ───────────────────
# `cloisonnement()` ci-dessus établit le refus au niveau du MOTEUR : un
# compte ClickHouse ne voit que sa base. La Partie 1 du sujet exige la même
# démonstration au niveau de L'INTERFACE — c'est ici, contre l'API Metabase,
# avec les comptes applicatifs posés par `eds.restitution`.
#
# Vérifié EMPIRIQUEMENT contre Metabase 0.58.32, édition gratuite (même
# démarche que l'en-tête de eds/restitution.py) : GET /api/database liste
# TOUJOURS les deux connexions à tout compte connecté — masquer un NOM de
# base ("blocked") exige un jeton premium, refusé en pratique par cette
# instance. Ce que l'édition gratuite borne réellement, et que ① / ②
# vérifient : AUCUNE table de la base étrangère n'est synchronisée pour un
# compte qui n'y a pas droit (`/api/database/:id/metadata` -> `tables: []`),
# et toute requête dessus est refusée par Metabase LUI-MÊME
# ("missing-required-permissions"), avant même d'atteindre ClickHouse.
MESSAGE_MOTEUR = "Not enough privileges"


def _session_metabase(mb: ClientMetabase, email_var: str, mdp_var: str) -> str:
    """Ouvre une session Metabase pour le compte visé par ces deux variables
    d'environnement. Un échec ici signifie que `eds.restitution` n'a pas
    encore créé ce compte — jamais un problème de mot de passe erroné, ceux
    de `.env` sont la seule source (voir `eds.config.exiger`)."""
    reponse = mb.post("/api/session", {"username": exiger(email_var), "password": exiger(mdp_var)})
    return reponse["id"]


def _sur_session(mb: ClientMetabase, session_id: str) -> ClientMetabase:
    """Un second client, même hôte, mais authentifié pour un autre compte —
    pour interroger l'API tour à tour comme pilotage, recherche et
    administrateur sans mélanger leurs sessions."""
    autre = ClientMetabase(mb.base_url)
    autre.session_id = session_id
    return autre


def _requete_native(mb: ClientMetabase, database_id: int, sql: str) -> dict:
    """Exécute une question SQL native à travers Metabase et renvoie sa
    réponse brute — jamais une exception : un refus ("missing-required-
    permissions" côté Metabase, ou un message ClickHouse dans `via`) se lit
    dans le corps JSON, à HTTP 202 (vérifié en pratique, voir plus bas)."""
    return mb.post("/api/dataset", {"type": "native", "native": {"query": sql}, "database": database_id})


def _message_moteur(reponse: dict) -> str:
    """Le message ClickHouse d'un échec de requête native transite dans
    `via[0]["error"]` (le pilote JDBC), jamais dans `error` — ce dernier
    champ ne porte que les refus prononcés par Metabase lui-même."""
    via = reponse.get("via") or [{}]
    return via[0].get("error", "")


def restitution() -> list[str]:
    """Démontre le cloisonnement au niveau applicatif — l'interface, pas
    seulement le moteur. Ne plante jamais si Metabase est éteint ou n'a pas
    encore été provisionné : elle le constate, l'annonce, et échoue
    proprement (la propriété demandée n'a alors pas pu être vérifiée)."""
    mod_journal.configurer()
    mb = ClientMetabase()

    _entete("Disponibilité de Metabase")
    try:
        etat = mb.get("/api/health")
    except ErreurMetabase as erreur:
        print(f"   {ROUGE}✗{RAZ} injoignable sur {MB_URL} : {erreur}")
        print(f"   {GRIS}Démarrez-le (docker compose up -d metabase) puis provisionnez-le "
              f"(python -m eds.restitution) avant de rejouer cette section.{RAZ}\n")
        return ["Metabase injoignable — cloisonnement applicatif non vérifiable"]
    if not etat or etat.get("status") != "ok":
        print(f"   {ROUGE}✗{RAZ} état inattendu : {etat}\n")
        return ["Metabase ne répond pas 'ok' sur /api/health"]
    print(f"   {VERT}✓{RAZ} {MB_URL} répond {{'status': 'ok'}}")

    try:
        mb.session_id = _session_metabase(mb, "MB_ADMIN_EMAIL", "MB_ADMIN_PASSWORD")
        pilotage = _sur_session(mb, _session_metabase(mb, "MB_PILOTAGE_EMAIL", "MB_PILOTAGE_PASSWORD"))
        recherche = _sur_session(mb, _session_metabase(mb, "MB_RECHERCHE_EMAIL", "MB_RECHERCHE_PASSWORD"))
    except ErreurMetabase as erreur:
        print(f"   {ROUGE}✗{RAZ} authentification en échec : {erreur}")
        print(f"   {GRIS}Metabase tourne mais n'est probablement pas encore provisionné — "
              f"lancez python -m eds.restitution.{RAZ}\n")
        return [f"authentification Metabase en échec : {erreur}"]
    admin = mb
    print(f"   {VERT}✓{RAZ} sessions ouvertes : administrateur, pilotage, recherche")

    # Bases et tableaux de bord retrouvés par NOM auprès de l'administrateur,
    # jamais par un identifiant supposé stable — même principe que
    # `eds.restitution` (recherche-avant-usage, pas d'id en dur).
    bases = {b["name"]: b["id"] for b in admin.get("/api/database")["data"] if b["engine"] != "h2"}
    dashboards = {d["name"]: d["id"] for d in admin.get("/api/dashboard")}
    if NOM_BASE_PILOTAGE not in bases or NOM_BASE_RECHERCHE not in bases:
        print(f"   {ROUGE}✗{RAZ} connexions Metabase incomplètes : {sorted(bases)}\n")
        return ["connexions Metabase absentes — lancez python -m eds.restitution"]
    if NOM_BASE_PILOTAGE not in dashboards or NOM_BASE_RECHERCHE not in dashboards:
        print(f"   {ROUGE}✗{RAZ} tableaux de bord Metabase incomplets : {sorted(dashboards)}\n")
        return ["tableaux de bord Metabase absents — lancez python -m eds.restitution"]
    pilotage_db_id, recherche_db_id = bases[NOM_BASE_PILOTAGE], bases[NOM_BASE_RECHERCHE]
    pilotage_dash_id, recherche_dash_id = dashboards[NOM_BASE_PILOTAGE], dashboards[NOM_BASE_RECHERCHE]

    echecs: list[str] = []

    _entete("① ② Chaque compte métier n'accède qu'au CONTENU de sa base")
    for session, nom, id_a_soi, nom_etranger, id_etranger in (
        (pilotage, "pilotage", pilotage_db_id, NOM_BASE_RECHERCHE, recherche_db_id),
        (recherche, "recherche", recherche_db_id, NOM_BASE_PILOTAGE, pilotage_db_id),
    ):
        a_soi = session.get(f"/api/database/{id_a_soi}/metadata").get("tables") or []
        etrangeres = session.get(f"/api/database/{id_etranger}/metadata").get("tables") or []
        ok_a_soi, ok_etranger = len(a_soi) > 0, len(etrangeres) == 0
        if not ok_a_soi:
            echecs.append(f"{nom} : aucune table exposée sur sa propre base")
        if not ok_etranger:
            echecs.append(f"{nom} : {len(etrangeres)} table(s) de {nom_etranger} exposée(s)")
        print(f"   {VERT + '✓' if ok_a_soi else ROUGE + '✗'}{RAZ} {nom:10} voit "
              f"{len(a_soi)} table(s) de sa base")
        print(f"   {VERT + '✓' if ok_etranger else ROUGE + '✗'}{RAZ} {nom:10} voit "
              f"{len(etrangeres)} table(s) de {nom_etranger!r} {GRIS}(attendu 0){RAZ}")

        reponse = _requete_native(session, id_etranger, "SELECT 1")
        bloque_par_metabase = reponse.get("error_type") == "missing-required-permissions"
        if not bloque_par_metabase:
            echecs.append(f"{nom} a pu interroger {nom_etranger} via l'interface")
        print(f"   {VERT + '✓' if bloque_par_metabase else ROUGE + '✗'}{RAZ} {nom:10} requête native "
              f"sur {nom_etranger!r} — {GRIS}{reponse.get('error', '?')}{RAZ}")

    _entete("③ Chacun n'ouvre que SON tableau de bord")
    for session, nom, dash_id, autre_dash_id, autre_nom in (
        (pilotage, "pilotage", pilotage_dash_id, recherche_dash_id, NOM_BASE_RECHERCHE),
        (recherche, "recherche", recherche_dash_id, pilotage_dash_id, NOM_BASE_PILOTAGE),
    ):
        try:
            session.get(f"/api/dashboard/{dash_id}")
            ouvre_le_sien = True
        except ErreurMetabase:
            ouvre_le_sien = False
        if not ouvre_le_sien:
            echecs.append(f"{nom} ne peut pas ouvrir son propre tableau de bord")
        print(f"   {VERT + '✓' if ouvre_le_sien else ROUGE + '✗'}{RAZ} {nom:10} ouvre son tableau de bord")

        try:
            session.get(f"/api/dashboard/{autre_dash_id}")
            refuse = False
        except ErreurMetabase as erreur:
            refuse = "HTTP 403" in str(erreur)
        if not refuse:
            echecs.append(f"{nom} a pu ouvrir le tableau de bord {autre_nom!r}")
        print(f"   {VERT + '✓' if refuse else ROUGE + '✗'}{RAZ} {nom:10} se voit refuser "
              f"{autre_nom!r} {GRIS}(HTTP 403){RAZ}")

    _entete("④ La borne ne vient pas de Metabase — un ADMINISTRATEUR ne la franchit pas non plus")
    for id_connexion, nom_connexion, table_etrangere in (
        (recherche_db_id, NOM_BASE_RECHERCHE, "gold_pilotage.fact_sejour"),
        (pilotage_db_id, NOM_BASE_PILOTAGE, "gold_recherche.coh_prevalence"),
    ):
        reponse = _requete_native(admin, id_connexion, f"SELECT count() FROM {table_etrangere}")
        message = _message_moteur(reponse)
        refuse_par_clickhouse = MESSAGE_MOTEUR in message
        if not refuse_par_clickhouse:
            echecs.append(f"l'administrateur a pu lire {table_etrangere} via la connexion {nom_connexion!r}")
        print(f"   {VERT + '✓' if refuse_par_clickhouse else ROUGE + '✗'}{RAZ} admin, connexion "
              f"{nom_connexion!r} -> {table_etrangere}")
        print(f"        {GRIS}{message.strip() or reponse}{RAZ}")

    _entete("⑤ Aucune des deux connexions n'atteint bronze, silver ou quarantaine")
    for id_connexion, nom_connexion in (
        (pilotage_db_id, NOM_BASE_PILOTAGE), (recherche_db_id, NOM_BASE_RECHERCHE)
    ):
        for table in ("bronze.sejours", "silver.sejours", "quarantaine.rejets"):
            reponse = _requete_native(admin, id_connexion, f"SELECT count() FROM {table}")
            message = _message_moteur(reponse)
            refuse = MESSAGE_MOTEUR in message
            if not refuse:
                echecs.append(f"connexion {nom_connexion!r} atteint {table}")
            print(f"   {VERT + '✓' if refuse else ROUGE + '✗'}{RAZ} {nom_connexion:24} -> {table:22} "
                  f"{GRIS}{'refusé par le moteur' if refuse else 'ACCESSIBLE'}{RAZ}")

    _entete("⑥ Ce que Metabase AFFICHE correspond à l'entrepôt, À L'INSTANT PRÉSENT")
    # Recoupement calculé EN DIRECT contre ClickHouse, jamais contre une
    # constante figée dans le code : le jeu de données source n'est pas
    # versionné (voir `eds.run --tout`) et un rechargement légitime change
    # les agrégats gold — un chiffre en dur romprait cette démonstration à
    # chaque nouveau jeu de données, sans que le provisionnement Metabase
    # soit en cause (c'est pourquoi `eds.restitution.verifier_cartes` ne
    # recoupe plus de chiffres, seulement l'absence d'erreur). On réexécute
    # donc le SQL MÊME de la carte (`_cartes_pilotage` / `_cartes_recherche`,
    # celui posé par `eds.restitution`) directement contre ClickHouse, et on
    # compare au résultat renvoyé par Metabase pour cette carte via l'API.
    sql_par_nom = {
        carte["nom"]: carte["sql"]
        for carte in (*_cartes_pilotage(), *_cartes_recherche())
        if carte["type"] == "question"
    }
    cartes_pilotage = {
        dc["card"]["name"]: dc["card_id"]
        for dc in admin.get(f"/api/dashboard/{pilotage_dash_id}")["dashcards"]
        if dc.get("card_id")
    }
    cartes_recherche = {
        dc["card"]["name"]: dc["card_id"]
        for dc in admin.get(f"/api/dashboard/{recherche_dash_id}")["dashcards"]
        if dc.get("card_id")
    }
    ch_pilotage_db = clickhouse_connect.get_client(
        host="localhost", port=8123,
        username=exiger("CH_ADMIN_USER"), password=exiger("CH_ADMIN_PASSWORD"),
        database="gold_pilotage",
    )
    ch_recherche_db = clickhouse_connect.get_client(
        host="localhost", port=8123,
        username=exiger("CH_ADMIN_USER"), password=exiger("CH_ADMIN_PASSWORD"),
        database="gold_recherche",
    )

    def _valeur_totale(lignes) -> float:
        """Somme la dernière colonne : un scalaire (une ligne) comme une
        série (une ligne par jour, pour les passages aux urgences)."""
        return sum(float(ligne[-1]) for ligne in lignes)

    for nom, ch_base, cartes in (
        ("Nombre de séjours", ch_pilotage_db, cartes_pilotage),
        ("Taux de réadmission à 30 jours (brut)", ch_pilotage_db, cartes_pilotage),
        ("Passages aux urgences (service des urgences), par jour", ch_pilotage_db, cartes_pilotage),
        ("Nombre de pathologies diffusables", ch_recherche_db, cartes_recherche),
    ):
        carte_id = cartes.get(nom)
        if carte_id is None:
            echecs.append(f"{nom} : carte introuvable dans le tableau de bord")
            print(f"   {ROUGE}✗{RAZ} {nom} : carte introuvable dans le tableau de bord")
            continue
        attendu = _valeur_totale(ch_base.query(sql_par_nom[nom]).result_rows)
        reponse = admin.post(f"/api/card/{carte_id}/query", {})
        if reponse.get("error"):
            echecs.append(f"{nom} : la carte échoue — {reponse['error']}")
            print(f"   {ROUGE}✗{RAZ} {nom[:55]:55} carte en erreur : {reponse['error']}")
            continue
        obtenu = _valeur_totale((reponse.get("data") or {}).get("rows") or [])
        conforme = round(obtenu, 2) == round(attendu, 2)
        if not conforme:
            echecs.append(f"{nom} : Metabase affiche {obtenu}, l'entrepôt vaut {attendu}")
        print(f"   {VERT + '✓' if conforme else ROUGE + '✗'}{RAZ} {nom[:55]:55} "
              f"{GRIS}Metabase={obtenu:g}  entrepôt={attendu:g}{RAZ}")

    print()
    return echecs


# ── Reprise sur incident ─────────────────────────────────────────────────
def _volumes(ch) -> dict[str, int]:
    return {t: int(ch.command(f"SELECT count() FROM {t}"))
            for t in ("bronze.sejours", "silver.sejours", "gold_pilotage.fact_sejour")}


def reprise() -> list[str]:
    """Trois pannes provoquées, puis contrôle de l'état et de la relance."""
    mod_journal.configurer()
    ch = client()
    echecs: list[str] = []

    avant = _volumes(ch)
    print(f"\n  {GRIS}État initial : {avant}{RAZ}\n")

    print("  ① Jour de dépôt malformé (validation d'entrée)")
    code = executer_pipeline(["--jour", "pas-une-date"])
    ok = code != 0
    if not ok:
        echecs.append("entrée invalide non rejetée")
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — rejeté avant toute écriture\n")

    print("  ② Jour absent du dépôt du CHU")
    code = executer_pipeline(["--jour", "2099-01-01"])
    ok = code != 0
    if not ok:
        echecs.append("jour inexistant non détecté")
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — "
          f"échec explicite, pas de table vide silencieuse\n")

    print("  ③ Traçabilité de l'échec dans ops.executions")
    # `jour` est NULL pour les étapes non journalières (schema, silver, gold) :
    # sans ce ifNull, l'affichage d'un échec de transformation planterait.
    lignes = ch.query("""
        SELECT etape, ifNull(toString(jour), '—'), statut, substring(message, 1, 58)
        FROM ops.executions WHERE statut = 'echec'
        ORDER BY demarre_a DESC LIMIT 3
    """).result_rows
    if lignes:
        for l in lignes:
            print(f"     {VERT}✓{RAZ} {l[0]:8} {l[1]:12} {l[2]:7} {GRIS}{l[3]}{RAZ}")
    else:
        echecs.append("aucun échec journalisé dans ops.executions")
        print(f"     {ROUGE}✗ aucun échec journalisé{RAZ}")

    print("\n  ④ Cohérence de l'entrepôt après les incidents")
    apres = _volumes(ch)
    intact = avant == apres
    if not intact:
        echecs.append("volumes modifiés par un run en échec")
    print(f"     {VERT if intact else ROUGE}{'inchangé' if intact else 'ALTÉRÉ'}{RAZ} — {apres}")

    print("\n  ⑤ Reprise : une simple relance suffit")
    code = executer_pipeline([])
    ok = code == 0 and _volumes(ch) == avant
    if not ok:
        echecs.append("la relance n'a pas rétabli l'état")
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — entrepôt rétabli à l'identique\n")

    # Le cas qui compte vraiment : un jour à moitié chargé. Le chargement
    # traite les sources l'une après l'autre ; si l'une échoue après les
    # séjours, le jour paraît ingéré alors qu'il lui manque une partition.
    # Une relance qui le sauterait perdrait ces lignes définitivement.
    print("  ⑥ Chargement partiel : la relance retrouve-t-elle la source manquante ?")
    jour, table = "2026-08-27", "bronze.monitoring"
    complet = int(ch.command(
        f"SELECT count() FROM {table} WHERE _jour_depot = toDate('{jour}')"))
    ch.command(f"ALTER TABLE {table} DROP PARTITION '{jour}'")
    ampute = int(ch.command(
        f"SELECT count() FROM {table} WHERE _jour_depot = toDate('{jour}')"))
    print(f"     {GRIS}{table} du {jour} : {complet} -> {ampute} lignes{RAZ}")

    executer_pipeline([])
    retrouve = int(ch.command(
        f"SELECT count() FROM {table} WHERE _jour_depot = toDate('{jour}')"))
    repare = retrouve == complet
    if not repare:
        echecs.append(f"chargement partiel non réparé : {retrouve}/{complet} lignes")
    print(f"     {VERT if repare else ROUGE}{retrouve} lignes{RAZ} — "
          f"{'partition retrouvée sans intervention' if repare else 'PERTE SILENCIEUSE'}\n")
    return echecs


# ── Qualité ──────────────────────────────────────────────────────────────
# Les contrôles de format du §3 — « dates valides, sexe normalisé (M/F) » — ne
# se déclenchent jamais sur les données fournies, qui sont propres. Une règle
# qu'aucune donnée n'exerce n'est pas une preuve : on injecte donc les lignes
# fautives que la source ne contient pas, et on vérifie ce que silver en fait.
JOUR_INJECTION = "2026-08-28"

# Séjour rattaché au patient à date de naissance illisible (DEMO_NAISSANCE_NULL
# ci-dessous) : sans lui, gold n'aurait rien à propager — l'exigence du sujet
# (« age_au_sejour devient NULL, tranche_age 'inconnu' ») ne se vérifie qu'au
# grain du FAIT, jamais sur le seul silver.patients.
STAY_NAISSANCE_NULL = "DEMO_SEJOUR_NAISSANCE_NULL"

# Séjour CLOS et par ailleurs valide (patient réel, dates cohérentes) : sert
# de support aux deux relevés hors fenêtre injectés plus bas. Un séjour
# dédié, distinct de STAY_NAISSANCE_NULL, pour ne pas mélanger deux
# démonstrations qui n'ont rien à voir.
STAY_FENETRE = "DEMO_SEJOUR_FENETRE"

# Séjour à INCOHÉRENCE TEMPORELLE (sortie avant admission), patient réel et
# par ailleurs identifié : démontre que ni son diagnostic ni son relevé ne
# sont écartés — seule silver.sejours l'exclut, cf. décision de l'intervenant
# (21_silver_transform.sql, en-tête).
PATIENT_INCOHERENT = "DEMO_PATIENT_INCOHERENT"
STAY_INCOHERENT = "DEMO_SEJOUR_INCOHERENT"

# Stay_id qui n'existe NULLE PART dans bronze.sejours : démontre le motif
# 'sejour_inconnu', distinct de 'sejour_ecarte' (qui suppose le séjour
# présent, mais sans patient identifié).
STAY_INCONNU = "DEMO_STAY_INCONNU"

# Séjour à ADMISSION ILLISIBLE porté par un patient RÉEL, au birth_year
# connu — distinct de DEMO_ADM_NULL (LIGNES_FAUTIVES ci-dessus, dont le
# patient_pseudo 'demo' n'existe dans aucune table patients). Sert à isoler
# les deux causes indépendantes d'un age_au_sejour manquant sur
# fact_diagnostic : ici c'est la date qui manque, pas le patient. Démontre
# aussi le partitionnement `coalesce(date_admission, 1970-01-01)` de
# fact_diagnostic (colonne Nullable, cf. 30_gold.sql et 31_gold_transform.sql).
PATIENT_ADM_NULL = "DEMO_PATIENT_ADM_NULL"
STAY_ADM_NULL = "DEMO_SEJOUR_ADM_NULL"

# Les actes sont déposés au 29 août, pas au 28 comme les autres sources. Le
# jour de dépôt commande la PARTITION, et la remise en état ne recharge que
# les partitions dont la source existe : injecter un acte au 28 le rendrait
# INDESTRUCTIBLE par `eds.run --tout` — il survivrait à la démonstration.
# L'horodatage de l'acte, lui, reste au 28 : c'est la fenêtre des séjours de
# démonstration, et un acte antérieur à son dépôt est le cas NORMAL ici.
JOUR_INJECTION_ACTES = "2026-08-29"

# Code d'acte absent de la nomenclature CCAM : démontre qu'un code inconnu
# n'écarte PAS l'acte — le perdre effacerait de l'activité réelle et du
# montant facturé. Le libellé vaut 'inconnu', l'acte reste compté.
CODE_CCAM_INCONNU = "ZZZZ999"

LIGNES_FAUTIVES = [
    ("séjour, date d'admission illisible", """
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_ADM_NULL', 'demo', 'CARDIO', NULL, NULL, 0,
                'urgence', 'domicile', toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("séjour, date de sortie non vide et illisible", """
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_SORTIE_KO', 'demo', 'CARDIO', toDateTime('2026-08-28 08:00:00'),
                NULL, 1, 'urgence', 'domicile',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("patient, sexe hors nomenclature", """
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_SEXE_KO', 1980, 'X', '35',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("patient, sexe en minuscule et espacé", """
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_SEXE_CASSE', 1980, '  f  ', '35',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("patient, date de naissance illisible", """
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_NAISSANCE_NULL', NULL, 'F', '35',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("patient, patient_id vide en source (pseudonyme vide)", """
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('', 1980, 'F', '35',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
    ("séjour, patient_id vide en source (pseudonyme vide)", """
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_PATIENT_VIDE', '', 'CARDIO',
                toDateTime('2026-08-28 08:00:00'), toDateTime('2026-08-28 12:00:00'), 0,
                'urgence', 'domicile',
                toDate('{jour}'), 'demo.csv', now(), 'demo')"""),
]


def qualite() -> list[str]:
    """Injecte des lignes fautives en bronze, puis constate le sort de chacune."""
    mod_journal.configurer()
    ch = client()
    echecs: list[str] = []

    def controle(libelle: str, obtenu, attendu) -> None:
        ok = obtenu == attendu
        if not ok:
            echecs.append(libelle)
        detail = f"{obtenu}" if ok else f"{obtenu} (attendu {attendu})"
        print(f"     {VERT if ok else ROUGE}{'✓' if ok else '✗'}{RAZ} {libelle:58} {GRIS}{detail}{RAZ}")

    _entete(f"{len(LIGNES_FAUTIVES)} lignes fautives, injectées en bronze")
    for libelle, sql in LIGNES_FAUTIVES:
        ch.command(sql.format(jour=JOUR_INJECTION))
        print(f"     {GRIS}injectée — {libelle}{RAZ}")

    # Le séjour à pseudonyme vide porte lui aussi un diagnostic : la chute
    # doit se propager, quel que soit le motif qui a écarté le séjour — le
    # mécanisme 'sejour_ecarte' est déjà en place, cette ligne le démontre.
    ch.command(f"""
        INSERT INTO bronze.diagnostics
        (stay_id, code_cim10, type_diag, _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_PATIENT_VIDE', 'E11', 'principal',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectée — diagnostic rattaché au séjour à pseudonyme vide{RAZ}")

    # Séjour réel rattaché au patient à date de naissance illisible : sans
    # lui, gold n'aurait rien à propager (fact_sejour est vide pour ce
    # patient). service_code et dates sont ordinaires — seul l'attribut
    # descriptif du patient est en cause, pas le séjour lui-même.
    ch.command(f"""
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_NAISSANCE_NULL}', 'DEMO_NAISSANCE_NULL', 'CARDIO',
                toDateTime('2026-08-28 08:00:00'), toDateTime('2026-08-28 12:00:00'), 0,
                'programme', 'domicile',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectée — séjour rattaché au patient à date de naissance illisible{RAZ}")

    # Séjour clos et par ailleurs valide (08:00 -> 12:00), qui sert de fenêtre
    # de référence aux deux relevés injectés ci-dessous. Rattaché au patient
    # DEMO_NAISSANCE_NULL, déjà conservé en silver.patients (règle 'corrige'),
    # pour ne pas dépendre d'un patient supplémentaire.
    ch.command(f"""
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_FENETRE}', 'DEMO_NAISSANCE_NULL', 'CARDIO',
                toDateTime('2026-08-28 08:00:00'), toDateTime('2026-08-28 12:00:00'), 0,
                'programme', 'domicile',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectée — séjour clos, fenêtre 08:00 -> 12:00{RAZ}")

    # Deux relevés physiologiquement PLAUSIBLES (pour n'être pas absorbés par
    # 'capteur_hors_plage'), l'un antérieur à l'admission, l'autre postérieur
    # à la sortie du séjour clos ci-dessus : les deux bornes de la fenêtre.
    ch.command(f"""
        INSERT INTO bronze.monitoring
        (stay_id, ts, heart_rate, spo2, temp_c,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES
            ('{STAY_FENETRE}', toDateTime('2026-08-28 07:00:00'), 80, 98, 37.0,
             toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo'),
            ('{STAY_FENETRE}', toDateTime('2026-08-28 13:00:00'), 82, 97, 37.2,
             toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectés — un relevé avant l'admission, un après la sortie{RAZ}")

    # Séjour réel (patient identifié), mais à INCOHÉRENCE TEMPORELLE :
    # discharge_ts < admission_ts. Porte un diagnostic ET un relevé
    # physiologiquement plausibles, pour démontrer que la décision de
    # l'intervenant tient sur les deux faits, pas seulement l'un.
    ch.command(f"""
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{PATIENT_INCOHERENT}', 1975, 'F', '35',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_INCOHERENT}', '{PATIENT_INCOHERENT}', 'CARDIO',
                toDateTime('2026-08-28 12:00:00'), toDateTime('2026-08-28 08:00:00'), 0,
                'urgence', 'domicile',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.diagnostics
        (stay_id, code_cim10, type_diag, _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_INCOHERENT}', 'E11', 'principal',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.monitoring
        (stay_id, ts, heart_rate, spo2, temp_c,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_INCOHERENT}', toDateTime('2026-08-28 09:00:00'), 80, 98, 37.0,
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectés — séjour à incohérence temporelle, avec son diagnostic et son relevé{RAZ}")

    # Un diagnostic et un relevé sur un stay_id ABSENT de bronze.sejours —
    # aucun séjour porteur, quel qu'il soit : motif 'sejour_inconnu'.
    ch.command(f"""
        INSERT INTO bronze.diagnostics
        (stay_id, code_cim10, type_diag, _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_INCONNU}', 'E11', 'principal',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.monitoring
        (stay_id, ts, heart_rate, spo2, temp_c,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_INCONNU}', toDateTime('2026-08-28 09:00:00'), 80, 98, 37.0,
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectés — diagnostic et relevé sur un stay_id absent de bronze.sejours{RAZ}")

    # Séjour à ADMISSION ILLISIBLE (admission_ts NULL), porté par un patient
    # réel au birth_year connu, avec un diagnostic : le séjour lui-même est
    # écarté de silver.sejours (motif date_illisible), mais son diagnostic
    # doit rester en silver — admission_ts NULL recopié, sejour_coherent = 0
    # — et son age_au_sejour doit devenir NULL en gold pour une raison
    # DIFFÉRENTE d'un birth_year manquant.
    ch.command(f"""
        INSERT INTO bronze.patients
        (patient_pseudo, birth_year, sex, region_code,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{PATIENT_ADM_NULL}', 1975, 'F', '35',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.sejours
        (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
         _discharge_illisible, admission_mode, discharge_mode,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_ADM_NULL}', '{PATIENT_ADM_NULL}', 'CARDIO', NULL, NULL, 0,
                'urgence', 'domicile',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    ch.command(f"""
        INSERT INTO bronze.diagnostics
        (stay_id, code_cim10, type_diag, _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('{STAY_ADM_NULL}', 'E11', 'principal',
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injectés — patient réel et séjour à admission illisible, avec diagnostic{RAZ}")

    # Un relevé sur le séjour à pseudonyme vide (DEMO_PATIENT_VIDE) : démontre
    # que 'sejour_ecarte' joue aussi côté monitoring, pas seulement diagnostics.
    ch.command(f"""
        INSERT INTO bronze.monitoring
        (stay_id, ts, heart_rate, spo2, temp_c,
         _jour_depot, _fichier_source, _ingested_at, _run_id)
        VALUES ('DEMO_PATIENT_VIDE', toDateTime('2026-08-28 09:00:00'), 80, 98, 37.0,
                toDate('{JOUR_INJECTION}'), 'demo.csv', now(), 'demo')""")
    print(f"     {GRIS}injecté — relevé rattaché au séjour à pseudonyme vide{RAZ}")

    # ── Actes ────────────────────────────────────────────────────────────
    # Les trois motifs de la source `actes` n'attrapent AUCUNE ligne sur le
    # dépôt fourni (8 112 actes, 0 orphelin, 0 hors fenêtre). Ils sont donc
    # exercés ici, sur les mêmes séjours de démonstration que le monitoring —
    # aucun séjour supplémentaire n'est fabriqué, les fenêtres et les cas
    # limites existent déjà.
    #
    # Une insertion par cas plutôt qu'une liste VALUES commentée : ClickHouse
    # ne tolère pas de commentaire `--` entre deux tuples de valeurs.
    def _acte(stay_id: str, code: str, heure: str) -> None:
        ch.command(f"""
            INSERT INTO bronze.actes
            (stay_id, code_ccam, acte_ts, _jour_depot, _fichier_source, _ingested_at, _run_id)
            VALUES ('{stay_id}', '{code}', toDateTime('2026-08-28 {heure}'),
                    toDate('{JOUR_INJECTION_ACTES}'), 'demo.csv', now(), 'demo')""")

    # De part et d'autre de la fenêtre 08:00 -> 12:00 : motif 'acte_hors_sejour'.
    _acte(STAY_FENETRE, "DZEA001", "07:00:00")
    _acte(STAY_FENETRE, "DZEA001", "13:00:00")
    # DANS la fenêtre, mais code hors nomenclature : conservé, libellé 'inconnu'.
    _acte(STAY_FENETRE, CODE_CCAM_INCONNU, "10:00:00")
    # stay_id absent de bronze.sejours : motif 'sejour_inconnu'.
    _acte(STAY_INCONNU, "DZEA001", "09:00:00")
    # Séjour présent, mais sans patient identifié : motif 'sejour_ecarte'.
    _acte("DEMO_PATIENT_VIDE", "DZEA001", "09:00:00")
    # Séjour à incohérence temporelle : conservé, la fenêtre ne s'applique pas.
    _acte(STAY_INCOHERENT, "DZEA001", "09:00:00")
    print(f"     {GRIS}injectés — six actes : deux hors fenêtre, un à code inconnu,"
          f" un orphelin, un sans patient, un sur séjour incohérent{RAZ}")

    print("\n  ① Silver est reconstruit sur ce bronze pollué")
    executer_fichier(ch, "21_silver_transform.sql", run_id="demoqualite")

    n = lambda requete: int(ch.command(requete))

    print("\n  ② Les deux dates illisibles sont ÉCARTÉES, et tracées comme telles")
    controle("absentes de silver.sejours",
             n("""SELECT count() FROM silver.sejours
                  WHERE stay_id IN ('DEMO_ADM_NULL', 'DEMO_SORTIE_KO')"""), 0)
    controle("présentes en quarantaine, motif date_illisible",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE cle IN ('DEMO_ADM_NULL', 'DEMO_SORTIE_KO')
                    AND motif = 'date_illisible' AND action = 'ecarte'"""), 2)

    print("\n  ③ La date de sortie corrompue n'est PAS prise pour un séjour en cours")
    controle("DEMO_SORTIE_KO écarté, non compté comme en cours",
             n("""SELECT count() FROM silver.sejours
                  WHERE stay_id = 'DEMO_SORTIE_KO' AND est_en_cours = 1"""), 0)

    print("\n  ④ Le sexe hors nomenclature est CORRIGÉ, la ligne conservée")
    controle("patient conservé, sexe ramené à 'inconnu'",
             n("""SELECT count() FROM silver.patients
                  WHERE patient_pseudo = 'DEMO_SEXE_KO' AND sex = 'inconnu'"""), 1)
    controle("correction tracée en quarantaine",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE cle = 'DEMO_SEXE_KO' AND motif = 'sexe_non_normalise'
                    AND action = 'corrige'"""), 1)

    print("\n  ⑤ Casse et espaces sont redressés, sans rien signaler")
    controle("'  f  ' normalisé en 'F'",
             n("""SELECT count() FROM silver.patients
                  WHERE patient_pseudo = 'DEMO_SEXE_CASSE' AND sex = 'F'"""), 1)
    controle("aucune trace en quarantaine — ce n'est pas une anomalie",
             n("SELECT count() FROM quarantaine.rejets WHERE cle = 'DEMO_SEXE_CASSE'"), 0)

    print("\n  ⑥ La date de naissance illisible est CORRIGÉE, le patient conservé")
    controle("patient conservé, birth_year NULL",
             n("""SELECT count() FROM silver.patients
                  WHERE patient_pseudo = 'DEMO_NAISSANCE_NULL' AND birth_year IS NULL"""), 1)
    controle("correction tracée en quarantaine, motif date_naissance_illisible",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE cle = 'DEMO_NAISSANCE_NULL' AND motif = 'date_naissance_illisible'
                    AND action = 'corrige'"""), 1)

    print("\n  ⑦ Le patient_id vide (pseudonyme vide) ÉCARTE la ligne patient, sans faux patient partagé")
    controle("aucun patient au pseudonyme vide en silver.patients",
             n("SELECT countIf(patient_pseudo = '') FROM silver.patients"), 0)
    controle("présent en quarantaine, motif patient_manquant",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'patients' AND cle = '(vide)'
                    AND motif = 'patient_manquant' AND action = 'ecarte'"""), 1)

    print("\n  ⑧ Le patient_id vide (pseudonyme vide) ÉCARTE le séjour, pas la journée")
    controle("absent de silver.sejours",
             n("SELECT count() FROM silver.sejours WHERE stay_id = 'DEMO_PATIENT_VIDE'"), 0)
    controle("présent en quarantaine, motif patient_manquant",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'sejours' AND cle = 'DEMO_PATIENT_VIDE'
                    AND motif = 'patient_manquant' AND action = 'ecarte'"""), 1)
    # 'sejour_ecarte' ne porte plus que ce cas précis — patient manquant — et
    # non plus toute exclusion de silver.sejours (cf. ⑨ter : un séjour
    # incohérent, lui, ne l'entraîne plus du tout).
    controle("son diagnostic suit via 'sejour_ecarte' (patient manquant)",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'diagnostics' AND cle = 'DEMO_PATIENT_VIDE/E11'
                    AND motif = 'sejour_ecarte' AND action = 'ecarte'"""), 1)
    controle("son relevé suit lui aussi via 'sejour_ecarte' (patient manquant)",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'monitoring'
                    AND cle = 'DEMO_PATIENT_VIDE@2026-08-28 09:00:00'
                    AND motif = 'sejour_ecarte' AND action = 'ecarte'"""), 1)

    print("\n  ⑨ Gold reconstruit sur silver pollué : âge NULL, tranche 'inconnu' pour ce patient")
    # Le sujet exige explicitement la propagation jusqu'aux FAITS, pas
    # seulement jusqu'à silver.patients — reconstruire gold ici, sur les
    # données polluées, est ce qui distingue cette vérification d'une simple
    # lecture de code.
    executer_fichier(ch, "31_gold_transform.sql", run_id="demoqualite", **seuils_alerte())
    controle("dim_patient.birth_year NULL pour ce patient",
             n("""SELECT count() FROM gold_pilotage.dim_patient
                  WHERE patient_pseudo = 'DEMO_NAISSANCE_NULL' AND birth_year IS NULL"""), 1)
    controle("fact_sejour.age_au_sejour NULL pour son séjour",
             n(f"""SELECT count() FROM gold_pilotage.fact_sejour
                  WHERE stay_id = '{STAY_NAISSANCE_NULL}' AND age_au_sejour IS NULL"""), 1)
    controle("fact_sejour.tranche_age 'inconnu' pour son séjour",
             n(f"""SELECT count() FROM gold_pilotage.fact_sejour
                  WHERE stay_id = '{STAY_NAISSANCE_NULL}' AND tranche_age = 'inconnu'"""), 1)
    controle("fact_diagnostic conserve le diagnostic d'un séjour incohérent, sejour_coherent = 0",
             n(f"""SELECT count() FROM gold_pilotage.fact_diagnostic
                  WHERE stay_id = '{STAY_INCOHERENT}' AND sejour_coherent = 0"""), 1)

    print(f"\n  ⑨bis Les deux relevés hors fenêtre du séjour {STAY_FENETRE} sont ÉCARTÉS")
    controle("absents de silver.monitoring",
             n(f"""SELECT count() FROM silver.monitoring WHERE stay_id = '{STAY_FENETRE}'"""), 0)
    controle("présents en quarantaine, motif releve_hors_sejour",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'monitoring' AND cle LIKE '{STAY_FENETRE}@%'
                    AND motif = 'releve_hors_sejour' AND action = 'ecarte'"""), 2)
    controle("le relevé antérieur à l'admission porte le bon détail",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'monitoring' AND cle = '{STAY_FENETRE}@2026-08-28 07:00:00'
                    AND motif = 'releve_hors_sejour'
                    AND detail LIKE '%antérieur à l''admission%'"""), 1)
    controle("le relevé postérieur à la sortie porte le bon détail",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'monitoring' AND cle = '{STAY_FENETRE}@2026-08-28 13:00:00'
                    AND motif = 'releve_hors_sejour'
                    AND detail LIKE '%postérieur à la sortie%'"""), 1)

    print(f"\n  ⑨ter Le séjour {STAY_INCOHERENT} (incohérent) garde son diagnostic ET son relevé")
    # Décision de l'intervenant : la cohérence temporelle du séjour porteur
    # n'écarte ni ses diagnostics ni ses relevés — seul silver.sejours
    # l'exclut. Les deux doivent donc être présents en silver, enrichis du
    # patient/service/admission du séjour, avec sejour_coherent = 0.
    controle("absent de silver.sejours (incohérence temporelle)",
             n(f"SELECT count() FROM silver.sejours WHERE stay_id = '{STAY_INCOHERENT}'"), 0)
    controle("son diagnostic EST en silver, sejour_coherent = 0",
             n(f"""SELECT count() FROM silver.diagnostics
                  WHERE stay_id = '{STAY_INCOHERENT}' AND code_cim10 = 'E11'
                    AND sejour_coherent = 0 AND patient_pseudo = '{PATIENT_INCOHERENT}'
                    AND service_code = 'CARDIO'"""), 1)
    controle("son relevé EST en silver, sejour_coherent = 0 — la fenêtre ne lui est pas appliquée",
             n(f"""SELECT count() FROM silver.monitoring
                  WHERE stay_id = '{STAY_INCOHERENT}'
                    AND sejour_coherent = 0 AND patient_pseudo = '{PATIENT_INCOHERENT}'"""), 1)
    # Le séjour LUI-MÊME est bien en quarantaine (motif incoherence_temporelle,
    # vérifié ci-dessus par son absence de silver.sejours) : seuls son
    # diagnostic et son relevé ne doivent pas l'être.
    controle("ni son diagnostic ni son relevé ne sont en quarantaine",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source IN ('diagnostics', 'monitoring')
                    AND cle LIKE '{STAY_INCOHERENT}%' AND action = 'ecarte'"""), 0)

    print(f"\n  ⑨quater Le stay_id {STAY_INCONNU}, absent de bronze.sejours, est écarté motif 'sejour_inconnu'")
    controle("absent de silver.diagnostics",
             n(f"SELECT count() FROM silver.diagnostics WHERE stay_id = '{STAY_INCONNU}'"), 0)
    controle("absent de silver.monitoring",
             n(f"SELECT count() FROM silver.monitoring WHERE stay_id = '{STAY_INCONNU}'"), 0)
    controle("diagnostic tracé en quarantaine, motif sejour_inconnu",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'diagnostics' AND cle = '{STAY_INCONNU}/E11'
                    AND motif = 'sejour_inconnu' AND action = 'ecarte'"""), 1)
    controle("relevé tracé en quarantaine, motif sejour_inconnu",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'monitoring' AND cle = '{STAY_INCONNU}@2026-08-28 09:00:00'
                    AND motif = 'sejour_inconnu' AND action = 'ecarte'"""), 1)

    print(f"\n  ⑨quinquies Le séjour {STAY_ADM_NULL} (admission illisible) garde son diagnostic,")
    print("             age_au_sejour NULL pour une raison DIFFÉRENTE d'un birth_year manquant")
    # Le séjour est écarté de silver.sejours (motif date_illisible), mais son
    # diagnostic reste en silver, enrichi de admission_ts NULL (recopié tel
    # quel depuis bronze.sejours) et sejour_coherent = 0.
    controle("absent de silver.sejours (admission illisible)",
             n(f"SELECT count() FROM silver.sejours WHERE stay_id = '{STAY_ADM_NULL}'"), 0)
    controle("son diagnostic EST en silver, admission_ts NULL, sejour_coherent = 0",
             n(f"""SELECT count() FROM silver.diagnostics
                  WHERE stay_id = '{STAY_ADM_NULL}' AND code_cim10 = 'E11'
                    AND sejour_coherent = 0 AND admission_ts IS NULL
                    AND patient_pseudo = '{PATIENT_ADM_NULL}'
                    AND service_code = 'CARDIO'"""), 1)
    # Le patient, lui, est parfaitement identifié en dimension : le
    # birth_year connu prouve que ce qui manque ici, c'est la date, pas le
    # patient — la distinction que le contrôle de tests.verifier vérifie
    # désormais explicitement (cf. sa correction dans la même section).
    controle("dim_patient.birth_year CONNU pour ce patient (ce n'est pas la cause du NULL)",
             n(f"""SELECT count() FROM gold_pilotage.dim_patient
                  WHERE patient_pseudo = '{PATIENT_ADM_NULL}' AND birth_year IS NOT NULL"""), 1)
    controle("fact_diagnostic.date_admission NULL pour ce diagnostic",
             n(f"""SELECT count() FROM gold_pilotage.fact_diagnostic
                  WHERE stay_id = '{STAY_ADM_NULL}' AND date_admission IS NULL"""), 1)
    controle("fact_diagnostic.age_au_sejour NULL malgré un birth_year connu",
             n(f"""SELECT count() FROM gold_pilotage.fact_diagnostic
                  WHERE stay_id = '{STAY_ADM_NULL}' AND age_au_sejour IS NULL"""), 1)
    controle("fact_diagnostic.tranche_age 'inconnu' malgré un birth_year connu",
             n(f"""SELECT count() FROM gold_pilotage.fact_diagnostic
                  WHERE stay_id = '{STAY_ADM_NULL}' AND tranche_age = 'inconnu'"""), 1)
    # Vérifie le partitionnement lui-même : `coalesce(NULL, 1970-01-01)`
    # range la ligne dans la partition sentinelle 197001, jamais dans une
    # partition NULL (impossible en ClickHouse) ni perdue.
    controle("partitionné sous la sentinelle 197001 (coalesce(NULL, 1970-01-01))",
             n(f"""SELECT count() FROM gold_pilotage.fact_diagnostic
                  WHERE stay_id = '{STAY_ADM_NULL}'
                    AND toYYYYMM(coalesce(date_admission, toDate('1970-01-01'))) = 197001"""), 1)

    print(f"\n  ⑨sexies Les actes suivent exactement les mêmes règles que les relevés")
    controle("acte antérieur à l'admission écarté, motif acte_hors_sejour",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'actes'
                    AND cle = '{STAY_FENETRE}/DZEA001@2026-08-28 07:00:00'
                    AND motif = 'acte_hors_sejour' AND action = 'ecarte'"""), 1)
    controle("acte postérieur à la sortie écarté, motif acte_hors_sejour",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'actes'
                    AND cle = '{STAY_FENETRE}/DZEA001@2026-08-28 13:00:00'
                    AND motif = 'acte_hors_sejour' AND action = 'ecarte'"""), 1)
    controle("acte sur stay_id absent de bronze.sejours écarté, motif sejour_inconnu",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'actes' AND cle LIKE '{STAY_INCONNU}%'
                    AND motif = 'sejour_inconnu' AND action = 'ecarte'"""), 1)
    controle("acte sur séjour à pseudonyme vide écarté, motif sejour_ecarte",
             n("""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'actes' AND cle LIKE 'DEMO\\_PATIENT\\_VIDE%'
                    AND motif = 'sejour_ecarte' AND action = 'ecarte'"""), 1)
    controle("les trois écartés sont absents de silver.actes",
             n(f"""SELECT count() FROM silver.actes
                  WHERE stay_id IN ('{STAY_INCONNU}', 'DEMO_PATIENT_VIDE')
                     OR (stay_id = '{STAY_FENETRE}'
                         AND acte_ts NOT BETWEEN toDateTime('2026-08-28 08:00:00')
                                             AND toDateTime('2026-08-28 12:00:00'))"""), 0)
    # La décision de l'intervenant vaut pour les actes comme pour les relevés :
    # un séjour temporellement incohérent n'a pas de fenêtre exploitable, son
    # acte est donc conservé sans condition de date.
    controle("acte sur séjour incohérent CONSERVÉ, sejour_coherent = 0",
             n(f"""SELECT count() FROM silver.actes
                  WHERE stay_id = '{STAY_INCOHERENT}' AND sejour_coherent = 0
                    AND patient_pseudo = '{PATIENT_INCOHERENT}'
                    AND service_code = 'CARDIO'"""), 1)
    controle("cet acte n'est PAS en quarantaine",
             n(f"""SELECT count() FROM quarantaine.rejets
                  WHERE source = 'actes' AND cle LIKE '{STAY_INCOHERENT}%'"""), 0)
    # Un code hors nomenclature n'écarte pas l'acte : l'activité et le montant
    # facturé resteraient sinon incomplets, sans que rien ne le signale.
    controle(f"acte à code {CODE_CCAM_INCONNU} CONSERVÉ, libellé 'inconnu'",
             n(f"""SELECT count() FROM silver.actes
                  WHERE code_ccam = '{CODE_CCAM_INCONNU}' AND libelle = 'inconnu'"""), 1)
    controle("le service de l'acte vient du SÉJOUR, pas de l'acte",
             n(f"""SELECT count() FROM silver.actes
                  WHERE stay_id = '{STAY_FENETRE}' AND service_code = 'CARDIO'"""), 1)

    print("\n  ⑩ L'équation de conservation tient malgré les injections")
    for source in ("sejours", "diagnostics", "monitoring", "actes"):
        bronze = n(f"SELECT count() FROM bronze.{source}")
        silver = n(f"SELECT count() FROM silver.{source}")
        ecartes = n(f"""SELECT count() FROM quarantaine.rejets
                       WHERE source = '{source}' AND action = 'ecarte'""")
        controle(f"{source} : {silver} + {ecartes}", silver + ecartes, bronze)

    print("\n  ⑪ Remise en état : le bronze est rechargé depuis le lake, gold reconstruit dessus")
    executer_pipeline(["--tout"])
    controle("aucune ligne de démonstration ne subsiste",
             n("""SELECT (SELECT count() FROM bronze.sejours
                          WHERE stay_id LIKE 'DEMO\\_%')
                       + (SELECT count() FROM bronze.patients
                          WHERE patient_pseudo LIKE 'DEMO\\_%'
                             OR patient_pseudo = '')
                       + (SELECT count() FROM bronze.diagnostics
                          WHERE stay_id LIKE 'DEMO\\_%')
                       + (SELECT count() FROM bronze.monitoring
                          WHERE stay_id LIKE 'DEMO\\_%')
                       + (SELECT count() FROM bronze.actes
                          WHERE stay_id LIKE 'DEMO\\_%')"""), 0)
    # Le code d'acte hors nomenclature n'est porté par aucun stay_id 'DEMO_' :
    # il est injecté sur le séjour de fenêtre, et disparaîtrait donc du
    # contrôle ci-dessus. Il est vérifié séparément.
    controle("le code d'acte hors nomenclature ne subsiste pas non plus",
             n(f"""SELECT count() FROM bronze.actes
                   WHERE code_ccam = '{CODE_CCAM_INCONNU}'"""), 0)
    print()
    return echecs


# ── Petits effectifs ─────────────────────────────────────────────────────
# « En recherche, ne diffusez pas les cohortes de moins de 5 patients. »
#
# Le filtre se déclenche sur les données fournies : deux prévalences (trisomie
# 21, mucoviscidose) et treize cohortes de description sont retirées. Ce qu'elles
# ne montrent pas, c'est OÙ tombe exactement la coupe — sous 5 ou à 5. On
# fabrique donc le cas limite, de part et d'autre du seuil.
COHORTES_FABRIQUEES = (("Z99", 4, "sous le seuil"), ("Z98", 5, "au seuil exact"))
JOUR_INJECTION_COHORTE = "2026-08-28"


def _injecter_cohorte(ch, code: str, nb_patients: int) -> None:
    """Fabrique une pathologie et la cohorte de patients qui la porte."""
    jour = JOUR_INJECTION_COHORTE
    ch.command(f"""
        INSERT INTO bronze.ref_cim10 (code_cim10, libelle, _fichier_source, _ingested_at, _run_id)
        VALUES ('{code}', 'Pathologie de démonstration {code}', 'demo.csv', now(), 'demo')""")
    for i in range(nb_patients):
        pseudo, stay = f"DEMOK{code}P{i}", f"DEMOK{code}S{i}"
        ch.command(f"""
            INSERT INTO bronze.patients
            (patient_pseudo, birth_year, sex, region_code,
             _jour_depot, _fichier_source, _ingested_at, _run_id)
            VALUES ('{pseudo}', 1980, 'F', '35',
                    toDate('{jour}'), 'demo.csv', now(), 'demo')""")
        ch.command(f"""
            INSERT INTO bronze.sejours
            (stay_id, patient_pseudo, service_code, admission_ts, discharge_ts,
             _discharge_illisible, admission_mode, discharge_mode,
             _jour_depot, _fichier_source, _ingested_at, _run_id)
            VALUES ('{stay}', '{pseudo}', 'CARDIO',
                    toDateTime('{jour} 08:00:00'), toDateTime('{jour} 12:00:00'), 0,
                    'programme', 'domicile',
                    toDate('{jour}'), 'demo.csv', now(), 'demo')""")
        ch.command(f"""
            INSERT INTO bronze.diagnostics
            (stay_id, code_cim10, type_diag,
             _jour_depot, _fichier_source, _ingested_at, _run_id)
            VALUES ('{stay}', '{code}', 'principal',
                    toDate('{jour}'), 'demo.csv', now(), 'demo')""")


def effectifs() -> list[str]:
    """Fabrique deux cohortes autour du seuil, et constate où passe la coupe."""
    mod_journal.configurer()
    ch = client()
    echecs: list[str] = []

    def controle(libelle: str, obtenu, attendu) -> None:
        ok = obtenu == attendu
        if not ok:
            echecs.append(libelle)
        detail = f"{obtenu}" if ok else f"{obtenu} (attendu {attendu})"
        print(f"     {VERT if ok else ROUGE}{'✓' if ok else '✗'}{RAZ} {libelle:58} {GRIS}{detail}{RAZ}")

    _entete("Deux pathologies fabriquées, de part et d'autre du seuil de 5")
    for code, nb, libelle in COHORTES_FABRIQUEES:
        _injecter_cohorte(ch, code, nb)
        print(f"     {GRIS}{code} — {nb} patients, {libelle}{RAZ}")

    print("\n  ① Silver puis gold sont reconstruits sur ce bronze")
    executer_fichier(ch, "21_silver_transform.sql", run_id="demoeffectifs")
    executer_fichier(ch, "31_gold_transform.sql", run_id="demoeffectifs", **seuils_alerte())

    n = lambda requete: int(ch.command(requete))

    print("\n  ② Les deux cohortes existent bien au grain du fait, en pilotage")
    for code, nb, _ in COHORTES_FABRIQUEES:
        controle(f"{code} : {nb} patients dans fact_diagnostic",
                 n(f"""SELECT uniqExact(patient_pseudo) FROM gold_pilotage.fact_diagnostic
                       WHERE code_cim10 = '{code}'"""), nb)

    print("\n  ③ La cohorte de 4 patients n'atteint PAS la base recherche")
    controle("Z99 absent de coh_prevalence",
             n("SELECT count() FROM gold_recherche.coh_prevalence WHERE code_cim10 = 'Z99'"), 0)
    controle("Z99 absent de coh_description",
             n("SELECT count() FROM gold_recherche.coh_description WHERE code_cim10 = 'Z99'"), 0)

    print("\n  ④ Celle de 5 patients passe — le filtre coupe SOUS 5, pas à 5")
    controle("Z98 présent dans coh_prevalence",
             n("SELECT count() FROM gold_recherche.coh_prevalence WHERE code_cim10 = 'Z98'"), 1)
    controle("Z98 compté à 5 patients",
             n("SELECT nb_patients FROM gold_recherche.coh_prevalence WHERE code_cim10 = 'Z98'"), 5)

    print("\n  ⑤ Aucune cohorte sous le seuil nulle part dans la base recherche")
    for table in ("coh_prevalence", "coh_description"):
        controle(f"{table} : minimum >= 5",
                 n(f"SELECT count() FROM gold_recherche.{table} WHERE nb_patients < 5"), 0)

    print("\n  ⑥ Remise en état : bronze rechargé, gold reconstruit")
    executer_pipeline(["--tout"])
    controle("aucune pathologie de démonstration ne subsiste",
             n("""SELECT count() FROM gold_pilotage.dim_cim10
                  WHERE code_cim10 IN ('Z98', 'Z99')"""), 0)
    print()
    return echecs


SECTIONS = {
    "cloisonnement": cloisonnement,
    "restitution": restitution,
    "reprise": reprise,
    "qualite": qualite,
    "effectifs": effectifs,
}

BILANS = {
    "cloisonnement": "Cloisonnement vérifié : chaque compte n'accède qu'à sa base.",
    "restitution": "Cloisonnement vérifié jusque dans l'interface : bases, tableaux de bord et "
                    "connexion ClickHouse restent hors de portée d'un compte étranger.",
    "reprise": "Les erreurs sont détectées, tracées, et la reprise est une simple relance.",
    "qualite": "Les contrôles de format écartent, corrigent et tracent — démontré, pas déclaré.",
    "effectifs": "Le seuil des 5 patients coupe au bon endroit : 4 est retenu, 5 passe.",
}


def main(argv: list[str] | None = None) -> int:
    args = choisir_sections(SECTIONS, argv)
    if args is None:
        return 2

    echecs: list[str] = []
    for nom in args:
        print(f"\n═══ {nom.upper()} ═══")
        echecs += SECTIONS[nom]()

    print()
    if echecs:
        print(f"{ROUGE}Défauts constatés :{RAZ}")
        for e in echecs:
            print(f"   {e}")
        return 1
    for nom in args:
        print(f"{VERT}{BILANS[nom]}{RAZ}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
