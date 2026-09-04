"""L'orchestrateur — ce qu'il tient pour déjà ingéré.

Cette question a une seule bonne réponse, et elle sert à deux endroits : à
décider ce que l'exécution incrémentale doit charger, et à afficher l'état de
l'entrepôt. Les faire diverger, c'est afficher « en attente » un jour qui est
en réalité chargé.
"""

from __future__ import annotations

import pytest

from eds import run
from tests.conftest import deposer


class ClientFactice:
    """Un ClickHouse réduit à ce que `jours_deja_ingeres` lui demande.

    Il ne répond qu'à une chose : quels jours de dépôt une table bronze
    contient. C'est exactement la connaissance qu'exige la fonction, et rien
    de plus.
    """

    def __init__(self, jours_par_table: dict[str, list[str]]):
        self._jours = jours_par_table

    def query(self, sql: str):
        table = sql.rsplit(" FROM ", 1)[1].strip()
        return type(
            "Reponse", (), {"result_rows": [(j,) for j in self._jours.get(table, [])]}
        )


@pytest.fixture
def depot(source):
    """Le dépôt du CHU, réduit à ce qu'un test veut y voir."""
    def deposer_jour(chemin: str) -> None:
        deposer(source, chemin, "contenu sans importance")
    return deposer_jour


def test_un_jour_dont_l_unique_source_deposee_est_chargee_est_ingere(depot):
    """Le cas réel du dépôt d'évolution : le 2026-08-29 n'apporte que des
    actes. Exiger des séjours ce jour-là le déclarerait éternellement en
    attente, alors que tout ce qui a été déposé est chargé."""
    depot("actes/2026-08-29/actes.parquet")
    ch = ClientFactice({"bronze.actes": ["2026-08-29"]})

    assert run.jours_deja_ingeres(ch) == {"2026-08-29"}


def test_un_jour_dont_une_source_manque_n_est_pas_ingere(depot):
    """La perte silencieuse que cette règle empêche : le chargement traite
    les sources l'une après l'autre, et si l'une échoue après les séjours, le
    jour paraîtrait ingéré — la relance le sauterait."""
    depot("sejours/2026-08-27/sejours.csv")
    depot("monitoring/2026-08-27/monitoring.parquet")
    ch = ClientFactice({"bronze.sejours": ["2026-08-27"]})

    assert run.jours_deja_ingeres(ch) == set()


def test_les_referentiels_ne_conditionnent_pas_l_ingestion_d_un_jour(depot):
    """Ils ne sont pas partitionnés par jour de dépôt : les attendre en
    bronze pour ce jour-là bloquerait tout jour où le CHU en redépose."""
    depot("referentiels/2026-08-29/ccam.csv")
    depot("actes/2026-08-29/actes.parquet")
    ch = ClientFactice({"bronze.actes": ["2026-08-29"]})

    assert run.jours_deja_ingeres(ch) == {"2026-08-29"}
