variable "project_id" {
  description = "Google Cloud project ID."
  type        = string
  default     = "ceo-dev123"
}

variable "region" {
  description = "Primary deployment region."
  type        = string
  default     = "us-central1"
}

variable "gateway_service_name" {
  description = "Cloud Run service name for the public agent gateway."
  type        = string
  default     = "agentic3-gateway"
}

variable "worker_service_name" {
  description = "Cloud Run service name for the persistence worker."
  type        = string
  default     = "agentic3-worker"
}

variable "billing_api_service_name" {
  description = "Cloud Run service name for the public Stripe Billing API."
  type        = string
  default     = "agentic3-billing"
}

variable "gateway_image" {
  description = "Container image URI for the gateway service."
  type        = string
}

variable "worker_image" {
  description = "Container image URI for the persistence worker."
  type        = string
}

variable "billing_api_image" {
  description = "Container image URI for the Stripe Billing API."
  type        = string
}

variable "gateway_service_account_name" {
  description = "Service account name for the gateway service."
  type        = string
  default     = "agentic3-gateway-sa"
}

variable "worker_service_account_name" {
  description = "Service account name for the worker service."
  type        = string
  default     = "agentic3-worker-sa"
}

variable "billing_api_service_account_name" {
  description = "Service account name for the Stripe Billing API."
  type        = string
  default     = "agentic3-billing-sa"
}

variable "eventarc_service_account_name" {
  description = "Service account name for the Eventarc trigger."
  type        = string
  default     = "agentic3-eventarc-sa"
}

variable "billing_reconciler_service_account_name" {
  description = "Service account name for the Cloud Scheduler billing reconciler."
  type        = string
  default     = "agentic3-reconciler-sa"
}


variable "default_model_name" {
  description = "Default Gemini model for usage pricing if not specified by agent registry or event payload."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "pubsub_topic_name" {
  description = "Topic for completed turn events."
  type        = string
  default     = "agentic3-turn-events"
}

variable "pubsub_publish_timeout_seconds" {
  description = "Pub/Sub publish timeout for the gateway."
  type        = number
  default     = 30
}

variable "require_firebase_auth" {
  description = "Whether the gateway requires Firebase bearer tokens."
  type        = bool
  default     = true
}

variable "allowed_origins" {
  description = "Allowed CORS origins for the public gateway."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for origin in var.allowed_origins : trimspace(origin) != "*"
    ])
    error_message = "Wildcard CORS origins are not allowed. Provide explicit web origins instead."
  }
}

variable "agent_registry_path" {
  description = "Absolute path inside the container to the production agent registry."
  type        = string
  default     = "/app/config/agents.prod.yaml"
}

variable "gateway_log_level" {
  description = "Gateway application log level."
  type        = string
  default     = "INFO"
}

variable "gateway_stream_debug" {
  description = "Whether to emit one-shot upstream stream diagnostics from the gateway."
  type        = bool
  default     = false
}

variable "worker_log_level" {
  description = "Worker application log level."
  type        = string
  default     = "INFO"
}

variable "runtime_delete_timeout_seconds" {
  description = "Worker timeout for Agent Runtime session delete operations."
  type        = number
  default     = 30
}

variable "upstream_connect_timeout_seconds" {
  description = "Gateway connect timeout to Agent Runtime."
  type        = number
  default     = 10
}

variable "upstream_read_timeout_seconds" {
  description = "Gateway read timeout to Agent Runtime."
  type        = number
  default     = 240
}

variable "firestore_threads_collection" {
  description = "Top-level Firestore collection for chat threads."
  type        = string
  default     = "agentic3_threads"
}

variable "firestore_messages_subcollection" {
  description = "Subcollection name for messages under each thread document."
  type        = string
  default     = "messages_agentic3"
}


variable "firestore_idempotency_collection" {
  description = "Top-level Firestore collection for processed event ids."
  type        = string
  default     = "processed_events_agentic3"
}

variable "firestore_billing_ledger_collection" {
  description = "Top-level immutable Firestore collection for completed-turn billing ledgers."
  type        = string
  default     = "agent_billing_ledger_agentic3"
}

variable "firestore_customer_wallets_collection" {
  description = "Top-level Firestore collection for server-owned prepaid customer wallets."
  type        = string
  default     = "customer_wallets_agentic3"
}

variable "firestore_billing_reservations_collection" {
  description = "Top-level Firestore collection for per-turn prepaid-credit reservations."
  type        = string
  default     = "billing_reservations_agentic3"
}

variable "firestore_wallet_transactions_collection" {
  description = "Top-level immutable Firestore collection for customer wallet transactions."
  type        = string
  default     = "wallet_transactions_agentic3"
}

variable "firestore_customer_billing_periods_collection" {
  description = "Top-level Firestore collection for customer monthly billing aggregates."
  type        = string
  default     = "customer_billing_periods_agentic3"
}

variable "firestore_customer_billing_accounts_collection" {
  description = "Top-level private Firestore collection mapping billing subjects to Stripe customer and subscription state."
  type        = string
  default     = "customer_billing_accounts_agentic3"
}

variable "firestore_stripe_webhook_events_collection" {
  description = "Top-level private Firestore collection for immutable Stripe webhook event receipts."
  type        = string
  default     = "stripe_webhook_events_agentic3"
}


variable "billing_api_stripe_secret_key_secret_id" {
  description = "Existing Secret Manager secret ID containing the Stripe secret API key. The value is never managed by Terraform."
  type        = string
  default     = "stripe-secret-key"

  validation {
    condition     = trimspace(var.billing_api_stripe_secret_key_secret_id) != ""
    error_message = "billing_api_stripe_secret_key_secret_id must not be empty."
  }
}

variable "billing_api_stripe_secret_key_secret_version" {
  description = "Pinned numeric version of the Stripe secret-key Secret Manager secret."
  type        = string
  default     = "1"

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.billing_api_stripe_secret_key_secret_version))
    error_message = "billing_api_stripe_secret_key_secret_version must be a positive numeric Secret Manager version."
  }
}

variable "billing_api_stripe_webhook_signing_secret_id" {
  description = "Secret Manager secret ID for the Stripe webhook signing secret."
  type        = string
  default     = "stripe-webhook-signing-secret-v3"
}

variable "billing_api_stripe_webhook_signing_secret_version" {
  description = "Pinned numeric version of the Stripe webhook-signing Secret Manager secret when configured."
  type        = string
  default     = "1"

  validation {
    condition = (
      var.billing_api_stripe_webhook_signing_secret_id == "" && var.billing_api_stripe_webhook_signing_secret_version == ""
    ) || can(regex("^[1-9][0-9]*$", var.billing_api_stripe_webhook_signing_secret_version))
    error_message = "Set a positive numeric billing_api_stripe_webhook_signing_secret_version when configuring its secret ID."
  }
}


variable "billing_api_allowed_origins" {
  description = "Explicit browser origins allowed to call Firebase-authenticated Billing API endpoints."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for origin in var.billing_api_allowed_origins : trimspace(origin) != "*"
    ])
    error_message = "Wildcard billing API CORS origins are not allowed. Provide explicit web origins instead."
  }
}

variable "billing_api_catalog_path" {
  description = "Absolute container path to the server-owned Stripe pricing catalog."
  type        = string
  default     = "/app/config/billing.test.yaml"
}

variable "billing_api_checkout_success_url" {
  description = "HTTPS return URL after Stripe Checkout. It must contain {CHECKOUT_SESSION_ID}. Leave empty until the FlutterFlow route exists."
  type        = string
  default     = ""

  validation {
    condition = var.billing_api_checkout_success_url == "" || (
      startswith(var.billing_api_checkout_success_url, "https://") &&
      strcontains(var.billing_api_checkout_success_url, "{CHECKOUT_SESSION_ID}")
    )
    error_message = "billing_api_checkout_success_url must be HTTPS and include {CHECKOUT_SESSION_ID}, or be empty before Checkout is configured."
  }
}

variable "billing_api_checkout_cancel_url" {
  description = "HTTPS return URL when the user cancels Stripe Checkout. Leave empty until the FlutterFlow route exists."
  type        = string
  default     = ""

  validation {
    condition     = var.billing_api_checkout_cancel_url == "" || startswith(var.billing_api_checkout_cancel_url, "https://")
    error_message = "billing_api_checkout_cancel_url must be HTTPS, or be empty before Checkout is configured."
  }
}

variable "billing_api_checkout_session_ttl_seconds" {
  description = "Lifetime for a server-created Stripe Checkout Session. Stripe requires at least 30 minutes."
  type        = number
  default     = 1800

  validation {
    condition     = var.billing_api_checkout_session_ttl_seconds >= 1800 && var.billing_api_checkout_session_ttl_seconds <= 86400
    error_message = "billing_api_checkout_session_ttl_seconds must be between 1800 and 86400."
  }
}

variable "billing_api_stripe_webhook_tolerance_seconds" {
  description = "Maximum age accepted by Stripe signature verification. Keep Stripe's 300-second default unless there is an incident-approved reason to change it."
  type        = number
  default     = 300

  validation {
    condition     = var.billing_api_stripe_webhook_tolerance_seconds >= 60 && var.billing_api_stripe_webhook_tolerance_seconds <= 900
    error_message = "billing_api_stripe_webhook_tolerance_seconds must be between 60 and 900."
  }
}

variable "billing_api_log_level" {
  description = "Billing API application log level."
  type        = string
  default     = "INFO"
}

variable "billing_enforcement_enabled" {
  description = "Whether the gateway must reserve prepaid credit before every agent request."
  type        = bool
  default     = true
}

variable "billing_reservation_nanos" {
  description = "Conservative per-turn USD prepaid-credit hold in nanos; 50000000 is USD 0.05."
  type        = number
  default     = 50000000
}

variable "billing_reservation_ttl_seconds" {
  description = "How long an unfinalized per-turn reservation remains held before reconciliation."
  type        = number
  default     = 3600
}

variable "monthly_service_fee_nanos" {
  description = "Monthly service fee recorded for each customer billing period; 5000000000 is USD 5.00."
  type        = number
  default     = 5000000000
}

variable "billing_reconciliation_batch_size" {
  description = "Maximum expired billing reservations processed by one scheduled reconciliation run."
  type        = number
  default     = 100
}

variable "billing_reconciliation_schedule" {
  description = "UTC cron schedule for reconciling finalized or expired billing reservations."
  type        = string
  default     = "*/15 * * * *"
}

variable "billing_reconciliation_enabled" {
  description = "Whether Terraform should schedule expired-reservation reconciliation on the worker service."
  type        = bool
  default     = false
}

variable "gateway_min_instances" {
  description = "Minimum number of gateway instances."
  type        = number
  default     = 0
}


variable "gateway_max_instances" {
  description = "Maximum number of gateway instances."
  type        = number
  default     = 20
}

variable "gateway_concurrency" {
  description = "Maximum concurrent requests per gateway instance."
  type        = number
  default     = 20
}

variable "gateway_cpu" {
  description = "Gateway CPU limit."
  type        = string
  default     = "1"
}

variable "gateway_memory" {
  description = "Gateway memory limit."
  type        = string
  default     = "1Gi"
}

variable "gateway_timeout" {
  description = "Gateway request timeout."
  type        = string
  default     = "600s"
}

variable "gemini_api_key_secret_id" {
  description = "Existing Secret Manager secret ID containing the Google Gemini API key."
  type        = string
  default     = "gemini-api-key"
}

variable "gemini_api_key_secret_version" {
  description = "Pinned numeric version of the Gemini API key Secret Manager secret."
  type        = string
  default     = "1"
}

variable "gemini_api_key" {
  description = "Direct Gemini API key (optional fallback when Secret Manager is not used)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloud_run_execution_environment" {
  description = "Pinned Cloud Run execution environment for both services."
  type        = string
  default     = "EXECUTION_ENVIRONMENT_GEN2"
}

variable "cloud_run_request_based_billing" {
  description = "Whether Cloud Run CPU should be allocated only during requests."
  type        = bool
  default     = true
}

variable "worker_min_instances" {
  description = "Minimum number of worker instances."
  type        = number
  default     = 0
}

variable "worker_max_instances" {
  description = "Maximum number of worker instances."
  type        = number
  default     = 20
}

variable "worker_concurrency" {
  description = "Maximum concurrent requests per worker instance."
  type        = number
  default     = 8
}

variable "worker_cpu" {
  description = "Worker CPU limit."
  type        = string
  default     = "1"
}

variable "worker_memory" {
  description = "Worker memory limit."
  type        = string
  default     = "512Mi"
}

variable "worker_timeout" {
  description = "Worker request timeout."
  type        = string
  default     = "120s"
}

variable "billing_api_min_instances" {
  description = "Minimum Billing API instances. Keep 0 in test; set 1 in production when cold-start latency is unacceptable."
  type        = number
  default     = 0
}

variable "billing_api_max_instances" {
  description = "Maximum Billing API instances allowed to handle concurrent Checkout and webhook traffic. Keep the test default within the project's regional CPU allocation quota."
  type        = number
  default     = 20
}

variable "billing_api_concurrency" {
  description = "Maximum simultaneous Billing API requests per instance. Tune with a staged webhook and Checkout load test."
  type        = number
  default     = 32
}

variable "billing_api_cpu" {
  description = "Billing API CPU limit."
  type        = string
  default     = "1"
}

variable "billing_api_memory" {
  description = "Billing API memory limit."
  type        = string
  default     = "512Mi"
}

variable "billing_api_timeout" {
  description = "Billing API request timeout; handlers must return a verified webhook result promptly."
  type        = string
  default     = "60s"
}

variable "billing_api_deletion_protection" {
  description = "Whether Terraform must block deletion of the Billing API Cloud Run service. Enable for production."
  type        = bool
  default     = false
}

variable "worker_require_eventarc_auth" {
  description = "Whether the worker verifies Eventarc OIDC tokens in application code."
  type        = bool
  default     = false
}

variable "worker_eventarc_allowed_service_account" {
  description = "Expected Eventarc service account email for worker push requests. Defaults to the Terraform-managed Eventarc service account when empty."
  type        = string
  default     = ""
}

variable "worker_eventarc_audience" {
  description = "Expected OIDC audience for worker push requests when application-level Eventarc auth is enabled."
  type        = string
  default     = ""
}

variable "alert_notification_channels" {
  description = "Cloud Monitoring notification channel resource names for alert policies."
  type        = list(string)
  default     = []
}

variable "gateway_p95_latency_threshold_ms" {
  description = "Gateway p95 latency threshold in milliseconds for launch alerting."
  type        = number
  default     = 8000
}
