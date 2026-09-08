# Multi-Cluster Deployment & State Isolation Guide

This guide explains how to deploy and maintain isolated **Managed Agents Middleware clusters** (e.g. `managed-v1`, `gemini-prod`, `antigravity-cluster`) within the same Google Cloud Project alongside existing ADK clusters (`v3`, `v4`, `v5`) without resource collisions or state overwrites.

---

## 1. Core Architectural Isolation Principle

Each cluster in our SuperApp architecture operates as an independent, decoupled stack:

```
                  Google Cloud Project (e.g. ceo-dev123)
  ┌─────────────────────────────────┐   ┌─────────────────────────────────────┐
  │     ADK Cluster V5 Stack        │   │    Managed Agents Cluster Stack     │
  │ • ceoagent-gateway-v5           │   │ • managed-agents-gateway-v1         │
  │ • ceoagent-persistence-worker-v5│   │ • managed-agents-worker-v1          │
  │ • ceoagent-billing-api-v5       │   │ • managed-agents-billing-api-v1     │
  │ • Topic: agent-turn-events-v5   │   │ • Topic: managed-agents-events-v1   │
  │ • Firestore: agent_threads_v5   │   │ • Firestore: managed_agent_threads  │
  │ • SA: ceoagent-gateway-sa-v5    │   │ • SA: managed-agents-gateway-sa     │
  └────────────────┬────────────────┘   └──────────────────┬──────────────────┘
                   │                                       │
                   ▼                                       ▼
      gs://...-tfstate/ceodev-v5/             gs://...-tfstate/managed-agents/
```

---

## 2. Terraform State Isolation (`backend.hcl`)

The foundation of multi-cluster isolation is the **Google Cloud Storage Terraform Backend Prefix**.

Each cluster MUST write to its own unique GCS state path:

| Cluster Stack | Remote State GCS Path |
| :--- | :--- |
| **ADK V3** | `gs://<PROJECT_ID>-tfstate/ceodev-v3/middleware/default.tfstate` |
| **ADK V5** | `gs://<PROJECT_ID>-tfstate/ceodev-v5/middleware/default.tfstate` |
| **Managed Agents (Default)** | `gs://<PROJECT_ID>-tfstate/managed-agents/middleware/default.tfstate` |
| **Managed Agents (Custom Suffix)** | `gs://<PROJECT_ID>-tfstate/managed-agents-<SUFFIX>/middleware/default.tfstate` |

### How It Works Automatically
Our deployment script [`scripts/cloudshell_deploy_middleware.sh`](../scripts/cloudshell_deploy_middleware.sh) automatically generates `backend.hcl` before running Terraform:

```bash
CLUSTER_SUFFIX="${CLUSTER_SUFFIX:-}"
if [[ -n "${CLUSTER_SUFFIX}" ]]; then
  TF_STATE_PREFIX="managed-agents-${CLUSTER_SUFFIX}/middleware"
else
  TF_STATE_PREFIX="managed-agents/middleware"
fi

cat > backend.hcl <<EOF
bucket = "${PROJECT_ID}-tfstate"
prefix = "${TF_STATE_PREFIX}"
EOF

terraform init -backend-config=backend.hcl -reconfigure
```

---

## 3. Dynamic Resource Naming Checklist

All Terraform resources are fully parameterized to prevent `409 Already Exists` conflicts.

When configuring a new cluster:
1. **Cloud Run Service Names**:
   * `gateway_service_name`: `managed-agents-gateway` (or `managed-agents-gateway-<SUFFIX>`)
   * `worker_service_name`: `managed-agents-worker` (or `managed-agents-worker-<SUFFIX>`)
   * `billing_api_service_name`: `managed-agents-billing-api` (or `managed-agents-billing-api-<SUFFIX>`)
2. **Pub/Sub Topic**:
   * `pubsub_topic_name`: `managed-agents-turn-events` (or `managed-agents-turn-events-<SUFFIX>`)
3. **Service Accounts**:
   * `gateway_service_account_name`: `managed-agents-gateway-sa`
   * `worker_service_account_name`: `managed-agents-worker-sa`
   * `billing_api_service_account_name`: `managed-agents-billing-sa`
   * `eventarc_service_account_name`: `managed-agents-eventarc-sa`
   * `billing_reconciler_service_account_name`: `managed-agents-reconciler-sa`
4. **Firestore Collections**:
   * Can share existing customer wallets (`customer_wallets_v3`) so users don't have to top up multiple balances.
   * Or specify isolated collections if strict tenancy separation is required.

---

## 4. Step-by-Step: Deploying a Managed Agents Cluster

### Step A: Local Setup
1. Copy `managed-agents-middleware-template` contents into your new repository folder:
   ```powershell
   cd C:\Users\Admin\Desktop\ANTIGRAVITY\my-managed-agents-stack
   git init -b main
   git remote add origin https://github.com/YOUR_ORG/my-managed-agents-stack.git
   ```
2. In `config/agents.prod.yaml`, configure your managed agents.
3. Commit and push:
   ```powershell
   git add .
   git commit -m "feat: initial commit for managed agents cluster"
   git push -u origin main
   ```

### Step B: Cloud Shell Deployment
```bash
# 1. Clone your new repo
cd ~
git clone https://github.com/YOUR_ORG/my-managed-agents-stack.git my-managed-agents-stack
cd my-managed-agents-stack

# 2. Set Gemini API Key secret in Secret Manager (if not already created)
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-

# 3. Build Middleware Images
bash ./scripts/cloudshell_build_middleware.sh

# 4. Deploy Middleware Infrastructure
# Default deployment:
bash ./scripts/cloudshell_deploy_middleware.sh

# Or with a dedicated cluster suffix:
CLUSTER_SUFFIX=v1 bash ./scripts/cloudshell_deploy_middleware.sh
```

---

## 5. Troubleshooting & FAQs

### Q: Why did an existing cluster disappear when I deployed a new one?
* **Reason**: Both repositories were using the exact same `prefix` in `backend.hcl`. Terraform thought you were updating resources within the same state file and replaced them.
* **Fix**: Ensure each repository uses a distinct `CLUSTER_SUFFIX` or distinct `prefix` in `backend.hcl`.

### Q: Why did Terraform throw `Error 409: Service account or Service already exists`?
* **Reason**: The service account or Cloud Run service was created by another state file or manual command.
* **Fix**: Provide a unique `CLUSTER_SUFFIX` (e.g. `CLUSTER_SUFFIX=v2 bash ./scripts/cloudshell_deploy_middleware.sh`) so all resource names are unique.
