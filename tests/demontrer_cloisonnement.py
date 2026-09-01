"""Démonstration du cloisonnement des droits (livrable Partie 1).

Chaque compte tente de lire les quatre bases de l'entrepôt. Le refus est
prononcé par ClickHouse lui-même : ce n'est pas une règle applicative que
l'on pourrait contourner en écrivant une autre requête.

Usage :  python -m tests.demontrer_cloisonnement
"""
from __future__ import annotations

import sys

import clickhouse_connect

from eds.config import exiger

VERT, ROUGE, GRIS, RAZ = "\033[32m", "\033[31m", "\033[90m", "\033[0m"

CIBLES_EXPLOITATION = ("bronze.sejours", "silver.rejets", "ops.executions")

CIBLES = [
    ("gold_pilotage.fact_sejour", "faits de séjour (pilotage)"),
    ("gold_recherche.coh_prevalence", "cohortes de recherche"),
    ("silver.patients", "détail patient (silver)"),
    ("bronze.sejours", "données brutes (bronze)"),
]


def _client(utilisateur: str, mot_de_passe: str):
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username=utilisateur, password=mot_de_passe
    )


def tester(compte: str, mot_de_passe: str, autorise: str) -> list[str]:
    """Vérifie qu'un compte accède à sa base et à AUCUNE autre."""
    print(f"\n  Compte {compte}")
    print(f"  {'─' * 66}")
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


def controler_contenu_recherche() -> list[str]:
    """La base recherche ne doit contenir ni âge fin, ni petit effectif."""
    print(f"\n  Contenu de la base recherche")
    print(f"  {'─' * 66}")
    ch = _client("eds_recherche", exiger("CH_RECHERCHE_PASSWORD"))
    echecs = []

    colonnes = {c for (c,) in ch.query(
        "SELECT name FROM system.columns WHERE database = 'gold_recherche'").result_rows}
    for interdite in ("birth_year", "patient_pseudo", "region_code"):
        present = interdite in colonnes
        marque = f"{ROUGE}✗{RAZ}" if present else f"{VERT}✓{RAZ}"
        if present:
            echecs.append(f"colonne {interdite} exposée en recherche")
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


def controler_comptes_metabase() -> list[str]:
    """Vérifie la séparation dans l'outil de restitution.

    Trois comptes, trois vues différentes. La séparation y est double :
    permissions de collection (quel tableau de bord est visible) et
    permissions de données (quelle base est interrogeable). Chaque connexion
    utilise par ailleurs un compte ClickHouse distinct, ce qui rend le
    cloisonnement opposable même à quelqu'un qui contournerait Metabase.
    """
    from eds.metabase import ErreurMetabase, _appel

    print(f"\n  Comptes Metabase")
    print(f"  {'─' * 66}")
    echecs = []

    attendus = {
        "admin":     (exiger("MB_ADMIN_EMAIL"), exiger("MB_ADMIN_PASSWORD"),
                      {"Pilotage hospitalier", "Recherche clinique"}),
        "pilotage":  (exiger("MB_PILOTAGE_EMAIL"), exiger("MB_PILOTAGE_PASSWORD"),
                      {"Pilotage hospitalier"}),
        "recherche": (exiger("MB_RECHERCHE_EMAIL"), exiger("MB_RECHERCHE_PASSWORD"),
                      {"Recherche clinique"}),
    }

    for nom, (courriel, mot_de_passe, attendu) in attendus.items():
        try:
            session = _appel("/session", "POST",
                             {"username": courriel, "password": mot_de_passe})["id"]
        except ErreurMetabase as erreur:
            echecs.append(f"connexion {nom} impossible : {erreur}")
            print(f"   {ROUGE}✗{RAZ} {nom:10} connexion refusée")
            continue

        visibles = {d["name"] for d in _appel("/dashboard", session=session)
                    if not d.get("archived")}
        bases = {b["name"] for b in _appel("/database", session=session)["data"]}
        conforme = visibles == attendu
        if not conforme:
            echecs.append(f"{nom} voit {visibles}, attendu {attendu}")
        marque = f"{VERT}✓{RAZ}" if conforme else f"{ROUGE}✗{RAZ}"
        print(f"   {marque} {nom:10} {len(visibles)} tableau(x), {len(bases)} base(s)"
              f"   {GRIS}{', '.join(sorted(visibles))}{RAZ}")

        # Un compte métier consulte des indicateurs enregistrés ; il ne doit
        # pas pouvoir composer sa propre requête. On teste ici la permission
        # elle-même, avec une requête que le compte de service autorise —
        # le refus, s'il vient, est donc bien celui de Metabase.
        peut_analyser = nom == "admin"
        try:
            reponse = _appel("/dataset", "POST", {
                "type": "native",
                "native": {"query": "SELECT countIf(est_urgence) FROM fact_sejour"},
                "database": 2}, session=session)
            obtenu = reponse.get("status") == "completed"
        except ErreurMetabase:
            obtenu = False

        conforme_sql = obtenu == peut_analyser
        if not conforme_sql:
            echecs.append(f"{nom} : requête libre {'autorisée' if obtenu else 'refusée'}, "
                          f"attendu l'inverse")
        marque = f"{VERT}✓{RAZ}" if conforme_sql else f"{ROUGE}✗{RAZ}"
        verdict = "peut composer ses requêtes" if obtenu else "ne peut pas composer de requête"
        print(f"   {marque} {'':10} {GRIS}{verdict}{RAZ}")

    return echecs


INTERDITS_PILOTAGE = (
    ("SELECT patient_pseudo FROM gold_pilotage.fact_sejour LIMIT 1",
     "lire le pseudonyme patient"),
    ("SELECT uniqExact(patient_pseudo) FROM gold_pilotage.fact_sejour",
     "dénombrer des patients"),
    ("SELECT stay_id FROM gold_pilotage.fact_sejour LIMIT 1",
     "identifier un séjour"),
    ("SELECT * FROM gold_pilotage.dim_patient LIMIT 1",
     "accéder à la dimension patient"),
    ("SELECT * FROM gold_pilotage.fact_diagnostic LIMIT 1",
     "accéder aux diagnostics"),
    ("SELECT * FROM gold_pilotage.fact_sejour LIMIT 1",
     "faire un SELECT *"),
)

REQUIS_PILOTAGE = (
    ("SELECT round(avg(duree_jours), 2) FROM gold_pilotage.fact_sejour "
     "WHERE est_en_cours = 0", "calculer la DMS"),
    ("SELECT countIf(est_urgence) FROM gold_pilotage.fact_sejour",
     "compter les passages aux urgences"),
    ("SELECT countIf(en_alerte) FROM gold_pilotage.fact_releve",
     "compter les relevés en alerte"),
)


def controler_droits_colonnes() -> list[str]:
    """Le compte de pilotage n'a de droits que sur les colonnes utiles.

    Un GRANT sur la base entière lui donnerait `patient_pseudo` et le grain
    du séjour. Les droits sont donc posés colonne par colonne : la direction
    consulte des indicateurs d'activité, elle n'a jamais à désigner un
    patient ni à relier deux séjours entre eux.
    """
    print(f"\n  Droits au niveau colonne — compte de pilotage")
    print(f"  {'─' * 66}")
    ch = _client("eds_pilotage", exiger("CH_PILOTAGE_PASSWORD"))
    echecs = []

    for requete, libelle in INTERDITS_PILOTAGE:
        try:
            ch.command(requete)
            echecs.append(f"pilotage peut {libelle}")
            print(f"   {ROUGE}✗{RAZ} AUTORISÉ  {libelle}")
        except Exception:
            print(f"   {VERT}✓{RAZ} REFUSÉ    {libelle}")

    # Le cloisonnement ne dépend pas du compte humain : c'est le compte de
    # SERVICE de la connexion qui borne ce qu'on peut lire. Même
    # l'administrateur, s'il passe par la connexion de pilotage, ne peut pas
    # atteindre le pseudonyme.
    from eds.metabase import _appel as _mb, ouvrir_session
    reponse = _mb("/dataset", "POST", {
        "type": "native",
        "native": {"query": "SELECT patient_pseudo FROM fact_sejour LIMIT 1"},
        "database": 2}, session=ouvrir_session())
    bloque = reponse.get("status") != "completed"
    if not bloque:
        echecs.append("l'admin lit le pseudonyme via la connexion de pilotage")
    marque = f"{VERT}✓{RAZ}" if bloque else f"{ROUGE}✗{RAZ}"
    print(f"   {marque} REFUSÉ    même à l'administrateur, via cette connexion")

    for requete, libelle in REQUIS_PILOTAGE:
        try:
            valeur = ch.command(requete)
            print(f"   {VERT}✓{RAZ} POSSIBLE  {libelle} {GRIS}= {valeur}{RAZ}")
        except Exception as erreur:
            echecs.append(f"pilotage ne peut pas {libelle}")
            print(f"   {ROUGE}✗{RAZ} IMPOSSIBLE {libelle} : {str(erreur)[:40]}")

    return echecs


def controler_compte_exploitation() -> list[str]:
    """Le compte d'investigation lit les couches techniques, sans écrire.

    L'administration doit pouvoir remonter à la ligne d'origine — incident,
    piste d'audit, demande d'effacement. Elle n'utilise pas pour autant le
    compte du pipeline, qui peut créer et supprimer des bases : un compte
    distinct, en lecture seule, applique le moindre privilège.
    """
    print(f"\n  Compte d'exploitation (investigation technique)")
    print(f"  {'─' * 66}")
    ch = _client("eds_exploitation", exiger("CH_EXPLOITATION_PASSWORD"))
    echecs = []

    for table in CIBLES_EXPLOITATION:
        try:
            n = int(ch.command(f"SELECT count() FROM {table}"))
            print(f"   {VERT}✓{RAZ} LECTURE   {table:24} {GRIS}{n} lignes{RAZ}")
        except Exception as erreur:
            echecs.append(f"exploitation ne peut pas lire {table}")
            print(f"   {ROUGE}✗{RAZ} {table} illisible : {str(erreur)[:50]}")

    # Le moindre privilège : aucune écriture, quelle que soit la requête.
    try:
        ch.command("TRUNCATE TABLE bronze.sejours")
        echecs.append("exploitation a pu ÉCRIRE — moindre privilège non respecté")
        print(f"   {ROUGE}✗{RAZ} ÉCRITURE  autorisée — moindre privilège non respecté")
    except Exception:
        print(f"   {VERT}✓{RAZ} ÉCRITURE  refusée par le moteur {GRIS}(lecture seule){RAZ}")

    return echecs


def main() -> int:
    print("\n═══ DÉMONSTRATION DU CLOISONNEMENT DES DROITS ═══")
    echecs = tester("eds_pilotage", exiger("CH_PILOTAGE_PASSWORD"), "gold_pilotage")
    echecs += tester("eds_recherche", exiger("CH_RECHERCHE_PASSWORD"), "gold_recherche")
    echecs += controler_contenu_recherche()
    echecs += controler_comptes_metabase()
    echecs += controler_droits_colonnes()
    echecs += controler_compte_exploitation()

    print()
    if echecs:
        print(f"{ROUGE}Cloisonnement en défaut :{RAZ}")
        for e in echecs:
            print(f"   {e}")
        return 1
    print(f"{VERT}Cloisonnement vérifié : chaque compte n'accède qu'à sa base.{RAZ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
