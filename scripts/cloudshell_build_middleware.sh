#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Build & Push 3 Middleware Container Images to Google Artifact Registry
# (agent_gateway, agent_persistence_worker, billing_api)
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-ceosystem}"

echo "================================================================="
echo " Building Middleware Container Images"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo " Repository : ${REPOSITORY}"
echo "================================================================="

gcloud config set project "${PROJECT_ID}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

TAG="$(git rev-parse --short=12 HEAD 2>/dev/null || echo "managed-$(date +%s)")"
echo "Build Tag: ${TAG}"

# Ensure Artifact Registry repository exists
gcloud artifacts repositories describe "${REPOSITORY}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPOSITORY}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Middleware Docker Repository" \
  --project="${PROJECT_ID}"

GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway:${TAG}"
GATEWAY_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-gateway:latest"

WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker:${TAG}"
WORKER_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-worker:latest"

BILLING_API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api:${TAG}"
BILLING_API_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/managed-agents-billing-api:latest"

echo "--> [1/3] Building Gateway Image: ${GATEWAY_IMAGE}"
gcloud builds submit . \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "services/agent_gateway_v3/Dockerfile", "-t", "${GATEWAY_IMAGE}", "-t", "${GATEWAY_LATEST}", "."]
images:
- "${GATEWAY_IMAGE}"
- "${GATEWAY_LATEST}"
EOF
) --project="${PROJECT_ID}"

echo "--> [2/3] Building Persistence Worker Image: ${WORKER_IMAGE}"
gcloud builds submit . \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "services/agent_persistence_worker_v3/Dockerfile", "-t", "${WORKER_IMAGE}", "-t", "${WORKER_LATEST}", "."]
images:
- "${WORKER_IMAGE}"
- "${WORKER_LATEST}"
EOF
) --project="${PROJECT_ID}"

echo "--> [3/3] Building Billing API Image: ${BILLING_API_IMAGE}"
gcloud builds submit . \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "services/billing_api_v3/Dockerfile", "-t", "${BILLING_API_IMAGE}", "-t", "${BILLING_API_LATEST}", "."]
images:
- "${BILLING_API_IMAGE}"
- "${BILLING_API_LATEST}"
EOF
) --project="${PROJECT_ID}"

echo "================================================================="
echo " Middleware Container Images Built & Pushed Successfully!"
echo " Gateway Image     : ${GATEWAY_IMAGE}"
echo " Worker Image      : ${WORKER_IMAGE}"
echo " Billing API Image : ${BILLING_API_IMAGE}"
echo "================================================================="
