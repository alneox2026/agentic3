#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Import Pre-existing GCP Resources into Terraform State
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}/infra/terraform"

import_if_needed() {
  local tf_resource="$1"
  local gcp_id="$2"

  if ! terraform state list 2>/dev/null | grep -Fxq "${tf_resource}"; then
    echo "--> Checking/importing ${tf_resource} (${gcp_id})..."
    terraform import -var="project_id=${PROJECT_ID}" -var="region=${REGION}" "${tf_resource}" "${gcp_id}" 2>/dev/null || true
  else
    echo "    ${tf_resource} is already tracked in state."
  fi
}

echo "================================================================="
echo " Reconciling Pre-existing GCP Resources with Terraform State"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo "================================================================="

# Service Accounts
import_if_needed "google_service_account.gateway" "projects/${PROJECT_ID}/serviceAccounts/ceoagent-gateway-sa-v3@${PROJECT_ID}.iam.gserviceaccount.com"
import_if_needed "google_service_account.worker" "projects/${PROJECT_ID}/serviceAccounts/ceoagent-worker-sa-v3@${PROJECT_ID}.iam.gserviceaccount.com"
import_if_needed "google_service_account.billing_api" "projects/${PROJECT_ID}/serviceAccounts/ceoagent-billing-api-sa-v3@${PROJECT_ID}.iam.gserviceaccount.com"
import_if_needed "google_service_account.eventarc" "projects/${PROJECT_ID}/serviceAccounts/ceoagent-eventarc-sa-v3@${PROJECT_ID}.iam.gserviceaccount.com"
import_if_needed "google_service_account.billing_reconciler" "projects/${PROJECT_ID}/serviceAccounts/ceoagent-reconciler-sa-v3@${PROJECT_ID}.iam.gserviceaccount.com"

# Pub/Sub Topic
import_if_needed "google_pubsub_topic.agent_turn_events" "projects/${PROJECT_ID}/topics/agent-turn-events-v3"

# Cloud Run Services (if pre-existing)
import_if_needed "google_cloud_run_v2_service.gateway" "projects/${PROJECT_ID}/locations/${REGION}/services/ceoagent-gateway-v3"
import_if_needed "google_cloud_run_v2_service.worker" "projects/${PROJECT_ID}/locations/${REGION}/services/ceoagent-persistence-worker-v3"
import_if_needed "google_cloud_run_v2_service.billing_api" "projects/${PROJECT_ID}/locations/${REGION}/services/ceoagent-billing-api-v3"

# Eventarc Trigger (if pre-existing)
import_if_needed "google_eventarc_trigger.worker_turn_events" "projects/${PROJECT_ID}/locations/${REGION}/triggers/ceoagent-turn-events-trigger-v3"

# Cloud Scheduler Jobs (if pre-existing)
import_if_needed "google_cloud_scheduler_job.billing_reconciliation" "projects/${PROJECT_ID}/locations/${REGION}/jobs/billing-expired-reservations-reconciler"
import_if_needed "google_cloud_scheduler_job.cancellation_reconciliation" "projects/${PROJECT_ID}/locations/${REGION}/jobs/subscription-cancellation-reconciler"

# Logging Metric (if pre-existing)
import_if_needed "google_logging_metric.worker_retryable_failures" "ceoagent-persistence-worker-v3_retryable_failures"

echo "--> Pre-existing resource reconciliation check complete."
