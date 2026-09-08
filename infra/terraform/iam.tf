resource "google_service_account" "gateway" {
  account_id   = var.gateway_service_account_name
  display_name = "CEOsystem Agent Gateway"
}

resource "google_service_account" "worker" {
  account_id   = var.worker_service_account_name
  display_name = "CEOsystem Agent Persistence Worker"
}

resource "google_service_account" "billing_api" {
  account_id   = var.billing_api_service_account_name
  display_name = "CEOsystem Billing API"
}

resource "google_service_account" "eventarc" {
  account_id   = var.eventarc_service_account_name
  display_name = "CEOsystem Eventarc Trigger"
}

resource "google_service_account" "billing_reconciler" {
  account_id   = var.billing_reconciler_service_account_name
  display_name = "CEOsystem Billing Reconciler"
}

locals {
  gateway_roles = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/datastore.user",
    "roles/logging.logWriter",
    "roles/pubsub.publisher",
  ])

  worker_roles = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/datastore.user",
    "roles/logging.logWriter",
  ])

  # Firestore does not offer collection-scoped IAM for application writes.
  # This service gets no Agent Runtime, Pub/Sub, or broad Secret Manager role.
  billing_api_roles = toset([
    "roles/datastore.user",
    "roles/logging.logWriter",
  ])

  eventarc_roles = toset([
    "roles/eventarc.eventReceiver",
    "roles/logging.logWriter",
  ])
}

resource "google_project_iam_member" "gateway_roles" {
  for_each = local.gateway_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = local.worker_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "billing_api_roles" {
  for_each = local.billing_api_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.billing_api.email}"
}

resource "google_project_iam_member" "eventarc_roles" {
  for_each = local.eventarc_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.eventarc.email}"
}

# The Stripe secret is created manually in Secret Manager so its value never
# enters Terraform state. Terraform manages only this service account binding.
resource "google_secret_manager_secret_iam_member" "billing_api_stripe_secret_key_accessor" {
  project   = var.project_id
  secret_id = var.billing_api_stripe_secret_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.billing_api.email}"
}

resource "google_secret_manager_secret_iam_member" "billing_api_stripe_webhook_signing_secret_accessor" {
  count = var.billing_api_stripe_webhook_signing_secret_id == "" ? 0 : 1

  project   = var.project_id
  secret_id = var.billing_api_stripe_webhook_signing_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.billing_api.email}"
}

# The gateway needs access to the Gemini API Key secret to authenticate calls to the Interactions API
resource "google_secret_manager_secret_iam_member" "gateway_gemini_api_key_accessor" {
  count = var.gemini_api_key_secret_id == "" ? 0 : 1

  project   = var.project_id
  secret_id = var.gemini_api_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

