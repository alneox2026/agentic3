resource "google_cloud_run_v2_service" "gateway" {
  name                = var.gateway_service_name
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.gateway.email
    timeout                          = var.gateway_timeout
    max_instance_request_concurrency = var.gateway_concurrency
    execution_environment            = var.cloud_run_execution_environment

    scaling {
      min_instance_count = var.gateway_min_instances
      max_instance_count = var.gateway_max_instances
    }

    containers {
      image = var.gateway_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.gateway_cpu
          memory = var.gateway_memory
        }
        cpu_idle = var.cloud_run_request_based_billing
      }

      dynamic "env" {
        for_each = local.gateway_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.gateway_secret_env
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
    google_project_iam_member.gateway_roles,
    google_secret_manager_secret_iam_member.gateway_gemini_api_key_accessor,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "gateway_public_invoker" {
  location = google_cloud_run_v2_service.gateway.location
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
