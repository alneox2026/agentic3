resource "google_eventarc_trigger" "worker_turn_events" {
  provider = google-beta

  name     = "${var.worker_service_name}-turn-events"
  location = var.region

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }

  transport {
    pubsub {
      topic = google_pubsub_topic.agent_turn_events.id
    }
  }

  destination {
    cloud_run_service {
      service = google_cloud_run_v2_service.worker.name
      region  = var.region
      path    = "/events/pubsub"
    }
  }

  service_account = google_service_account.eventarc.email

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.eventarc_roles,
    google_cloud_run_v2_service.worker,
    google_cloud_run_v2_service_iam_member.worker_eventarc_invoker,
  ]
}
