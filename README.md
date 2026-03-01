# Propagate ID Entra

End-to-end identity propagation from users through AI agents to backend APIs in Azure — deployed with **Azure Developer CLI (`azd`)** and **Bicep**.

## Architecture

```
Browser (MSAL.js)
        │  Sign in → Entra ID → access token
        │  POST /api/chat { message, access_token }
        ▼
┌─────────────────────┐
│  Chat App (FastAPI)  │  Container App: ca-chat-app
│  UserTokenCredential │  Port 8080
│  Responses API (v2)  │
└────────┬────────────┘
         │  openai_client.responses.create()
         ▼
┌─────────────────────┐     ┌──────────────────┐
│  AI Foundry Agent   │────▶│  OAuth Consent   │
│  (gpt-4o)           │     │  (ApiHub)         │
└────────┬────────────┘     └──────────────────┘
         │
         │ Orders MCP
         ▼
┌──────────────────────────────────────────────────┐
│  API Management (APIM)                            │
│  ├─ Orders MCP API  (/orders-mcp)                 │
│  ├─ Orders REST API (/orders-api)                 │
│  └─ Azure OpenAI API (/openai)                    │
└────────┬─────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  Orders API      │
│  (Container App) │
│  FastAPI · 6 CRUD│
└──────────────────┘
```

An AI Foundry agent uses an **MCP tool** to call the Orders API through APIM. APIM also serves as an **AI Gateway** — proxying Azure OpenAI with managed identity auth, token rate limiting, and token metrics.

## Resource Overview

| Resource | Name Pattern | Purpose |
|----------|--------------|---------|
| AI Services Account | `aoai-{env}` | Foundry project + gpt-4o deployment |
| API Management | `apim-{env}` | Gateway — REST, MCP, and OpenAI APIs |
| Container App | `ca-orders-api` | FastAPI Orders API |
| Container App | `ca-chat-app` | Chat frontend + Foundry agent bridge |
| Monitor Workbook | `identity-propagation` | Observability dashboard |
| Container Registry | `acr{env}*` | Docker image hosting |
| Log Analytics + App Insights | `log-{env}` / `appi-{env}` | Monitoring and token metrics |
| Key Vault | `kv-{env}` | Secrets (future) |
| Storage Account | `st{env}*` | Storage (future) |

## Prerequisites

- **Azure subscription** with Owner/Contributor access
- **Azure CLI** (`az`) — logged in
- **Azure Developer CLI** (`azd`)
- **Python** 3.9+
- **Git**

Docker is not required locally — builds run remotely on ACR.

## Quick Start

```bash
# Clone and enter the repo
git clone https://github.com/ozgurkarahan/propagate-id-entra.git
cd propagate-id-entra

# Create an azd environment
azd env new propagate-id-entra

# Deploy everything (infra + app + post-provision hook)
azd up

# Verify the deployment
python scripts/verify_deployment.py

# Grant OAuth consent via device code flow
python scripts/grant-mcp-consent.py

# Test agent with OAuth identity propagation (interactive)
python scripts/test-agent-oauth.py
```

`azd up` runs Bicep provisioning, builds and deploys the Orders API and Chat App containers, then executes the post-provision hook to create Entra app registrations and the Foundry agent.

## Project Structure

```
├── azure.yaml                    # azd project manifest
├── AGENT.md                      # Architecture, auth flow diagrams
├── requirements.txt              # Python dependencies (hook + scripts)
├── infra/
│   ├── main.bicep                # Orchestrator (subscription-scoped)
│   ├── main.parameters.json      # Parameters (azd env vars)
│   ├── policies/
│   │   ├── ai-gateway-policy.xml    # APIM OpenAI proxy policy
│   │   ├── mcp-api-policy.xml       # validate-azure-ad-token + 401 challenge
│   │   └── mcp-prm-policy.xml       # RFC 9728 Protected Resource Metadata
│   └── modules/
│       ├── cognitive.bicep          # AI Services + Project + gpt-4o
│       ├── container-app.bicep      # Container Apps Environment + App
│       ├── apim.bicep               # APIM + REST & OpenAI APIs
│       ├── apim-mcp.bicep           # APIM MCP Server (native)
│       ├── registry.bicep           # Container Registry
│       ├── monitoring.bicep         # Log Analytics + App Insights
│       ├── chat-app.bicep           # Chat App Container App
│       ├── workbook.bicep           # Azure Monitor Workbook
│       ├── role-assignment.bicep    # Cognitive Services User → APIM MI
│       ├── ai-gateway-connection.bicep
│       ├── mcp-oauth-connection.bicep
│       ├── entra-apps.bicep         # Graph Bicep (reference only — see note)
│       ├── storage.bicep            # (future)
│       └── keyvault.bicep           # (future)
├── infra/workbooks/
│   └── identity-propagation.json # Workbook KQL
├── src/orders-api/
│   ├── app.py                    # FastAPI endpoints
│   ├── data.py                   # In-memory order store (8 seed orders)
│   ├── Dockerfile                # Python 3.12-slim, non-root
│   └── requirements.txt
├── src/chat-app/
│   ├── app.py                    # FastAPI backend (MSAL → Foundry bridge)
│   ├── static/index.html         # SPA chat UI with MSAL.js
│   ├── static/style.css          # Chat styling
│   ├── static/app.js             # MSAL auth + chat logic
│   ├── Dockerfile                # Python 3.12-slim, port 8080
│   └── requirements.txt
├── hooks/
│   ├── postprovision.sh          # Shell wrapper (installs deps, calls .py)
│   └── postprovision.py          # Entra apps + Foundry agent creation
├── scripts/
│   ├── verify_deployment.py          # Deployment verification
│   ├── diagnose-mcp-auth.py          # MCP OAuth diagnostic
│   ├── test-agent-oauth.py           # Interactive multi-turn agent test
│   ├── grant-mcp-consent.py          # Device code flow → OAuth refresh token
│   ├── check-signin-logs.py          # Entra sign-in log viewer (Graph API)
│   └── generate_resource_inventory.py
└── docs/
    ├── deep-dive.md              # ARM resources, data flows, step-by-step
    ├── identity-security.md      # Identity & security architecture
    ├── project-reference.md      # Technical reference
    └── lessons-learned.md        # Workflow lessons
```

## Orders API

Six CRUD endpoints running on Azure Container Apps:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/orders` | List all orders |
| `GET` | `/orders/{id}` | Get order by ID |
| `POST` | `/orders` | Create order |
| `PUT` | `/orders/{id}` | Update order |
| `DELETE` | `/orders/{id}` | Delete order |

The API ships with 8 seed orders (ORD-001 through ORD-008) stored in memory.

## Bicep Deployment Tiers

Modules deploy in dependency order:

| Tier | Modules | Depends On |
|------|---------|------------|
| 1 | cognitive, registry, monitoring, storage, keyvault | — |
| 1.5 | workbook | monitoring |
| 2 | container-app | registry, monitoring |
| 2.5 | chat-app | registry, container-app, cognitive |
| 3 | apim | container-app, cognitive |
| 3.5 | apim-mcp | apim |
| 4 | role-assignment, chat-app-role, ai-gateway-connection, mcp-oauth-connection | cognitive, apim-mcp, chat-app |

## IaC Principle

**Bicep first** — every resource Bicep can create is defined in Bicep. The post-provision hook handles only what Bicep cannot deploy:

- **Entra App Registrations** — ARM deployment identity lacks `Application.ReadWrite.All` in managed tenants; the hook uses `az ad app` with delegated permissions instead.
- **Foundry Agent** — No ARM resource type exists; created via the `azure-ai-projects` SDK.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/verify_deployment.py` | Deployment verification across infra, post-provision, OAuth, and functional layers |
| `scripts/diagnose-mcp-auth.py` | MCP OAuth diagnostic — end-to-end token flow |
| `scripts/test-agent-oauth.py` | Interactive multi-turn agent test with OAuth consent + MCP approval |
| `scripts/grant-mcp-consent.py` | Device code flow to populate the OAuth connection with a refresh token |
| `scripts/check-signin-logs.py` | Query Entra ID sign-in logs for all 3 app registrations (Graph API) |
| `scripts/generate_resource_inventory.py` | Query ARM APIs and generate a resource inventory document |

## Observability

All tiers emit telemetry to a shared Application Insights instance:

- **Browser**: App Insights JS SDK tracks page views, sign-in events, chat responses, and exceptions
- **Chat App / Orders API**: `azure-monitor-opentelemetry` auto-instruments HTTP requests, exceptions, and structured logging
- **APIM**: Gateway logs with 100% sampling; response body logging disabled (breaks MCP SSE streaming)
- **Foundry Agent**: `responsesapi` cloud role emits AI dependency records (LLM calls + MCP tool executions) — connected via Foundry portal Tracing page
- **Correlation IDs**: `session_id` (browser), `request_id` (chat-app), `X-Request-ID` (APIM → Orders API), `Mcp-Session-Id` (MCP protocol)

The Azure Monitor Workbook (`infra/workbooks/identity-propagation.json`) provides tabs covering identity propagation traces, token metrics, auth failures, MCP request patterns, E2E request flow, container app logs, errors dashboard, and OAuth audit.

## Deployment Notes

| Issue | Workaround |
|-------|------------|
| CognitiveServices name reuse after purge | `azd env set COGNITIVE_ACCOUNT_SUFFIX 2` (increment for each purge) |
| Windows charmap errors in ACR build | `az acr build --no-logs` |
| MSYS path conversion in Git Bash | `export MSYS_NO_PATHCONV=1` before `az` commands with resource ID paths |
| Managed tenant blocks local auth | All connections use `authType: 'AAD'` |
| New client secret after re-deploy | Re-run `python scripts/test-agent-oauth.py` after each `azd up` |

## Documentation

- [`AGENT.md`](AGENT.md) — Architecture, auth flow diagrams, dependency chain
- [`docs/identity-security.md`](docs/identity-security.md) — Identity & security architecture: Entra app registrations, managed identities, auth flows, OAuth consent, APIM JWT validation with RFC 9728 Protected Resource Metadata, security design decisions, and configuration reference
- [`docs/deep-dive.md`](docs/deep-dive.md) — ARM resource details, step-by-step build guide, data flows
- [`docs/project-reference.md`](docs/project-reference.md) — Technical reference for development

## License

This project is a proof of concept for educational and demonstration purposes.
