output "gateway_url" {
  value       = "https://${var.gateway_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  description = "Deterministic public URL for the gateway service."
}

output "worker_url" {
  value       = "https://${var.worker_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  description = "Deterministic authenticated URL for the persistence worker."
}

output "billing_api_url" {
  value       = "https://${var.billing_api_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  description = "Deterministic public URL for the Stripe Billing API."
}

output "gateway_service_uri" {
  value       = google_cloud_run_v2_service.gateway.uri
  description = "Cloud Run reported URI for the gateway service."
}

output "worker_service_uri" {
  value       = google_cloud_run_v2_service.worker.uri
  description = "Cloud Run reported URI for the persistence worker."
}

output "billing_api_service_uri" {
  value       = google_cloud_run_v2_service.billing_api.uri
  description = "Cloud Run reported URI for the Stripe Billing API."
}

output "gateway_service_account_email" {
  value       = google_service_account.gateway.email
  description = "Gateway runtime service account email."
}

output "worker_service_account_email" {
  value       = google_service_account.worker.email
  description = "Worker runtime service account email."
}

output "billing_api_service_account_email" {
  value       = google_service_account.billing_api.email
  description = "Billing API runtime service account email."
}

output "eventarc_service_account_email" {
  value       = google_service_account.eventarc.email
  description = "Eventarc trigger service account email."
}

output "billing_reconciler_service_account_email" {
  value       = google_service_account.billing_reconciler.email
  description = "Cloud Scheduler service account authorized to invoke billing reconciliation."
}

output "agent_turn_events_topic" {
  value       = google_pubsub_topic.agent_turn_events.id
  description = "Pub/Sub topic id for completed turn events."
}

output "eventarc_trigger_name" {
  value       = google_eventarc_trigger.worker_turn_events.name
  description = "Eventarc trigger name for worker delivery."
}

output "gateway_5xx_alert_policy_name" {
  value       = google_monitoring_alert_policy.gateway_5xx.name
  description = "Cloud Monitoring alert policy for gateway 5xx responses."
}

output "gateway_latency_alert_policy_name" {
  value       = google_monitoring_alert_policy.gateway_latency.name
  description = "Cloud Monitoring alert policy for elevated gateway p95 latency."
}

output "worker_retry_alert_policy_name" {
  value       = google_monitoring_alert_policy.worker_retryable_failures.name
  description = "Cloud Monitoring alert policy for worker retryable failures."
}
