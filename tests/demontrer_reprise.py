"""Démonstration de la gestion des erreurs et de la reprise sur incident.

Trois scénarios de panne sont provoqués volontairement. Pour chacun on
vérifie que le pipeline : échoue proprement, journalise la cause dans
`ops.executions`, retourne un code de sortie non nul, et laisse l'entrepôt
dans un état cohérent — donc qu'une simple relance suffit à repartir.

Usage :  python -m tests.demontrer_reprise
"""
from __future__ import annotations

import sys

from eds import journal as mod_journal
from eds.run import main
from eds.warehouse import client

VERT, ROUGE, GRIS, RAZ = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def _volumes(ch) -> dict[str, int]:
    return {t: int(ch.command(f"SELECT count() FROM {t}"))
            for t in ("bronze.sejours", "silver.sejours",
                      "gold_pilotage.fact_sejour")}


def main_demo() -> int:
    mod_journal.configurer()
    ch = client()
    echecs = []

    print("\n═══ GESTION DES ERREURS ET REPRISE SUR INCIDENT ═══\n")
    avant = _volumes(ch)
    print(f"  {GRIS}État initial : {avant}{RAZ}\n")

    # ── Scénario 1 : entrée invalide, rejetée à la frontière ────────────
    print("  ① Jour de dépôt malformé (validation d'entrée)")
    code = main(["--jour", "pas-une-date"])
    ok = code != 0
    echecs += [] if ok else ["entrée invalide non rejetée"]
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — "
          f"rejeté avant toute écriture\n")

    # ── Scénario 2 : jour inexistant dans le dépôt ──────────────────────
    print("  ② Jour absent du dépôt du CHU")
    code = main(["--jour", "2099-01-01"])
    ok = code != 0
    echecs += [] if ok else ["jour inexistant non détecté"]
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — "
          f"échec explicite, pas de table vide silencieuse\n")

    # ── Scénario 3 : l'échec est-il tracé ? ─────────────────────────────
    print("  ③ Traçabilité de l'échec dans ops.executions")
    lignes = ch.query("""
        SELECT etape, toString(jour), statut, substring(message, 1, 58)
        FROM ops.executions WHERE statut = 'echec'
        ORDER BY demarre_a DESC LIMIT 3
    """).result_rows
    if lignes:
        for l in lignes:
            print(f"     {VERT}✓{RAZ} {l[0]:8} {l[1]:12} {l[2]:7} {GRIS}{l[3]}{RAZ}")
    else:
        echecs.append("aucun échec journalisé dans ops.executions")
        print(f"     {ROUGE}✗ aucun échec journalisé{RAZ}")

    # ── Scénario 4 : l'entrepôt est-il resté cohérent ? ─────────────────
    print("\n  ④ Cohérence de l'entrepôt après les incidents")
    apres = _volumes(ch)
    intact = avant == apres
    echecs += [] if intact else ["volumes modifiés par un run en échec"]
    print(f"     {VERT if intact else ROUGE}{'inchangé' if intact else 'ALTÉRÉ'}{RAZ} "
          f"— {apres}")

    # ── Scénario 5 : la relance répare-t-elle ? ─────────────────────────
    print("\n  ⑤ Reprise : une simple relance suffit")
    code = main([])
    ok = code == 0 and _volumes(ch) == avant
    echecs += [] if ok else ["la relance n'a pas rétabli l'état"]
    print(f"     {VERT if ok else ROUGE}code de sortie {code}{RAZ} — "
          f"entrepôt rétabli à l'identique\n")

    if echecs:
        print(f"{ROUGE}Défauts :{RAZ}")
        for e in echecs:
            print(f"   {e}")
        return 1
    print(f"{VERT}Les erreurs sont détectées, tracées, et la reprise est "
          f"une simple relance.{RAZ}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main_demo())
