#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${BOSS_STAGING_NAMESPACE:-boss-staging}"
RELEASE="${BOSS_STAGING_RELEASE:-boss}"
CHART="${ROOT}/deploy/helm/boss"
VALUES_FILE="${BOSS_STAGING_VALUES_FILE:-}"

echo "Deploying BOSS staging Helm release '${RELEASE}' to namespace '${NAMESPACE}'"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required but not installed." >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required but not installed." >&2
  exit 1
fi

kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

helm upgrade --install "${RELEASE}" "${CHART}" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  ${VALUES_FILE:+--values "${VALUES_FILE}"} \
  --set image.api.tag="${BOSS_IMAGE_TAG:-latest}" \
  --set image.web.tag="${BOSS_IMAGE_TAG:-latest}" \
  --set image.worker.tag="${BOSS_IMAGE_TAG:-latest}" \
  --wait --timeout 10m

echo "Waiting for BOSS API deployment..."
kubectl rollout status "deployment/${RELEASE}-api" -n "${NAMESPACE}" --timeout=300s
kubectl rollout status "deployment/${RELEASE}-worker" -n "${NAMESPACE}" --timeout=300s

echo "Staging Helm deployment complete."
echo "Next: export BOSS_API_BASE_URL to the staging API ingress/service and run:"
echo "  python3 scripts/record_staging_cluster_validation.py --enterprise-cluster-id <cluster-id>"
