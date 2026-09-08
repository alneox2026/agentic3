resource "google_cloud_scheduler_job" "billing_reconciliation" {
  count = var.billing_reconciliation_enabled ? 1 : 0

  name             = "${var.worker_service_name}-billing-reconciliation"
  description      = "Releases expired agent-credit holds and settles finalized billing ledgers."
  region           = var.region
  schedule         = var.billing_reconciliation_schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "120s"

  retry_config {
    retry_count = 3
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.worker.uri}/internal/billing/reconcile"
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.billing_reconciler.email
      audience              = google_cloud_run_v2_service.worker.uri
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service.worker,
    google_cloud_run_v2_service_iam_member.worker_billing_reconciler_invoker,
  ]
}
