# Managed Agents Middleware Template (Google Antigravity & Gemini Managed Agents)

Production-ready, highly scalable middleware template for **Google Gemini Managed Agents** and **Google Antigravity Agents** (`antigravity-preview-05-2026` and custom managed agents).

This middleware serves as the unified, secure API gateway between client applications (FlutterFlow, Web, Mobile) and Google's hosted agent sandboxes, providing **real-time SSE streaming**, **atomic wallet billing**, **spend protection**, and **stateful multi-turn memory**.

---

## 🌟 Key Features

1. **Native Gemini Interactions API Integration**:
   - Directly interfaces with `POST https://generativelanguage.googleapis.com/v1beta/interactions`.
   - Free environment compute (CPU/RAM Linux sandbox) during preview; bills only for underlying model tokens.
   - Built for the default `gemini-3.8-flash` model (also supports `gemini-3.7-flash`, `gemini-3.5-flash`, etc.).

2. **Real-Time SSE Streaming**:
   - Instant TTFT (Time-To-First-Token) with low latency.
   - Streams autonomous execution deltas (`step.delta`), thought reasoning chunks, and final `interaction.completed` usage.

3. **Multi-Turn Stateful Continuity**:
   - Maps client conversation `thread_id` to Google's stateful interaction parameters (`previous_interaction_id` and `environment_id`).
   - The agent remembers previous turns, files generated inside `/workspace`, and bash script outputs across the entire chat thread.

4. **Accurate Token Normalization & Billing**:
   - Normalizes `total_input_tokens`, `total_output_tokens`, and `total_thought_tokens`.
   - Computes dollar-exact costs per turn and debits user wallets asynchronously via Pub/Sub.

5. **Native Budget Protection (`max_total_tokens`)**:
   - Enforces configurable token limits (`max_total_tokens: 50000`).
   - Automatically stops the agent when the budget limit is reached, guaranteeing users never overspend.

6. **Shared Database Compatibility**:
   - Uses the exact same Firestore collections as your ADK clusters.
   - Users can use their existing API keys, wallets, and Stripe top-up balances across all your agent clusters.

7. **Production Cloud Run Optimization**:
   - Configured with `600s` timeout for deep multi-step agentic workflows.
   - Tuned concurrency (`20`) to eliminate stream jitter and CPU starvation.

---

## 📁 Project Structure

```
├── common/                  # Shared Pydantic schemas (AgentConfig, ChatRequest, Wallet)
├── config/
│   ├── agents.dev.yaml      # Development agent registry (Antigravity & custom agents)
│   ├── agents.prod.yaml     # Production agent registry
│   ├── billing.dev.yaml     # Pricing catalog (gemini-3.8-flash: $0.75 / $3.75 per 1M)
│   └── billing.prod.yaml    # Production pricing catalog
├── docs/
│   ├── ARCHITECTURE.md      # In-depth technical architecture
│   └── ADDING_MANAGED_AGENTS.md # Guide to registering new managed agents
├── infra/terraform/         # Cloud Run, Secret Manager, Pub/Sub, and Monitoring
├── scripts/
│   ├── cloudshell_build_middleware.sh  # Builds and pushes container images
│   └── cloudshell_deploy_middleware.sh # Applies Terraform deployment
├── services/
│   ├── agent_gateway_v3/    # Public API Gateway (FastAPI + SSE streaming)
│   ├── agent_persistence_worker_v3/ # Background Pub/Sub worker for Firestore & ledger
│   └── billing_api_v3/      # Stripe Checkout & balance top-up API
└── tests/unit/              # Complete test suite (86+ unit tests)
```

---

## 🚀 Quickstart: Deploying a New Cluster

### Step 1: Create a New GitHub Repository
1. In your GitHub account, create a new repository (e.g. `v6stack` or `gemini-agents-stack`).
2. Copy the contents of this template into your new repository and push to `main`.

### Step 2: Set Your Gemini API Key in Secret Manager
In Google Cloud Shell:
```bash
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
```

### Step 3: Clone, Build & Deploy from Cloud Shell
```bash
# Clone your new stack
cd ~
git clone https://github.com/YOUR_USER/v6stack.git ceodev-v6
cd ~/ceodev-v6

# Build container images
bash ./scripts/cloudshell_build_middleware.sh

# Deploy via Terraform
bash ./scripts/cloudshell_deploy_middleware.sh
```

---

## ⚙️ Configuration: Registering Agents

In `config/agents.prod.yaml`:
```yaml
agents:
  # Base Antigravity Agent
  antigravity_agent:
    agent_id: antigravity_agent
    backend: gemini_managed
    remote_agent_id: antigravity-preview-05-2026
    model: gemini-3.8-flash
    max_total_tokens: 50000
    streaming_enabled: true
    persistence_enabled: true

  # Custom Managed Agent (created via API or Google AI Studio)
  data_analyst:
    agent_id: data_analyst
    backend: gemini_managed
    remote_agent_id: data-analyst
    model: gemini-3.8-flash
    max_total_tokens: 50000
    streaming_enabled: true
    persistence_enabled: true
```

---

## 🧪 Running Tests Locally

```bash
python -m pytest tests/unit
```
All 86 unit tests should pass.
