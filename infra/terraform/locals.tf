locals {
  enabled_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "eventarc.googleapis.com",
    "firestore.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  gateway_env = {
    GOOGLE_CLOUD_PROJECT             = var.project_id
    GOOGLE_CLOUD_REGION              = var.region
    AGENT_REGISTRY_PATH              = var.agent_registry_path
    DEFAULT_MODEL_NAME               = var.default_model_name
    AGENT_TURN_EVENTS_TOPIC          = var.pubsub_topic_name
    FIRESTORE_THREADS_COLLECTION     = var.firestore_threads_collection
    FIRESTORE_CUSTOMER_WALLETS_COLLECTION = var.firestore_customer_wallets_collection
    FIRESTORE_BILLING_RESERVATIONS_COLLECTION = var.firestore_billing_reservations_collection
    BILLING_ENFORCEMENT_ENABLED      = tostring(var.billing_enforcement_enabled)
    BILLING_RESERVATION_NANOS        = tostring(var.billing_reservation_nanos)
    BILLING_RESERVATION_TTL_SECONDS  = tostring(var.billing_reservation_ttl_seconds)
    PUBSUB_PUBLISH_TIMEOUT_SECONDS   = tostring(var.pubsub_publish_timeout_seconds)
    REQUIRE_FIREBASE_AUTH            = tostring(var.require_firebase_auth)
    ALLOWED_ORIGINS                  = join(",", var.allowed_origins)
    GATEWAY_LOG_LEVEL                = var.gateway_log_level
    GATEWAY_STREAM_DEBUG             = tostring(var.gateway_stream_debug)
    UPSTREAM_CONNECT_TIMEOUT_SECONDS = tostring(var.upstream_connect_timeout_seconds)
    UPSTREAM_READ_TIMEOUT_SECONDS    = tostring(var.upstream_read_timeout_seconds)
  }

  worker_env = {
    GOOGLE_CLOUD_PROJECT                    = var.project_id
    WORKER_LOG_LEVEL                        = var.worker_log_level
    FIRESTORE_THREADS_COLLECTION            = var.firestore_threads_collection
    FIRESTORE_MESSAGES_SUBCOLLECTION        = var.firestore_messages_subcollection
    FIRESTORE_IDEMPOTENCY_COLLECTION        = var.firestore_idempotency_collection
    FIRESTORE_BILLING_LEDGER_COLLECTION      = var.firestore_billing_ledger_collection
    FIRESTORE_CUSTOMER_WALLETS_COLLECTION    = var.firestore_customer_wallets_collection
    FIRESTORE_BILLING_RESERVATIONS_COLLECTION = var.firestore_billing_reservations_collection
    FIRESTORE_WALLET_TRANSACTIONS_COLLECTION = var.firestore_wallet_transactions_collection
    FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION = var.firestore_customer_billing_periods_collection
    MONTHLY_SERVICE_FEE_NANOS                = tostring(var.monthly_service_fee_nanos)
    BILLING_RECONCILIATION_BATCH_SIZE        = tostring(var.billing_reconciliation_batch_size)
    RUNTIME_DELETE_TIMEOUT_SECONDS          = tostring(var.runtime_delete_timeout_seconds)
    WORKER_REQUIRE_EVENTARC_AUTH            = tostring(var.worker_require_eventarc_auth)
    WORKER_EVENTARC_ALLOWED_SERVICE_ACCOUNT = var.worker_eventarc_allowed_service_account != "" ? var.worker_eventarc_allowed_service_account : google_service_account.eventarc.email
    WORKER_EVENTARC_AUDIENCE                = var.worker_eventarc_audience
  }

  billing_api_env = {
    GOOGLE_CLOUD_PROJECT                              = var.project_id
    GOOGLE_CLOUD_REGION                               = var.region
    BILLING_API_LOG_LEVEL                             = var.billing_api_log_level
    BILLING_ALLOWED_ORIGINS                           = join(",", var.billing_api_allowed_origins)
    BILLING_CATALOG_PATH                              = var.billing_api_catalog_path
    BILLING_CHECKOUT_SUCCESS_URL                       = var.billing_api_checkout_success_url
    BILLING_CHECKOUT_CANCEL_URL                        = var.billing_api_checkout_cancel_url
    BILLING_CHECKOUT_SESSION_TTL_SECONDS               = tostring(var.billing_api_checkout_session_ttl_seconds)
    STRIPE_WEBHOOK_TOLERANCE_SECONDS                   = tostring(var.billing_api_stripe_webhook_tolerance_seconds)
    FIRESTORE_CUSTOMER_WALLETS_COLLECTION             = var.firestore_customer_wallets_collection
    FIRESTORE_WALLET_TRANSACTIONS_COLLECTION          = var.firestore_wallet_transactions_collection
    FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION     = var.firestore_customer_billing_periods_collection
    FIRESTORE_CUSTOMER_BILLING_ACCOUNTS_COLLECTION    = var.firestore_customer_billing_accounts_collection
    FIRESTORE_STRIPE_WEBHOOK_EVENTS_COLLECTION        = var.firestore_stripe_webhook_events_collection
  }

  gateway_secret_env = var.gemini_api_key_secret_id == "" ? {} : {
    GEMINI_API_KEY = {
      secret  = var.gemini_api_key_secret_id
      version = var.gemini_api_key_secret_version
    }
  }

  # Pin a numbered secret version. Do not use latest for environment-variable
  # secrets because an existing Cloud Run instance resolves them at startup.
  billing_api_secret_env = merge(
    {
      STRIPE_SECRET_KEY = {
        secret  = var.billing_api_stripe_secret_key_secret_id
        version = var.billing_api_stripe_secret_key_secret_version
      }
    },
    var.billing_api_stripe_webhook_signing_secret_id == "" ? {} : {
      STRIPE_WEBHOOK_SIGNING_SECRET = {
        secret  = var.billing_api_stripe_webhook_signing_secret_id
        version = var.billing_api_stripe_webhook_signing_secret_version
      }
    },
  )
}

