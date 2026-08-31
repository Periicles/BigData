#!/usr/bin/env bash
# Enchaîne les sorties à capturer pour le rapport, séparées et titrées.
#
#   bash ops/captures.sh            # tout, à la suite
#   bash ops/captures.sh 3          # une seule section
#
# Agrandissez la police du terminal avant de capturer (Cmd + sur macOS).

set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

titre() { printf '\n\033[1;36m═══ %s ═══\033[0m\n\n' "$1"; }

c1() {
  titre "1 · Pseudonymisation — la source contre le lake"
  echo "SOURCE (dépôt du CHU, identité réelle) :"
  head -2 eds-chu-sujet/source-filestorage/patients/2026-08-26/patients.csv
  echo
  echo "LAKE (ce qui entre dans l'entrepôt) :"
  head -2 lake/patients/2026-08-26/patients.csv
}
c2() { titre "2 · Preuve de non-fuite";        $PY -m tests.verifier_pseudonymisation; }
c3() { titre "3 · Qualité et conservation";    $PY -m tests.verifier_qualite; }
c4() { titre "4 · Cloisonnement des droits";   $PY -m tests.demontrer_cloisonnement; }
c5() { titre "5 · Erreurs et reprise";         $PY -m tests.demontrer_reprise 2>&1 | grep -vE '^[0-9]{2}:[0-9]{2}:[0-9]{2}  INFO'; }
c6() { titre "6 · Pipeline complet";           $PY -m eds.run --tout; }
c7() { titre "7 · État de l'entrepôt";         $PY -m eds.run --etat; }

if [ $# -eq 1 ]; then "c$1"; else for i in 1 2 3 4 5 6 7; do "c$i"; done; fi
echo
