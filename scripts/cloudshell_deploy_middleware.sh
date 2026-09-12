#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Deploy Middleware Infrastructure via Terraform (Gateway, Worker, Billing API, Pub/Sub)
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-ceosystem}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# Auto-detect latest built digests or tags per service if not explicitly provided
if [ -n "${TAG:-}" ] && [ "${TAG}" != "latest" ]; then
  GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway:${TAG}"
  WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker:${TAG}"
  BILLING_API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api:${TAG}"
else
  echo "--> Resolving :latest image digests from Artifact Registry..."
  GATEWAY_DIGEST="$(gcloud artifacts docker images describe "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway:latest" --format='value(image_summary.digest)' 2>/dev/null || true)"
  WORKER_DIGEST="$(gcloud artifacts docker images describe "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker:latest" --format='value(image_summary.digest)' 2>/dev/null || true)"
  BILLING_API_DIGEST="$(gcloud artifacts docker images describe "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api:latest" --format='value(image_summary.digest)' 2>/dev/null || true)"

  if [ -n "${GATEWAY_DIGEST}" ]; then
    GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway@${GATEWAY_DIGEST}"
  else
    GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway:latest"
  fi

  if [ -n "${WORKER_DIGEST}" ]; then
    WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker@${WORKER_DIGEST}"
  else
    WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker:latest"
  fi

  if [ -n "${BILLING_API_DIGEST}" ]; then
    BILLING_API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api@${BILLING_API_DIGEST}"
  else
    BILLING_API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api:latest"
  fi
fi
# Auto-detect cluster suffix from the repo directory name (e.g. ceodev-v6 → v6)
# Can be overridden by setting CLUSTER_SUFFIX env var explicitly.
if [ -z "${CLUSTER_SUFFIX:-}" ]; then
  REPO_DIR_NAME="$(basename "${ROOT_DIR}")"
  CLUSTER_SUFFIX="${REPO_DIR_NAME##*-}"  # extracts "v6" from "ceodev-v6"
fi
TF_STATE_PREFIX="${TF_STATE_PREFIX:-managed-agents-${CLUSTER_SUFFIX}/middleware}"

echo "================================================================="
echo " Deploying Middleware Infrastructure (Terraform)"
echo " Project ID        : ${PROJECT_ID}"
echo " Region            : ${REGION}"
echo " Cluster Suffix    : ${CLUSTER_SUFFIX}"
echo " State Prefix      : ${TF_STATE_PREFIX}"
echo " Gateway Image     : ${GATEWAY_IMAGE}"
echo " Worker Image      : ${WORKER_IMAGE}"
echo " Billing API Image : ${BILLING_API_IMAGE}"
echo "================================================================="

cd "${ROOT_DIR}/infra/terraform"

# Clean any stale generated tfvars
rm -f terraform.auto.tfvars.json

# Generate isolated cluster backend configuration
cat > backend.hcl <<EOF
bucket = "${PROJECT_ID}-tfstate"
prefix = "${TF_STATE_PREFIX}"
EOF

terraform init -backend-config=backend.hcl -reconfigure

EXTRA_TFVARS=""
if [ -n "${STRIPE_WEBHOOK_SIGNING_SECRET_ID:-}" ]; then
  EXTRA_TFVARS=",\"billing_api_stripe_webhook_signing_secret_id\": \"${STRIPE_WEBHOOK_SIGNING_SECRET_ID}\""
fi

cat > terraform.auto.tfvars.json <<EOF
{
  "project_id": "${PROJECT_ID}",
  "region": "${REGION}",
  "gateway_image": "${GATEWAY_IMAGE}",
  "worker_image": "${WORKER_IMAGE}",
  "billing_api_image": "${BILLING_API_IMAGE}",
  "allowed_origins": ["https://ceoappdev.flutterflow.app"],
  "billing_api_allowed_origins": ["https://ceoappdev.flutterflow.app"],
  "billing_api_stripe_secret_key_secret_version": "1",
  "billing_api_stripe_webhook_signing_secret_version": "1",
  "billing_api_checkout_success_url": "https://ceoappdev.flutterflow.app/billing-complete?session_id={CHECKOUT_SESSION_ID}",
  "billing_api_checkout_cancel_url": "https://ceoappdev.flutterflow.app/billing-cancelled",
  "billing_enforcement_enabled": true${EXTRA_TFVARS}
}
EOF

if ! terraform apply -auto-approve; then
  echo "--> Initial apply hit a transient dependency or GCP indexing delay (e.g. logging metrics). Retrying in 20 seconds..."
  sleep 20
  terraform apply -auto-approve
fi

echo "================================================================="
echo " Middleware Deployed Successfully!"
echo "================================================================="
terraform output
