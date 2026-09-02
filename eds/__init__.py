"""Pipeline EDS CHU — ingestion, pseudonymisation et transformation."""

from __future__ import annotations

import sys
from collections.abc import Iterable


def choisir_sections(
    disponibles: Iterable[str], argv: list[str] | None
) -> list[str] | None:
    """Résout les sections demandées en ligne de commande.

    Partagée par les trois scripts à sections (profilage, verifier,
    demontrer) : sans argument, toutes les sections ; sinon celles nommées.
    Retourne None si un nom est inconnu, après avoir écrit les noms valides
    sur stderr — au script appelant de sortir en code 2.
    """
    disponibles = list(disponibles)
    demandees = (argv if argv is not None else sys.argv[1:]) or disponibles
    inconnues = [s for s in demandees if s not in disponibles]
    if inconnues:
        print(f"Section inconnue : {', '.join(inconnues)}", file=sys.stderr)
        print(f"Disponibles : {', '.join(disponibles)}", file=sys.stderr)
        return None
    return demandees
