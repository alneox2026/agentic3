resource "google_logging_metric" "worker_retryable_failures" {
  name        = "${var.worker_service_name}_retryable_failures"
  description = "Counts retryable persistence and delete failures emitted by the worker."
  filter      = <<-EOT

resource.type="cloud_run_revision"
resource.labels.service_name="${var.worker_service_name}"
(jsonPayload.event="worker_event_persist_retryable_failure" OR jsonPayload.event="worker_thread_delete_retryable_failure")
EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "gateway_5xx" {
  display_name          = "${var.gateway_service_name} 5xx responses"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.alert_notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "The public gateway is returning 5xx responses. Check Cloud Run logs for `gateway_chat_failed`, `gateway_stream_failed`, or upstream agent failures before cutting over more traffic."
  }

  conditions {
    display_name = "Gateway 5xx count above zero"

    condition_threshold {
      filter          = <<-EOT
metric.type="run.googleapis.com/request_count"
resource.type="cloud_run_revision"
resource.label."service_name"="${var.gateway_service_name}"
metric.label."response_code_class"="5xx"
EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  user_labels = {
    component = "gateway"
    service   = "${var.gateway_service_name}"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "gateway_latency" {
  display_name          = "${var.gateway_service_name} elevated p95 latency"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.alert_notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Gateway p95 request latency is above the launch threshold. Check logs before cutting over more traffic."
  }

  conditions {
    display_name = "Gateway p95 latency above threshold"

    condition_threshold {
      filter          = <<-EOT
metric.type="run.googleapis.com/request_latencies"
resource.type="cloud_run_revision"
resource.label."service_name"="${var.gateway_service_name}"
EOT
      comparison      = "COMPARISON_GT"
      threshold_value = var.gateway_p95_latency_threshold_ms
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }

      trigger {
        count = 1
      }
    }
  }

  user_labels = {
    component = "gateway"
    service   = "${var.gateway_service_name}"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "worker_retryable_failures" {
  display_name          = "${var.worker_service_name} retryable failures"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.alert_notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "The worker reported retryable failures while persisting turns or deleting runtime sessions. Inspect `worker_event_persist_retryable_failure` and `worker_thread_delete_retryable_failure` logs and confirm Eventarc is not entering a retry loop."
  }

  conditions {
    display_name = "Worker retryable failure count above zero"

    condition_threshold {
      filter          = <<-EOT
metric.type="logging.googleapis.com/user/${google_logging_metric.worker_retryable_failures.name}"
resource.type="cloud_run_revision"
resource.label."service_name"="${var.worker_service_name}"
EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  user_labels = {
    component = "worker"
    service   = "${var.worker_service_name}"
  }

  depends_on = [
    google_project_service.apis,
    google_logging_metric.worker_retryable_failures,
  ]
}
