#!/usr/bin/env bash
# Exploitation du déploiement cloud — l'équivalent de « docker compose up »
# pour Azure. Cinq verbes, tous rejouables :
#
#   ops/cloud.sh deployer    crée l'infrastructure, construit l'image, envoie
#                            le dépôt source, pose les manifestes
#   ops/cloud.sh charger     recharge tout le dépôt (Job eds-charger)
#   ops/cloud.sh restituer   provisionne Metabase (Job eds-restituer) et
#                            affiche l'adresse et le compte administrateur
#   ops/cloud.sh etat        ce qui tourne
#   ops/cloud.sh detruire    supprime tout ; --oui saute la confirmation
#
# Prérequis : az (connecté), terraform, kubectl, et
# infra/terraform/terraform.tfvars renseigné (cf. terraform.tfvars.example).
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$RACINE/infra/terraform"
BASE="$RACINE/infra/k8s/base"
RENDU="$RACINE/infra/k8s/rendu"
NS=eds
DELAI_JOB_S=1200

echo_err() { echo "$*" >&2; }

verifier_outils() {
  for outil in az terraform kubectl git; do
    command -v "$outil" >/dev/null || { echo_err "outil manquant : $outil"; exit 2; }
  done
  az account show >/dev/null 2>&1 || { echo_err "az login requis"; exit 2; }
  [[ -f "$TF/terraform.tfvars" ]] || {
    echo_err "infra/terraform/terraform.tfvars absent : copier terraform.tfvars.example"; exit 2; }
}

sortie_tf() { terraform -chdir="$TF" output -raw "$1"; }

tag_image() { git -C "$RACINE" rev-parse --short HEAD; }

# Remplace les marque-places des manifestes par les sorties Terraform. Le
# résultat porte des identifiants propres à CE déploiement : il n'est pas
# versionné (cf. .gitignore).
rendre() {
  local tag="$1"
  local acr kv tenant csi sa rg kubelet ip
  acr=$(sortie_tf acr_login_server);    kv=$(sortie_tf key_vault_name)
  tenant=$(sortie_tf tenant_id);        csi=$(sortie_tf csi_client_id)
  sa=$(sortie_tf storage_account_name); rg=$(sortie_tf resource_group_name)
  kubelet=$(sortie_tf kubelet_client_id); ip=$(sortie_tf ip_autorisee)
  rm -rf "$RENDU"; mkdir -p "$RENDU"
  for f in "$BASE"/*.yaml; do
    sed -e "s|__ACR_LOGIN_SERVER__|$acr|g" -e "s|__TAG__|$tag|g" \
        -e "s|__KEY_VAULT__|$kv|g"        -e "s|__TENANT_ID__|$tenant|g" \
        -e "s|__CSI_CLIENT_ID__|$csi|g"   -e "s|__STORAGE_ACCOUNT__|$sa|g" \
        -e "s|__RESOURCE_GROUP__|$rg|g"   -e "s|__KUBELET_CLIENT_ID__|$kubelet|g" \
        -e "s|__IP_AUTORISEE__|$ip|g" "$f" > "$RENDU/$(basename "$f")"
  done
  if grep -rq "__[A-Z_]*__" "$RENDU"; then
    echo_err "marque-place non rendu :"; grep -rho "__[A-Z_]*__" "$RENDU" | sort -u >&2; exit 1
  fi
}

deployer() {
  verifier_outils
  terraform -chdir="$TF" init -input=false
  terraform -chdir="$TF" apply -input=false -auto-approve
  local tag; tag=$(tag_image)
  # Construite dans Azure, pour l'architecture des nœuds : ni Docker local,
  # ni croisement ARM/AMD64 depuis un Mac.
  az acr build --registry "$(sortie_tf acr_name)" --image "eds-pipeline:$tag" \
    --platform linux/amd64 --file "$RACINE/infra/Dockerfile" "$RACINE"
  az aks get-credentials --resource-group "$(sortie_tf resource_group_name)" \
    --name "$(sortie_tf aks_name)" --overwrite-existing
  # Le dépôt du CHU, par l'identité de l'opérateur (rôle posé par Terraform).
  az storage blob upload-batch --auth-mode login \
    --account-name "$(sortie_tf storage_account_name)" \
    --destination source --source "$RACINE/eds-chu-sujet/source-filestorage" \
    --overwrite --only-show-errors
  rendre "$tag"
  kubectl apply -k "$RENDU"
  kubectl -n "$NS" rollout status statefulset/clickhouse --timeout=600s
  echo "déployé — image eds-pipeline:$tag"
}

# Lance un Job à partir de son manifeste rendu, attend sa fin, affiche ses
# journaux, rend son code de sortie.
lancer_job() {
  local nom="$1"
  kubectl -n "$NS" delete job "$nom" --ignore-not-found --wait=true
  kubectl apply -f "$RENDU/job-${nom#eds-}.yaml"
  local debut=$SECONDS
  while true; do
    local reussi echoue
    reussi=$(kubectl -n "$NS" get job "$nom" -o jsonpath='{.status.succeeded}')
    echoue=$(kubectl -n "$NS" get job "$nom" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}')
    [[ "${reussi:-0}" -ge 1 ]] && break
    if [[ "$echoue" == "True" ]]; then
      kubectl -n "$NS" logs "job/$nom" --all-containers --tail=200 || true
      echo_err "job $nom en échec"; return 1
    fi
    (( SECONDS - debut > DELAI_JOB_S )) && { echo_err "job $nom : délai dépassé"; return 1; }
    sleep 5
  done
  kubectl -n "$NS" logs "job/$nom" --all-containers
}

charger() {
  verifier_outils
  [[ -d "$RENDU" ]] || rendre "$(tag_image)"
  lancer_job eds-charger
}

restituer() {
  verifier_outils
  [[ -d "$RENDU" ]] || rendre "$(tag_image)"
  lancer_job eds-restituer
  local ip
  ip=$(kubectl -n "$NS" get svc metabase -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
  echo
  echo "Metabase          http://$ip:3000  (depuis $(sortie_tf ip_autorisee) seulement)"
  echo "Administrateur    $(kubectl -n "$NS" get configmap eds-config -o jsonpath='{.data.MB_ADMIN_EMAIL}')"
  echo "Mot de passe      az keyvault secret show --vault-name $(sortie_tf key_vault_name) --name mb-admin-password --query value -o tsv"
}

etat() {
  verifier_outils
  kubectl -n "$NS" get pods,jobs,cronjobs,svc,pvc
}

detruire() {
  verifier_outils
  if [[ "${1:-}" != "--oui" ]]; then
    read -r -p "Détruire tout le déploiement cloud (groupe $(sortie_tf resource_group_name)) ? [oui/N] " reponse
    [[ "$reponse" == "oui" ]] || { echo "abandon"; exit 0; }
  fi
  # Les Services LoadBalancer d'abord : Terraform ne connaît pas l'IP
  # publique créée par Kubernetes, elle bloquerait la suppression du groupe.
  if [[ -d "$RENDU" ]] && kubectl get ns "$NS" >/dev/null 2>&1; then
    kubectl delete -k "$RENDU" --ignore-not-found --wait=true --timeout=300s || true
  fi
  terraform -chdir="$TF" destroy -input=false -auto-approve
  rm -rf "$RENDU"
  echo "détruit"
}

case "${1:-}" in
  deployer)  deployer ;;
  charger)   charger ;;
  restituer) restituer ;;
  etat)      etat ;;
  detruire)  detruire "${2:-}" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
