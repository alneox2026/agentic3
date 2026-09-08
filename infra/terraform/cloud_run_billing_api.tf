resource "google_cloud_run_v2_service" "billing_api" {
  name                = var.billing_api_service_name
  location            = var.region
  deletion_protection = var.billing_api_deletion_protection
  ingress             = "INGRESS_TRAFFIC_ALL"
  description         = "Stripe Checkout and webhook API for prepaid agent credit and monthly platform fees."

  template {
    service_account                  = google_service_account.billing_api.email
    timeout                          = var.billing_api_timeout
    max_instance_request_concurrency = var.billing_api_concurrency
    execution_environment            = var.cloud_run_execution_environment

    scaling {
      min_instance_count = var.billing_api_min_instances
      max_instance_count = var.billing_api_max_instances
    }

    containers {
      image = var.billing_api_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.billing_api_cpu
          memory = var.billing_api_memory
        }
        cpu_idle = var.cloud_run_request_based_billing
      }

      dynamic "env" {
        for_each = local.billing_api_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.billing_api_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.billing_api_roles,
    google_secret_manager_secret_iam_member.billing_api_stripe_secret_key_accessor,
    google_secret_manager_secret_iam_member.billing_api_stripe_webhook_signing_secret_accessor,
  ]
}

# Stripe cannot authenticate with Cloud Run IAM. This service is public solely
# so Stripe can deliver webhooks; the future webhook route verifies the raw
# Stripe signature, and every FlutterFlow-facing route verifies Firebase Auth.
resource "google_cloud_run_v2_service_iam_member" "billing_api_public_invoker" {
  location = google_cloud_run_v2_service.billing_api.location
  name     = google_cloud_run_v2_service.billing_api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
