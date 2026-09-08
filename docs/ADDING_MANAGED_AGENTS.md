# How to Add & Configure Managed Agents

This guide explains how to add new Gemini Managed Agents and Antigravity agents to your cluster.

---

## 1. Types of Managed Agents

You can connect three types of managed agents through this middleware:

### Type A: The Base Antigravity Agent
The general-purpose autonomous agent with built-in code execution, file management, and Google Search in an isolated Linux sandbox:
* Base agent ID: `antigravity-preview-05-2026`
* Default model: `gemini-3.8-flash`

### Type B: Named Custom Managed Agents
Agents pre-configured and saved in your Google Cloud Project or Google AI Studio with customized prompts, tools, and skills:
* Created via `POST https://generativelanguage.googleapis.com/v1beta/agents`
* Invoked by their unique ID (e.g. `data-analyst`, `security-auditor`, `financial-modeler`)

### Type C: Inline-Configured Agents
Agents customized on-the-fly directly inside the middleware configuration using `system_instruction` and inline skill files.

---

## 2. Adding an Agent to `config/agents.prod.yaml`

To expose a new agent through the middleware, simply add an entry to `config/agents.prod.yaml`:

```yaml
agents:
  # Example 1: Named Agent created in Google Cloud / AI Studio
  my_new_agent:
    agent_id: my_new_agent               # The route ID used by your client app (/v1/agents/my_new_agent/chat)
    backend: gemini_managed              # Always gemini_managed
    remote_agent_id: financial-analyst   # The registered ID in Google
    model: gemini-3.8-flash              # Model used for pricing calculation
    max_total_tokens: 60000              # Budget cap per interaction
    streaming_enabled: true              # Real-time SSE streaming
    persistence_enabled: true            # Firestore thread & turn persistence

  # Example 2: Inline Custom Agent with System Prompt
  code_reviewer:
    agent_id: code_reviewer
    backend: gemini_managed
    remote_agent_id: antigravity-preview-05-2026
    system_instruction: "You are an expert software engineer. Always verify syntax before answering."
    model: gemini-3.8-flash
    max_total_tokens: 40000
    streaming_enabled: true
    persistence_enabled: true
```

---

## 3. How to Create a Custom Managed Agent via Google API

If you want to register a custom managed agent using curl:

```bash
curl -X POST "https://generativelanguage.googleapis.com/v1beta/agents" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -d '{
    "id": "financial-analyst",
    "base_agent": "antigravity-preview-05-2026",
    "description": "Analyzes financial spreadsheets and generates reports.",
    "system_instruction": "You are a financial analyst. Provide clear tables and summaries.",
    "agent_config": {
      "type": "antigravity",
      "model": "gemini-3.8-flash"
    }
  }'
```

Once created, set `remote_agent_id: financial-analyst` in your `config/agents.prod.yaml` and redeploy.

---

## 4. Deploying Updates

Whenever you update `config/agents.prod.yaml`:
```bash
bash ./scripts/cloudshell_build_middleware.sh
bash ./scripts/cloudshell_deploy_middleware.sh
```
The gateway will automatically reload the new agent registry.
