resource "google_pubsub_topic" "agent_turn_events" {
  name       = var.pubsub_topic_name
  project    = var.project_id
  depends_on = [google_project_service.apis]
}
