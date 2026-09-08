resource "google_cloud_run_v2_service" "worker" {
  name                = var.worker_service_name
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = var.worker_timeout
    max_instance_request_concurrency = var.worker_concurrency
    execution_environment            = var.cloud_run_execution_environment

    scaling {
      min_instance_count = var.worker_min_instances
      max_instance_count = var.worker_max_instances
    }

    containers {
      image = var.worker_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
        cpu_idle = var.cloud_run_request_based_billing
      }

      dynamic "env" {
        for_each = local.worker_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.worker_roles,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "worker_eventarc_invoker" {
  location = google_cloud_run_v2_service.worker.location
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.eventarc.email}"
}

resource "google_cloud_run_v2_service_iam_member" "worker_billing_reconciler_invoker" {
  location = google_cloud_run_v2_service.worker.location
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.billing_reconciler.email}"
}
