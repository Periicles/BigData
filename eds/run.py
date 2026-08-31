"""Point d'entrée du pipeline EDS — collecte, transformation, restitution.

    python -m eds.run                      # incrémental : les jours non encore ingérés
    python -m eds.run --jour 2026-08-27    # rejoue un jour précis
    python -m eds.run --tout               # recharge l'intégralité du dépôt
    python -m eds.run --etat               # état de l'entrepôt, sans rien modifier

Propriétés garanties :

  IDEMPOTENCE   rejouer un jour réécrit sa partition ; les compteurs ne
                bougent pas et aucun doublon n'apparaît.
  INCRÉMENTAL   par défaut, seuls les jours absents de l'entrepôt sont
                ingérés ; les anciens ne sont ni relus ni dupliqués.
  REPRISE       bronze est la source de vérité durable. Silver et gold en
                sont intégralement reconstructibles : après un incident, une
                simple relance suffit, sans restauration ni intervention.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime

from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from eds import journal as mod_journal
from eds.config import LAKE, exiger
from eds.lake import copier_jour, jours_disponibles
from eds.warehouse import (
    charger_bronze_jour,
    charger_referentiels,
    client,
    executer_fichier,
    valider_jour,
)

LOG = logging.getLogger("eds.run")

# Fichiers de schéma appliqués à chaque démarrage : tous en CREATE IF NOT
# EXISTS, donc rejouables sans effet de bord.
SCHEMA = (
    "00_databases.sql",
    "10_bronze.sql",
    "20_silver.sql",
    "30_gold.sql",
    "60_ops.sql",
)

TENTATIVES_MAX = 3
ATTENTE_INITIALE_S = 2


def _en_date(jour: str | None) -> date | None:
    """Convertit un jour AAAA-MM-JJ en objet date pour l'insertion."""
    return date.fromisoformat(jour) if jour else None


class ErreurPipeline(Exception):
    """Échec métier : la relance à l'identique ne changera rien."""


def _est_transitoire(erreur: Exception) -> bool:
    """Distingue une panne passagère d'une erreur définitive.

    Une coupure réseau ou un moteur qui redémarre se retentent ; une erreur
    de syntaxe SQL ou une contrainte violée, non.
    """
    return isinstance(erreur, OperationalError) or (
        isinstance(erreur, DatabaseError) and "Connection" in str(erreur)
    )


def avec_reprises(action, description: str):
    """Exécute une action en retentant les seules pannes transitoires."""
    attente = ATTENTE_INITIALE_S
    for tentative in range(1, TENTATIVES_MAX + 1):
        try:
            return action()
        except Exception as erreur:
            if not _est_transitoire(erreur) or tentative == TENTATIVES_MAX:
                raise
            LOG.warning(
                "panne transitoire, nouvelle tentative dans %ss : %s",
                attente,
                description,
            )
            time.sleep(attente)
            attente *= 2  # temporisation exponentielle


class Pipeline:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex[:12]
        self.ch = client()

    # ── journalisation ───────────────────────────────────────────────────
    @contextmanager
    def etape(self, nom: str, jour: str | None = None):
        """Encadre une étape : mesure, journalise le succès comme l'échec.

        Le bilan est écrit dans `ops.executions` dans TOUS les cas — c'est ce
        qui rend un échec aussi traçable qu'une réussite.
        """
        debut, t0 = datetime.now(), time.monotonic()
        compteur = {"lignes": 0}
        try:
            yield compteur
        except Exception as erreur:
            self._consigner(
                nom,
                jour,
                "echec",
                0,
                time.monotonic() - t0,
                f"{type(erreur).__name__}: {erreur}"[:500],
                debut,
            )
            LOG.error(
                "étape en échec",
                extra={"etape": nom, "jour": jour, "run_id": self.run_id},
                exc_info=True,
            )
            raise
        else:
            duree = time.monotonic() - t0
            self._consigner(nom, jour, "succes", compteur["lignes"], duree, "", debut)
            LOG.info(
                "étape terminée",
                extra={
                    "etape": nom,
                    "jour": jour,
                    "lignes": compteur["lignes"],
                    "duree_s": round(duree, 3),
                    "run_id": self.run_id,
                },
            )

    def _consigner(self, etape, jour, statut, lignes, duree, message, debut) -> None:
        try:
            self.ch.insert(
                "ops.executions",
                [
                    [
                        self.run_id,
                        etape,
                        _en_date(jour),
                        statut,
                        int(lignes),
                        round(duree, 3),
                        message,
                        debut,
                        datetime.now(),
                    ]
                ],
                column_names=[
                    "run_id",
                    "etape",
                    "jour",
                    "statut",
                    "lignes",
                    "duree_s",
                    "message",
                    "demarre_a",
                    "termine_a",
                ],
            )
        except Exception:
            # Ne jamais faire échouer le pipeline à cause de son propre
            # journal : le fichier de log reste la trace de secours.
            LOG.warning("journal ClickHouse indisponible", exc_info=True)

    # ── état ─────────────────────────────────────────────────────────────
    def jours_deja_ingeres(self) -> set[str]:
        lignes = self.ch.query(
            "SELECT DISTINCT toString(_jour_depot) FROM bronze.sejours"
        ).result_rows
        return {l[0] for l in lignes}

    def jours_a_traiter(self, tout: bool) -> list[str]:
        disponibles = jours_disponibles()
        if tout:
            return disponibles
        deja = self.jours_deja_ingeres()
        return [j for j in disponibles if j not in deja]

    # ── étapes ───────────────────────────────────────────────────────────
    def verifier_acces_lake(self) -> None:
        """Vérifie que ClickHouse voit le lake avant de tenter de le lire.

        Le lake est un montage Docker. Supprimer le répertoire côté hôte
        (`rm -rf lake`) casse le montage : le conteneur reste attaché à
        l'ancien inode et ne voit plus aucun fichier. L'erreur brute du
        moteur (FILE_DOESNT_EXIST) n'oriente alors pas vers la vraie cause,
        d'où ce contrôle explicite et son message d'action.
        """
        temoin = LAKE / ".sonde"
        temoin.write_text("sonde", encoding="utf-8")
        try:
            self.ch.command("SELECT count() FROM file('lake/.sonde', LineAsString)")
        except Exception as erreur:
            raise ErreurPipeline(
                "ClickHouse ne voit pas le lake. Le montage est probablement "
                "rompu (le répertoire lake/ a-t-il été supprimé ?). "
                "Correction : docker compose restart clickhouse"
            ) from erreur
        finally:
            temoin.unlink(missing_ok=True)

    def preparer_schema(self) -> None:
        with self.etape("schema") as c:
            c["lignes"] = sum(
                avec_reprises(lambda f=f: executer_fichier(self.ch, f), f)
                for f in SCHEMA
            )

    def ingerer(self, jour: str) -> None:
        valider_jour(jour)
        with self.etape("lake", jour) as c:
            resultats = copier_jour(jour)
            c["lignes"] = sum(r.lignes or 0 for r in resultats)
            if not resultats:
                raise ErreurPipeline(f"aucun fichier trouvé pour le {jour}")

        with self.etape("bronze", jour) as c:
            compteurs = avec_reprises(
                lambda: charger_bronze_jour(self.ch, jour, self.run_id),
                f"bronze {jour}",
            )
            c["lignes"] = sum(compteurs.values())

    def ingerer_referentiels(self) -> None:
        """Rechargés intégralement : ils ne sont déposés que le premier jour."""
        jours = [j for j in jours_disponibles() if (LAKE / "referentiels" / j).is_dir()]
        if not jours:
            LOG.warning("aucun référentiel dans le lake")
            return
        with self.etape("referentiels") as c:
            compteurs = charger_referentiels(self.ch, jours[0], self.run_id)
            c["lignes"] = sum(compteurs.values())

    def transformer(self) -> None:
        with self.etape("silver") as c:
            avec_reprises(
                lambda: executer_fichier(
                    self.ch, "21_silver_transform.sql", run_id=self.run_id
                ),
                "silver",
            )
            c["lignes"] = sum(
                int(self.ch.command(f"SELECT count() FROM silver.{t}"))
                for t in ("patients", "sejours", "diagnostics", "monitoring")
            )

        with self.etape("gold") as c:
            avec_reprises(
                lambda: executer_fichier(
                    self.ch, "31_gold_transform.sql", run_id=self.run_id
                ),
                "gold",
            )
            c["lignes"] = sum(
                int(self.ch.command(f"SELECT count() FROM {t}"))
                for t in (
                    "gold_pilotage.fact_sejour",
                    "gold_pilotage.fact_diagnostic",
                    "gold_pilotage.fact_releve",
                    "gold_recherche.coh_prevalence",
                    "gold_recherche.coh_description",
                )
            )

    def publier_restitution(self) -> None:
        """Configure Metabase et publie les tableaux de bord.

        Une indisponibilité de Metabase n'invalide pas l'entrepôt : l'étape
        est journalisée en échec, mais le pipeline de données a déjà abouti.
        La restitution se rattrape par `python -m eds.metabase`.
        """
        from eds.metabase import ErreurMetabase, installer

        try:
            with self.etape("restitution") as c:
                c["lignes"] = len(installer())
        except (ErreurMetabase, OSError) as erreur:
            LOG.warning("restitution indisponible, entrepôt inchangé : %s", erreur)

    def appliquer_droits(self) -> None:
        with self.etape("droits") as c:
            c["lignes"] = executer_fichier(
                self.ch,
                "50_droits.sql",
                mdp_pilotage=exiger("CH_PILOTAGE_PASSWORD"),
                mdp_recherche=exiger("CH_RECHERCHE_PASSWORD"),
                mdp_exploitation=exiger("CH_EXPLOITATION_PASSWORD"),
            )

    # ── exécution ────────────────────────────────────────────────────────
    def executer(self, jours: list[str]) -> None:
        LOG.info("démarrage", extra={"run_id": self.run_id})
        self.preparer_schema()
        self.verifier_acces_lake()

        if not jours:
            LOG.info("aucun nouveau jour à ingérer — entrepôt à jour")
        for jour in jours:
            self.ingerer(jour)

        self.ingerer_referentiels()
        self.transformer()
        self.appliquer_droits()
        self.publier_restitution()
        LOG.info("terminé", extra={"run_id": self.run_id})


def afficher_etat() -> int:
    """Affiche l'état de l'entrepôt sans rien modifier."""
    ch = client()
    disponibles = jours_disponibles()
    ingeres = {
        l[0]
        for l in ch.query(
            "SELECT DISTINCT toString(_jour_depot) FROM bronze.sejours"
        ).result_rows
    }

    print("\nJOURS DE DÉPÔT")
    for jour in disponibles:
        print(f"   {'ingéré ' if jour in ingeres else 'EN ATTENTE'}  {jour}")

    print("\nVOLUMES")
    for table in (
        "bronze.sejours",
        "silver.sejours",
        "silver.rejets",
        "gold_pilotage.fact_sejour",
        "gold_recherche.coh_prevalence",
    ):
        print(f"   {table:34} {ch.command(f'SELECT count() FROM {table}'):>7}")

    print("\nCINQ DERNIÈRES ÉTAPES")
    lignes = ch.query("""
        SELECT demarre_a, run_id, etape, statut, lignes, duree_s
        FROM ops.executions ORDER BY demarre_a DESC LIMIT 5
    """).result_rows
    for l in lignes:
        print(
            f"   {l[0]:%Y-%m-%d %H:%M:%S}  {l[1]}  {l[2]:12} {l[3]:7} "
            f"{l[4]:>7} lignes  {l[5]:>6.2f}s"
        )
    if not lignes:
        print("   (aucune exécution enregistrée)")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        prog="python -m eds.run",
        description="Pipeline EDS CHU — collecte, transformation, restitution.",
    )
    groupe = analyseur.add_mutually_exclusive_group()
    groupe.add_argument(
        "--jour", metavar="AAAA-MM-JJ", help="rejoue un jour de dépôt précis"
    )
    groupe.add_argument(
        "--tout", action="store_true", help="recharge l'intégralité du dépôt"
    )
    groupe.add_argument(
        "--etat",
        action="store_true",
        help="affiche l'état de l'entrepôt sans rien modifier",
    )
    args = analyseur.parse_args(argv)

    mod_journal.configurer()

    if args.etat:
        return afficher_etat()

    # Validé avant toute connexion : une saisie erronée ne doit pas se
    # présenter comme une panne du système.
    try:
        jour_demande = valider_jour(args.jour) if args.jour else None
    except ValueError as erreur:
        LOG.error("argument invalide : %s", erreur)
        return 1

    try:
        pipeline = Pipeline()
        jours = (
            [jour_demande] if jour_demande else pipeline.jours_a_traiter(tout=args.tout)
        )
        pipeline.executer(jours)
    except ErreurPipeline as erreur:
        LOG.error("pipeline interrompu : %s", erreur)
        LOG.error(
            "reprise : corriger la cause puis relancer "
            "`python -m eds.run` — l'exécution est idempotente"
        )
        return 1
    except Exception:
        LOG.critical("échec inattendu", exc_info=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
