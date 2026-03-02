# Identity Propagation PoC — Deep Dive

A comprehensive reference covering every ARM resource, how the project was built step-by-step, dependency graph, data flows, and deployment automation.

> End-to-end identity propagation with JWT validation via Entra ID.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Step-by-Step — How the Project Was Built](#2-step-by-step--how-the-project-was-built)
3. [Complete ARM Resource Inventory](#3-complete-arm-resource-inventory)
4. [Dependency Graph](#4-dependency-graph)
5. [Resource Details](#5-resource-details)
6. [Data Flows](#6-data-flows)
7. [What Runs Outside ARM](#7-what-runs-outside-arm)
8. [Deployment Automation](#8-deployment-automation)

---

## 1. Project Overview

### What This PoC Validates

End-to-end **identity propagation** from users through AI agents to backend APIs in Azure:

```
User → AI Foundry Agent (gpt-4o) → APIM (MCP Gateway + AI Gateway) → Backend API / Azure OpenAI
```

Phase 1 proved the data flow works. Phase 2 added JWT validation with `validate-jwt` on the MCP endpoint and verified user identity propagation end-to-end via App Insights. Phase 3 added the chat frontend and observability.

### Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| IaC | Azure Developer CLI (`azd`) + Bicep | Provision + deploy in one command |
| API | FastAPI (Python 3.12) on Container Apps | Orders CRUD backend |
| Gateway | Azure API Management (StandardV2) | REST proxy, MCP server, AI Gateway |
| AI | Azure AI Foundry (CognitiveServices pattern) | Agent hosting, gpt-4o, MCP tool calling |
| Monitoring | Log Analytics + Application Insights | Logging, token usage metrics |
| Future | Key Vault, Storage | Phase 2+ (secrets, state) |

### What's Included (Phase 1 + 1.5 + 2 + 3)

- Orders API running as a Container App with 5 CRUD endpoints + health check
- APIM imports the Orders API and exposes it as both REST and MCP server
- APIM proxies Azure OpenAI traffic (AI Gateway) with managed identity auth, token rate limiting, and token metrics
- APIM validates JWT tokens on MCP requests (`validate-jwt` policy + 401 challenge)
- RFC 9728 Protected Resource Metadata endpoint on APIM
- AI Foundry agent (gpt-4o) connects to the MCP server using UserEntraToken connection — no OAuth consent flow
- Chat App Entra registration created by postprovision hook
- User identity propagated end-to-end via UserEntraToken passthrough
- `ApiManagement` connection on the AI Services account links APIM as the AI Gateway in the Foundry portal
- Chat App (FastAPI + MSAL.js) with `UserTokenCredential` for end-to-end identity propagation
- Azure Monitor Workbook with 8-tab observability dashboard

---

## 2. Step-by-Step — How the Project Was Built

This section walks through the entire build in the order a reader would recreate it from scratch.

### Step 1: azd Project Scaffold

**Files:** `azure.yaml`, `infra/main.bicep`, `infra/main.bicepparam`

The `azure.yaml` manifest defines the azd project:

```yaml
name: identity-poc
services:
  orders-api:
    project: ./src/orders-api
    language: docker
    host: containerapp
    docker:
      path: ./Dockerfile
      remoteBuild: true
hooks:
  postprovision:
    shell: sh
    run: hooks/postprovision.sh
```

- `services.orders-api` tells azd to build the Docker image and deploy to a Container App
- `hooks.postprovision` runs after `azd provision` to create resources Bicep can't handle
- `remoteBuild: true` builds the Docker image in ACR (not locally)

`infra/main.bicep` is the orchestrator that wires all Bicep modules together at `subscription` scope. It creates a single resource group and deploys all modules into it with a 4-tier dependency chain.

`infra/main.bicepparam` reads `AZURE_ENV_NAME` and `AZURE_LOCATION` from environment variables, defaulting to `identity-poc` and `swedencentral`.

**ARM resources created:** `Microsoft.Resources/resourceGroups` (`rg-identity-poc`)

### Step 2: Orders API

**Files:** `src/orders-api/app.py`, `src/orders-api/data.py`, `src/orders-api/Dockerfile`, `src/orders-api/requirements.txt`

A FastAPI application with in-memory CRUD operations:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/orders` | GET | List all orders |
| `/orders/{order_id}` | GET | Get order by ID |
| `/orders` | POST | Create new order |
| `/orders/{order_id}` | PUT | Update order |
| `/orders/{order_id}` | DELETE | Delete order |

The `data.py` module seeds 8 sample orders (ORD-001 through ORD-008) at startup. The Dockerfile uses a multi-stage build with `python:3.12-slim`, runs as non-root `appuser`, and includes a health check.

**Why it exists:** The backend API that the MCP server and AI agent interact with. Provides the "real work" that identity propagation will protect in later phases.

### Step 3: Monitoring Module

**File:** `infra/modules/monitoring.bicep`

Creates the observability stack:

| Resource | ARM Type | Name |
|----------|----------|------|
| Log Analytics Workspace | `Microsoft.OperationalInsights/workspaces@2023-09-01` | `log-identity-poc` |
| Application Insights | `Microsoft.Insights/components@2020-02-02` | `appi-identity-poc` |

- Log Analytics uses `PerGB2018` SKU with 30-day retention
- App Insights is linked to the Log Analytics workspace via `WorkspaceResourceId`

**Why it exists:** Container Apps Environment requires a Log Analytics workspace for logging. App Insights receives token usage metrics from the AI Gateway policy.

**Outputs:** `logAnalyticsWorkspaceId`, `appInsightsId`, `appInsightsConnectionString`

### Step 4: Container Registry Module

**File:** `infra/modules/registry.bicep`

| Resource | ARM Type | Name |
|----------|----------|------|
| Container Registry | `Microsoft.ContainerRegistry/registries@2023-11-01-preview` | `acridentitypoc{uniqueString}` |

- `Basic` SKU, `adminUserEnabled: true` (for Container App to pull images)
- Name uses `uniqueString(subscription().id, baseName, location)` suffix for global uniqueness — ACR names cannot contain hyphens

**Why it exists:** Stores the Docker image for the Orders API. `azd deploy` builds and pushes here via `az acr build`.

**Outputs:** `registryId`, `registryName`, `registryLoginServer`

### Step 5: Container App Module

**File:** `infra/modules/container-app.bicep`

| Resource | ARM Type | Name |
|----------|----------|------|
| Container Apps Environment | `Microsoft.App/managedEnvironments@2024-03-01` | `cae-identity-poc` |
| Container App | `Microsoft.App/containerApps@2024-03-01` | `ca-orders-api` |

**Depends on:** registry (login server, name), monitoring (Log Analytics workspace ID)

- Environment connects to Log Analytics for logging (using workspace customerId and sharedKey)
- Container App starts with a placeholder image (`containerapps-helloworld`); `azd deploy` replaces it with the Orders API image
- Ingress: external, port 8000, HTTPS only
- Resources: 0.25 vCPU, 0.5 Gi memory, 1–3 replicas
- ACR credentials passed via `registries` config and `acr-password` secret
- Tagged with `azd-service-name: orders-api` so azd knows which service to deploy here

**Outputs:** `containerAppFqdn`, `containerAppName`

### Step 6: Cognitive Module

**File:** `infra/modules/cognitive.bicep`

Creates the entire AI Services stack as a single module:

| Resource | ARM Type | API Version | Name |
|----------|----------|-------------|------|
| AI Services Account | `Microsoft.CognitiveServices/accounts` | `2025-04-01-preview` | `aoai-identity-poc3` |
| AI Foundry Project | `Microsoft.CognitiveServices/accounts/projects` | `2025-04-01-preview` | `aiproj-identity-poc` |
| AzureOpenAI Connection | `Microsoft.CognitiveServices/accounts/connections` | `2025-04-01-preview` | `aoai-connection` |
| gpt-4o Deployment | `Microsoft.CognitiveServices/accounts/deployments` | `2024-10-01` | `gpt-4o` |

Key design decisions:
- Uses `kind: 'AIServices'` with `allowProjectManagement: true` — this is the **CognitiveServices pattern**, not the ML Hub/Project pattern. Avoids portal activation issues in managed tenants.
- `SystemAssigned` managed identity on the account
- Connection uses `authType: 'AAD'` and `isSharedToAll: true` — it's a self-referencing connection pointing to the account's own OpenAI endpoint
- gpt-4o deployment: `Standard` SKU, capacity 10, model version `2024-08-06`. gpt-4o is specifically required because other models don't support MCP tools.
- Account name suffix is parameterized (`COGNITIVE_ACCOUNT_SUFFIX`, default `3`) — increment after `azd down --purge` to avoid caching issues

**Outputs:** `cognitiveAccountId`, `cognitiveAccountName`, `cognitiveEndpoint`, `openaiEndpoint`, `projectId`, `projectName`, `projectEndpoint`

### Step 7: APIM Module

**File:** `infra/modules/apim.bicep`

The largest module — creates the API Management instance with two backends, two APIs, and 9 operations:

| Resource | ARM Type | Name |
|----------|----------|------|
| APIM Instance | `Microsoft.ApiManagement/service@2024-06-01-preview` | `apim-identity-poc` |
| Orders Backend | `Microsoft.ApiManagement/service/backends` | `orders-api-backend` |
| OpenAI Backend | `Microsoft.ApiManagement/service/backends` | `openai-backend` |
| Orders API | `Microsoft.ApiManagement/service/apis` | `orders-api` |
| Azure OpenAI API | `Microsoft.ApiManagement/service/apis` | `azure-openai` |
| 6 Orders API operations | `Microsoft.ApiManagement/service/apis/operations` | `list-orders`, `get-order`, `create-order`, `update-order`, `delete-order`, `health-check` |
| 3 OpenAI API operations | `Microsoft.ApiManagement/service/apis/operations` | `chat-completions`, `completions`, `embeddings` |

**Depends on:** containerApp (FQDN for orders backend URL), cognitive (OpenAI endpoint for AI Gateway backend URL)

- `StandardV2` SKU, capacity 1
- `SystemAssigned` managed identity (used for MI auth to Azure OpenAI)
- Orders API: path `/orders-api`, no subscription required, backend URL = Container App FQDN
- Azure OpenAI API: path `/openai`, policy loaded via `loadTextContent('../policies/ai-gateway-policy.xml')` with `rawxml` format
- OpenAI backend URL = `{cognitiveEndpoint}openai` (the cognitive account's OpenAI Language Model Instance API endpoint + `/openai`)

**Note:** The MCP API (`orders-mcp`) is created by the separate `apim-mcp.bicep` module (Tier 3.5), which references the operations defined here as `existing` resources.

**Outputs:** `apimGatewayUrl`, `apimName`, `apimPrincipalId`, `apimResourceId`, `ordersApiPath`

### Step 8: APIM MCP Module

**File:** `infra/modules/apim-mcp.bicep`

Deploys the MCP Server API using Bicep with the `2025-03-01-preview` API version:

| Resource | ARM Type | Name |
|----------|----------|------|
| MCP API | `Microsoft.ApiManagement/service/apis@2025-03-01-preview` | `orders-mcp` |

- Uses both `apiType: 'mcp'` and `type: 'mcp'` in properties
- `mcpTools` array maps 6 tool names to REST API operation ARM resource IDs (via `existing` resource references)
- Tools: `list-orders`, `get-order`, `create-order`, `update-order`, `delete-order`, `health-check`
- MCP endpoint: `{apimGatewayUrl}/orders-mcp/mcp`
- Bicep shows BCP037 warnings for `apiType`, `type`, `mcpTools` — expected and safe to ignore

**Depends on:** apim (APIM instance + Orders API operations must exist first)

### Step 8b: Post-Provision Hook

**Files:** `hooks/postprovision.sh` (shell wrapper), `hooks/postprovision.py` (Python logic)

Handles everything that Bicep cannot deploy:

**Step 1: Create Chat App Entra registration** (via `az` CLI — delegated permissions):
- Creates Chat App SPA app with redirect URIs for localhost and deployed FQDN
- PATCH `requiredResourceAccess` for Azure AI Services (`user_impersonation`)
- Creates service principal
- Idempotent — checks by `displayName` before creating

**Step 2: Create Foundry Agent** (via `azure-ai-projects` SDK):
- Creates an agent named `orders-assistant` using `gpt-4o`
- Attaches an MCP tool pointing to the APIM MCP endpoint (provisioned by Bicep)
- `server_label: "orders_mcp"` (underscores only — no hyphens)
- `project_connection_id` references the `mcp-entra` UserEntraToken connection

**Step 3: Update Chat App settings** — sets `CHAT_APP_ENTRA_CLIENT_ID`, `TENANT_ID`, `AGENT_NAME` on the container app

The shell wrapper installs Python dependencies (`azure-ai-projects`, `azure-ai-agents`, `azure-identity`) and loads azd environment variables before running the Python script.

### Step 9: AI Gateway Addition

**Files:** `infra/modules/apim.bicep` (OpenAI backend + API + operations), `infra/policies/ai-gateway-policy.xml`

Added to the APIM module (Step 7 above). The AI Gateway makes APIM proxy Azure OpenAI traffic with:

1. **Managed identity authentication** — `authentication-managed-identity` policy authenticates to Azure OpenAI using APIM's system-assigned MI
2. **Token rate limiting** — `azure-openai-token-limit` at 10,000 tokens per minute per client IP, with estimated prompt tokens
3. **Token usage metrics** — `llm-emit-token-metric` emits metrics with dimensions: API ID, Operation ID, Client IP, Deployment
4. **Backend routing** — `set-backend-service` routes to the `openai-backend`

Policy uses `rawxml` format because C# expressions contain double quotes that conflict with XML attribute quoting (e.g., `@(context.Request.MatchedParameters["deployment-id"])`).

### Step 10: Role Assignment

**File:** `infra/modules/role-assignment.bicep`

| Resource | ARM Type | Name |
|----------|----------|------|
| Role Assignment | `Microsoft.Authorization/roleAssignments@2022-04-01` | `guid(cognitiveAccount.id, principalId, roleDefinitionId)` |

**Depends on:** apim (principal ID), cognitive (account name)

- Assigns `Cognitive Services User` role (GUID: `a97b65f3-24c7-4388-baec-2e87135dc908`) to APIM's managed identity
- Scoped to the cognitive account (not the resource group or subscription)
- Name generated by `guid()` for idempotent deployment

**Why it exists:** APIM needs this role to authenticate to Azure OpenAI via managed identity when proxying AI Gateway requests.

### Step 11: AI Gateway Connection

**File:** `infra/modules/ai-gateway-connection.bicep`

| Resource | ARM Type | Name |
|----------|----------|------|
| ApiManagement Connection | `Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview` | `apim-gateway` |

**Depends on:** apim (gateway URL, resource ID), cognitive (account name)

- Category: `ApiManagement` (not `AzureOpenAI` — this is a different connection type)
- Auth: `AAD`, shared to all projects
- Target: `{apimGatewayUrl}/openai`
- Metadata includes the APIM resource ID

**Why it exists:** Links APIM as the AI Gateway in the Foundry portal. When this connection exists, the Foundry portal shows APIM as the gateway for the project, and AI Gateway metrics flow through APIM.

### Step 12: Storage & Key Vault

**Files:** `infra/modules/storage.bicep`, `infra/modules/keyvault.bicep`

| Resource | ARM Type | Name |
|----------|----------|------|
| Storage Account | `Microsoft.Storage/storageAccounts@2023-05-01` | `stidentitypoc{uniqueString}` |
| Key Vault | `Microsoft.KeyVault/vaults@2023-07-01` | `kv-identity-poc` |

- Storage: `Standard_LRS`, `StorageV2`, TLS 1.2, no public blob access
- Key Vault: standard SKU, RBAC authorization, soft delete (7 days)

**Why they exist:** Placeholders for Phase 2+. Storage will hold state; Key Vault will hold secrets and certificates for OAuth flows. Not wired to anything in Phase 1.

---

## 3. Complete ARM Resource Inventory

Every ARM resource in the deployed project:

```
Subscription (<sub-id> — <subscription-name>)
│
└── Microsoft.Resources/resourceGroups
    └── rg-identity-poc
        │
        ├── MONITORING
        │   ├── Microsoft.OperationalInsights/workspaces (log-identity-poc)
        │   └── Microsoft.Insights/components (appi-identity-poc)
        │
        ├── CONTAINER PLATFORM
        │   ├── Microsoft.ContainerRegistry/registries (acridentitypoc{unique})
        │   ├── Microsoft.App/managedEnvironments (cae-identity-poc)
        │   ├── Microsoft.App/containerApps (ca-orders-api)
        │   └── Microsoft.App/containerApps (ca-chat-app)
        │
        ├── AI SERVICES
        │   └── Microsoft.CognitiveServices/accounts (aoai-identity-poc3)
        │       ├── /projects/aiproj-identity-poc
        │       ├── /connections/aoai-connection
        │       ├── /connections/apim-gateway
        │       ├── /connections/mcp-entra (UserEntraToken)
        │       └── /deployments/gpt-4o
        │
        ├── API MANAGEMENT
        │   └── Microsoft.ApiManagement/service (apim-identity-poc)
        │       ├── /backends/orders-api-backend
        │       ├── /backends/openai-backend
        │       ├── /apis/orders-api
        │       │   ├── /operations/list-orders
        │       │   ├── /operations/get-order
        │       │   ├── /operations/create-order
        │       │   ├── /operations/update-order
        │       │   ├── /operations/delete-order
        │       │   └── /operations/health-check
        │       ├── /apis/azure-openai (+ policy)
        │       │   ├── /operations/chat-completions
        │       │   ├── /operations/completions
        │       │   └── /operations/embeddings
        │       └── /apis/orders-mcp (+ 6 mcpTools) [apim-mcp.bicep]
        │
        ├── ACCESS CONTROL
        │   └── Microsoft.Authorization/roleAssignments
        │       └── Cognitive Services User → APIM managed identity
        │
        ├── STORAGE (Phase 2)
        │   └── Microsoft.Storage/storageAccounts (stidentitypoc{unique})
        │
        └── KEY VAULT (Phase 2)
            └── Microsoft.KeyVault/vaults (kv-identity-poc)
```

### Resource Detail Table

| # | Resource | ARM Type | API Version | Created By | SKU / Kind | Identity |
|---|----------|----------|-------------|------------|-----------|----------|
| 1 | `rg-identity-poc` | `Microsoft.Resources/resourceGroups` | `2024-03-01` | `main.bicep` | — | — |
| 2 | `log-identity-poc` | `Microsoft.OperationalInsights/workspaces` | `2023-09-01` | `monitoring.bicep` | PerGB2018 | — |
| 3 | `appi-identity-poc` | `Microsoft.Insights/components` | `2020-02-02` | `monitoring.bicep` | web | — |
| 4 | `acridentitypoc{unique}` | `Microsoft.ContainerRegistry/registries` | `2023-11-01-preview` | `registry.bicep` | Basic | — |
| 5 | `cae-identity-poc` | `Microsoft.App/managedEnvironments` | `2024-03-01` | `container-app.bicep` | Consumption | — |
| 6 | `ca-orders-api` | `Microsoft.App/containerApps` | `2024-03-01` | `container-app.bicep` | — | — |
| 7 | `aoai-identity-poc3` | `Microsoft.CognitiveServices/accounts` | `2025-04-01-preview` | `cognitive.bicep` | S0 / AIServices | SystemAssigned |
| 8 | `aiproj-identity-poc` | `Microsoft.CognitiveServices/accounts/projects` | `2025-04-01-preview` | `cognitive.bicep` | — | SystemAssigned |
| 9 | `aoai-connection` | `Microsoft.CognitiveServices/accounts/connections` | `2025-04-01-preview` | `cognitive.bicep` | AzureOpenAI | — |
| 10 | `apim-gateway` | `Microsoft.CognitiveServices/accounts/connections` | `2025-04-01-preview` | `ai-gateway-connection.bicep` | ApiManagement | — |
| 10b | `mcp-entra` | `Microsoft.CognitiveServices/accounts/projects/connections` | `2025-04-01-preview` | `mcp-oauth-connection.bicep` | UserEntraToken / RemoteTool | — |
| 11 | `gpt-4o` | `Microsoft.CognitiveServices/accounts/deployments` | `2024-10-01` | `cognitive.bicep` | Standard (cap 10) | — |
| 12 | `apim-identity-poc` | `Microsoft.ApiManagement/service` | `2024-06-01-preview` | `apim.bicep` | StandardV2 | SystemAssigned |
| 13 | `orders-api-backend` | `Microsoft.ApiManagement/service/backends` | `2024-06-01-preview` | `apim.bicep` | — | — |
| 14 | `openai-backend` | `Microsoft.ApiManagement/service/backends` | `2024-06-01-preview` | `apim.bicep` | — | — |
| 15 | `orders-api` | `Microsoft.ApiManagement/service/apis` | `2024-06-01-preview` | `apim.bicep` | http | — |
| 16 | `azure-openai` | `Microsoft.ApiManagement/service/apis` | `2024-06-01-preview` | `apim.bicep` | http + rawxml policy | — |
| 17 | 6x operations | `Microsoft.ApiManagement/service/apis/operations` | `2024-06-01-preview` | `apim.bicep` | — | — |
| 18 | 3x operations | `Microsoft.ApiManagement/service/apis/operations` | `2024-06-01-preview` | `apim.bicep` | — | — |
| 19 | `orders-mcp` | `Microsoft.ApiManagement/service/apis` | `2025-03-01-preview` | `apim-mcp.bicep` | mcp | — |
| 20 | Role assignment | `Microsoft.Authorization/roleAssignments` | `2022-04-01` | `role-assignment.bicep` | Cognitive Services User | — |
| 21 | `stidentitypoc{unique}` | `Microsoft.Storage/storageAccounts` | `2023-05-01` | `storage.bicep` | Standard_LRS / StorageV2 | — |
| 22 | `kv-identity-poc` | `Microsoft.KeyVault/vaults` | `2023-07-01` | `keyvault.bicep` | standard | — |

---

## 4. Dependency Graph

### Bicep Deployment Tiers

```
Tier 1 (parallel — no dependencies):
┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌─────────────┐
│ storage  │  │ keyvault │  │ cognitive │  │ registry  │  │ monitoring  │
└──────────┘  └──────────┘  └─────┬─────┘  └─────┬─────┘  └──────┬──────┘
                                  │               │               │
Tier 2:                           │        ┌──────┴───────────────┘
                                  │        │
                                  │ ┌──────┴───────┐
                                  │ │ container-app│
                                  │ └──────┬───────┘
                                  │        │
Tier 3:                           │ ┌──────┴──────┐
                                  ├─┤    apim     │
                                  │ └──────┬──────┘
                                  │        │
Tier 3.5:                         │ ┌──────┴──────┐
                                  │ │  apim-mcp   │
                                  │ └──────┬──────┘
                                  │        │
Tier 4:                    ┌──────┴────────┴──────────────────┐
                           │                                   │
                    ┌──────┴──────────┐          ┌─────────────┴─────────┐
                    │ role-assignment │          │ ai-gateway-connection │
                    └─────────────────┘          │ mcp-oauth-connection  │  (deploys mcp-entra)
                                                 └───────────────────────┘
```

### Every Dependency Edge

| From | To | Data Passed | Why |
|------|----|-------------|-----|
| `monitoring` | `containerApp` | `logAnalyticsWorkspaceId` | Container Apps Environment needs Log Analytics for app logging |
| `registry` | `containerApp` | `registryLoginServer`, `registryName` | Container App pulls Docker images from ACR using admin credentials |
| `containerApp` | `apim` | `containerAppFqdn` | APIM orders-api-backend routes traffic to the Container App's FQDN |
| `cognitive` | `apim` | `openaiEndpoint` | APIM openai-backend routes AI Gateway traffic to the cognitive account's OpenAI endpoint |
| `apim` | `role-assignment` | `apimPrincipalId` | Role assignment targets the APIM managed identity's principal ID |
| `cognitive` | `role-assignment` | `cognitiveAccountName` | Role is scoped to the cognitive account resource |
| `apim` | `ai-gateway-connection` | `apimGatewayUrl`, `apimResourceId` | Connection target URL and metadata reference the APIM instance |
| `cognitive` | `ai-gateway-connection` | `cognitiveAccountName` | Connection is a child resource of the cognitive account |

### Post-Provision Dependencies

After Bicep completes, the post-provision hook depends on these outputs (passed as azd environment variables):

| azd Env Var | Source Module | Used By |
|-------------|---------------|---------|
| `APIM_MCP_ENDPOINT` | `apim-mcp.bicep` | MCP endpoint URL for agent |
| `APIM_GATEWAY_URL` | `apim.bicep` | Fallback MCP endpoint construction |
| `AI_FOUNDRY_PROJECT_ENDPOINT` | `cognitive.bicep` | Foundry SDK connection |

---

## 5. Resource Details

### 5.1 AI Services Account (`aoai-identity-poc3`)

```
ARM Type:    Microsoft.CognitiveServices/accounts
API Version: 2025-04-01-preview
Bicep File:  infra/modules/cognitive.bicep
```

| Property | Value | Notes |
|----------|-------|-------|
| `kind` | `AIServices` | NOT `OpenAI` — AIServices supports project management |
| `sku` | `S0` | Standard pricing tier |
| `identity` | `SystemAssigned` | Used internally by the platform |
| `allowProjectManagement` | `true` | Enables child project resources without ML Hub |
| `customSubDomainName` | `aoai-identity-poc3` | Required for AAD auth; defines the endpoint URL |
| `publicNetworkAccess` | `Enabled` | Phase 1 — no network restrictions |
| `disableLocalAuth` | `false` (set) | Managed tenant overrides this to `true` |

**Why `kind: AIServices` (not `OpenAI`)?** The `AIServices` kind with `allowProjectManagement: true` replaces the ML Hub/Project pattern (`Microsoft.MachineLearningServices`). The Hub pattern requires portal "activation" which fails in managed tenants. The CognitiveServices pattern works directly via Bicep with no portal interaction.

**Why suffix `3`?** After `azd down --purge`, Azure's data plane caches the old account state. Recreating with the same name causes "Project not found" errors for hours. Solution: use a new name each time (suffix `2` → `3`).

**Endpoints map:** The account exposes multiple endpoints:
- `endpoint` — base cognitive services endpoint
- `endpoints['OpenAI Language Model Instance API']` — the OpenAI-compatible endpoint used by the AI Gateway

### 5.2 AI Foundry Project (`aiproj-identity-poc`)

```
ARM Type:    Microsoft.CognitiveServices/accounts/projects
API Version: 2025-04-01-preview
Bicep File:  infra/modules/cognitive.bicep
Parent:      aoai-identity-poc3
```

A child resource of the AI Services account. The project provides:
- A scoped workspace for agents, connections, and deployments
- Its own `SystemAssigned` managed identity
- A project endpoint: `https://aoai-identity-poc3.services.ai.azure.com/api/projects/aiproj-identity-poc`

The Foundry SDK (`AIProjectClient`) connects to this project endpoint to manage agents.

### 5.3 Connections

The cognitive account has two connections:

**`aoai-connection` (AzureOpenAI)**

```
Bicep File: infra/modules/cognitive.bicep
Category:   AzureOpenAI
AuthType:   AAD
Target:     {account OpenAI endpoint}
```

A self-referencing connection — the account points to its own OpenAI endpoint. This is required for the Foundry project to discover and use the gpt-4o deployment. `isSharedToAll: true` makes it available to all projects.

**`apim-gateway` (ApiManagement)**

```
Bicep File: infra/modules/ai-gateway-connection.bicep
Category:   ApiManagement
AuthType:   AAD
Target:     {apimGatewayUrl}/openai
```

Links APIM as the AI Gateway for the Foundry project. When this connection exists:
- The Foundry portal shows APIM as the gateway
- AI traffic can be routed through APIM for rate limiting and metrics
- Metadata includes the APIM ARM resource ID for portal integration

### 5.4 gpt-4o Deployment

```
ARM Type:    Microsoft.CognitiveServices/accounts/deployments
API Version: 2024-10-01
Bicep File:  infra/modules/cognitive.bicep
Parent:      aoai-identity-poc3
```

| Property | Value |
|----------|-------|
| Model format | `OpenAI` |
| Model name | `gpt-4o` |
| Model version | `2024-08-06` |
| SKU name | `Standard` |
| SKU capacity | `10` (10K tokens per minute) |

**Why gpt-4o specifically?** It's the only model that supports MCP tool calling in Azure AI Foundry. Other models (gpt-4, gpt-35-turbo) fail when the agent attempts to use MCP tools.

### 5.5 Container Apps

**Container Apps Environment (`cae-identity-poc`)**

```
ARM Type:    Microsoft.App/managedEnvironments
API Version: 2024-03-01
Bicep File:  infra/modules/container-app.bicep
```

Configured with Log Analytics for app logging. Uses the workspace's customer ID and shared key for the log analytics configuration.

**Orders API Container App (`ca-orders-api`)**

```
ARM Type:    Microsoft.App/containerApps
API Version: 2024-03-01
Bicep File:  infra/modules/container-app.bicep
```

| Property | Value |
|----------|-------|
| Ingress | External, port 8000, HTTPS |
| Revisions | Single active revision mode |
| Resources | 0.25 vCPU, 0.5 Gi memory |
| Scale | 1–3 replicas |
| Registry | ACR (admin credentials via secret) |
| Initial image | `containerapps-helloworld` (placeholder) |

The Container App starts with a Microsoft placeholder image. After `azd deploy`, the image is replaced with the Orders API built from `src/orders-api/`. The `azd-service-name: orders-api` tag tells azd which Container App maps to which service.

### 5.6 API Management (`apim-identity-poc`)

```
ARM Type:    Microsoft.ApiManagement/service
API Version: 2024-06-01-preview
Bicep File:  infra/modules/apim.bicep
```

| Property | Value |
|----------|-------|
| SKU | `StandardV2`, capacity 1 |
| Identity | `SystemAssigned` |
| Publisher | `Identity PoC` / `admin@identity-poc.dev` |

APIM hosts three APIs:

**1. Orders REST API (`orders-api`)**
- Path: `/orders-api`
- Backend: `orders-api-backend` → `https://{containerAppFqdn}`
- 6 operations: `list-orders` (GET /orders), `get-order` (GET /orders/{order_id}), `create-order` (POST /orders), `update-order` (PUT /orders/{order_id}), `delete-order` (DELETE /orders/{order_id}), `health-check` (GET /health)
- No subscription required, no special policy

**2. Azure OpenAI API (`azure-openai`) — AI Gateway**
- Path: `/openai`
- Backend: `openai-backend` → `{cognitiveEndpoint}openai`
- 3 operations: `chat-completions`, `completions`, `embeddings`
- Policy: MI auth, token rate limit, token metrics, backend routing
- No subscription required

**3. Orders MCP Server (`orders-mcp`) — Bicep-deployed**
- Path: `/orders-mcp`
- Type: `mcp` (not standard `http`)
- 6 mcpTools mapping tool names to REST API operation ARM resource IDs
- MCP endpoint: `{gatewayUrl}/orders-mcp/mcp`
- Deployed by `apim-mcp.bicep` using `@2025-03-01-preview` (BCP037 warnings expected)

### 5.7 AI Gateway Policy

**File:** `infra/policies/ai-gateway-policy.xml`

The policy has 4 elements applied in the `<inbound>` section:

```xml
<policies>
  <inbound>
    <base />
    <!-- 1. Managed Identity Authentication -->
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <!-- 2. Token Rate Limiting -->
    <azure-openai-token-limit counter-key="@(context.Request.IpAddress)"
      tokens-per-minute="10000" estimate-prompt-tokens="true"
      remaining-tokens-header-name="x-ratelimit-remaining-tokens" />
    <!-- 3. Token Usage Metrics -->
    <llm-emit-token-metric>
      <dimension name="API ID" />
      <dimension name="Operation ID" />
      <dimension name="Client IP" value="@(context.Request.IpAddress)" />
      <dimension name="Deployment" value="@(context.Request.MatchedParameters[&quot;deployment-id&quot;])" />
    </llm-emit-token-metric>
    <!-- 4. Backend Routing -->
    <set-backend-service backend-id="openai-backend" />
  </inbound>
</policies>
```

| # | Element | Purpose |
|---|---------|---------|
| 1 | `authentication-managed-identity` | Acquires a token for `https://cognitiveservices.azure.com` using APIM's system-assigned MI. Injects it as `Authorization: Bearer` header. |
| 2 | `azure-openai-token-limit` | Rate limits by client IP at 10K tokens/minute. Estimates prompt tokens before sending to backend. Returns remaining tokens in response header. |
| 3 | `llm-emit-token-metric` | Emits token usage as custom metrics to App Insights. Dimensions allow filtering by API, operation, client IP, and model deployment. |
| 4 | `set-backend-service` | Routes the request to the `openai-backend` (which points to the cognitive account's OpenAI endpoint). |

**Why `rawxml` format?** The C# expressions use double quotes inside XML attributes (e.g., `@(context.Request.MatchedParameters["deployment-id"])`). The `rawxml` format handles this; the standard `xml` format would require double-escaping.

### 5.8 Role Assignment

```
ARM Type:    Microsoft.Authorization/roleAssignments
API Version: 2022-04-01
Bicep File:  infra/modules/role-assignment.bicep
Scope:       aoai-identity-poc3 (cognitive account)
```

| Property | Value |
|----------|-------|
| Role | `Cognitive Services User` |
| Role GUID | `a97b65f3-24c7-4388-baec-2e87135dc908` |
| Principal | APIM managed identity (ServicePrincipal) |
| Name | `guid(cognitiveAccount.id, principalId, roleDefinitionId)` |

The `guid()` function generates a deterministic name from the three inputs, ensuring the deployment is idempotent — redeploying won't fail on "role assignment already exists" errors.

**Why it exists:** Without this role, APIM's `authentication-managed-identity` policy would fail with 403 when trying to call Azure OpenAI. The `Cognitive Services User` role grants read access to the cognitive account's data plane.

### 5.9 Monitoring

**Log Analytics Workspace (`log-identity-poc`)**

```
ARM Type:    Microsoft.OperationalInsights/workspaces
API Version: 2023-09-01
Bicep File:  infra/modules/monitoring.bicep
```

- SKU: `PerGB2018` (pay-per-GB ingestion)
- Retention: 30 days
- Receives: Container App logs, APIM diagnostics (future), token metrics from AI Gateway

**Application Insights (`appi-identity-poc`)**

```
ARM Type:    Microsoft.Insights/components
API Version: 2020-02-02
Bicep File:  infra/modules/monitoring.bicep
```

- Kind: `web`
- Linked to Log Analytics workspace via `WorkspaceResourceId`
- Receives: `llm-emit-token-metric` data from the AI Gateway policy (token counts per API/operation/deployment)

---

## 6. Data Flows

### Flow 1: Direct API Call

```
Client                    Container App (ca-orders-api)
  │                                │
  │  GET https://{fqdn}/orders     │
  ├───────────────────────────────>│
  │                                │  FastAPI processes request
  │                                │  Returns in-memory order data
  │  200 OK + JSON array           │
  │<───────────────────────────────┤
```

**Resources touched:** `ca-orders-api` only

**Auth:** None (Phase 1). Ingress is external and open.

**Data:** 8 seed orders (ORD-001 through ORD-008) returned as JSON array.

### Flow 2: MCP Tool Call (Agent → APIM → API)

```
User/Test         Foundry Agent        APIM              Container App
  │               (orders-agent)    (apim-identity-poc)  (ca-orders-api)
  │                    │                   │                    │
  │ "List all orders"  │                   │                    │
  ├───────────────────>│                   │                    │
  │                    │                   │                    │
  │                    │  gpt-4o decides   │                    │
  │                    │  to call MCP tool │                    │
  │                    │  "list-orders"    │                    │
  │                    │                   │                    │
  │                    │  POST /orders-mcp/mcp                  │
  │                    ├──────────────────>│                    │
  │                    │                   │                    │
  │                    │                   │  MCP resolves tool │
  │                    │                   │  "list-orders" to  │
  │                    │                   │  GET /orders on    │
  │                    │                   │  orders-api        │
  │                    │                   │                    │
  │                    │                   │  GET /orders       │
  │                    │                   ├───────────────────>│
  │                    │                   │                    │
  │                    │                   │  200 OK + JSON     │
  │                    │                   │<───────────────────┤
  │                    │                   │                    │
  │                    │  MCP response     │                    │
  │                    │  (tool result)    │                    │
  │                    │<──────────────────┤                    │
  │                    │                   │                    │
  │                    │  gpt-4o formats   │                    │
  │                    │  response         │                    │
  │                    │                   │                    │
  │  Natural language  │                   │                    │
  │  response with     │                   │                    │
  │  order data        │                   │                    │
  │<───────────────────┤                   │                    │
```

**Resources touched:** Foundry Agent (control plane) → `apim-identity-poc` (MCP API `orders-mcp`) → `ca-orders-api`

**Auth:** None (Phase 1). The Foundry agent calls APIM's MCP endpoint directly. APIM forwards to the Container App without auth.

**Key detail:** The MCP API's `mcpTools` array maps tool names to REST API operation ARM resource IDs. When the agent calls `list-orders`, APIM resolves it to the `list-orders` operation on the `orders-api` API, which maps to `GET /orders` on the orders-api-backend.

### Flow 3: AI Gateway Call (Client → APIM → Azure OpenAI)

```
Client                 APIM                     Azure OpenAI
                    (apim-identity-poc)       (aoai-identity-poc3)
  │                       │                         │
  │  POST /openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21
  ├──────────────────────>│                         │
  │                       │                         │
  │                       │  1. MI auth: acquire    │
  │                       │     Bearer token for    │
  │                       │     cognitiveservices    │
  │                       │                         │
  │                       │  2. Token rate limit:   │
  │                       │     check 10K TPM       │
  │                       │     per client IP       │
  │                       │                         │
  │                       │  3. Emit token metrics  │
  │                       │     to App Insights     │
  │                       │                         │
  │                       │  4. Route to            │
  │                       │     openai-backend      │
  │                       │                         │
  │                       │  POST {cognitiveEndpoint}openai/deployments/gpt-4o/...
  │                       ├────────────────────────>│
  │                       │                         │
  │                       │  200 OK + completion    │
  │                       │<────────────────────────┤
  │                       │                         │
  │  200 OK + completion  │                         │
  │  + x-ratelimit-remaining-tokens header          │
  │<──────────────────────┤                         │
```

**Resources touched:** `apim-identity-poc` (Azure OpenAI API + policy) → `aoai-identity-poc3` (gpt-4o deployment)

**Auth:** APIM authenticates to Azure OpenAI using its system-assigned managed identity. The `authentication-managed-identity` policy acquires a token for `https://cognitiveservices.azure.com` and injects it as a Bearer token. This works because the role assignment (Step 10) grants APIM's MI the `Cognitive Services User` role on the account.

**Rate limiting:** The `azure-openai-token-limit` policy tracks token usage per client IP. At 10K TPM, it returns 429 if the limit is exceeded. The `x-ratelimit-remaining-tokens` response header tells the client how many tokens remain.

**Metrics:** The `llm-emit-token-metric` policy emits custom metrics to App Insights with dimensions for API ID, Operation ID, Client IP, and Deployment name — enabling per-model, per-client usage dashboards.

### Flow 4: MCP Tool Call with UserEntraToken (identity propagation)

```
User/Test         Foundry Agent        APIM              Container App
  │               (orders-assistant)  (apim-identity-poc)  (ca-orders-api)
  │                    │                   │                    │
  │ "List all orders"  │                   │                    │
  ├───────────────────>│                   │                    │
  │                    │                   │                    │
  │  mcp_approval_     │                   │                    │
  │  request           │                   │                    │
  │<───────────────────┤                   │                    │
  │                    │                   │                    │
  │  Approve tool call │                   │                    │
  ├───────────────────>│                   │                    │
  │                    │                   │                    │
  │                    │  POST /orders-mcp/mcp                  │
  │                    │  Authorization: Bearer {user JWT}       │
  │                    ├──────────────────>│                    │
  │                    │                   │                    │
  │                    │                   │ validate-jwt:      │
  │                    │                   │ aud, iss, exp      │
  │                    │                   │                    │
  │                    │                   │  GET /orders       │
  │                    │                   ├───────────────────>│
  │                    │                   │  200 OK            │
  │                    │                   │<───────────────────┤
  │                    │                   │                    │
  │                    │  MCP response     │                    │
  │                    │<──────────────────┤                    │
  │                    │                   │                    │
  │  Response with     │                   │                    │
  │  order data        │                   │                    │
  │<───────────────────┤                   │                    │
```

**Resources touched:** Foundry Agent → `apim-identity-poc` (MCP API + `validate-jwt`) → `ca-orders-api`

**Auth:** UserEntraToken passthrough — the user's existing Entra token (`aud=https://ai.azure.com`) is passed directly to APIM via the `mcp-entra` connection:
1. Turn 1: Agent returns `mcp_approval_request` — user approves the tool call
2. Turn 2: Agent sends the user's Bearer token to APIM via the UserEntraToken connection
3. APIM validates the JWT: `aud` = `https://ai.azure.com`, issuer = v1 or v2 format
4. App Insights logs `auth.type: bearer-token` with user identity claims

**No OAuth consent flow needed.** The UserEntraToken connection passes the user's existing token directly — no ApiHub involvement, no consent prompts, no refresh token expiry.

**JWT claims (verified in App Insights):**
- `aud`: `https://ai.azure.com`
- `upn`/`oid`: user's identity propagated from browser sign-in

**Connection setup:**
1. Bicep creates the `mcp-entra` UserEntraToken connection on the Foundry project (`mcp-oauth-connection.bicep`)
2. The agent's MCP tool references the connection via `project_connection_id`
3. No postprovision hook steps needed for the connection

---

## 7. What Runs Outside ARM

Two components in this project don't have ARM resource types:

### 7.1 Foundry Agent (`orders-assistant`)

Created by `hooks/postprovision.py` via the `azure-ai-projects` SDK.

**Why not Bicep/ARM?** Foundry agents live in the AI Foundry control plane, not in ARM. There's no ARM resource type for agents — they're managed exclusively through the Foundry SDK or REST API.

**What it creates:** An agent with:
- Model: `gpt-4o` (required for MCP support)
- Name: `orders-assistant`
- Instructions: "You are an orders management assistant..."
- MCP tool: `server_label: "orders_mcp"`, `server_url: "{mcp_endpoint}"`, 6 allowed tools, `project_connection_id: "mcp-entra"`

### 7.2 Chat App Entra Registration (postprovision hook)

Created by `hooks/postprovision.py` via `az ad app create`.

**Why not Bicep/ARM?** The `Microsoft.Graph` Bicep extension requires `Application.ReadWrite.All` on the ARM deployment identity, which is unavailable in managed tenants. The hook uses `az` CLI with the user's delegated permissions instead.

**What it creates (idempotent — checks by displayName before creating):**
- **Chat App SPA** — SPA app with redirect URIs for localhost and deployed FQDN, `requiredResourceAccess` for Azure AI Services (`user_impersonation`)
- **Service principal** for the app

### 7.3 azd Environment Variables

`main.bicep` outputs values that become azd environment variables:

| Variable | Value | Consumed By |
|----------|-------|-------------|
| `AZURE_CONTAINER_REGISTRY_NAME` | ACR name | azd deploy |
| `AZURE_CONTAINER_REGISTRY_ENDPOINT` | ACR login server | azd deploy |
| `ORDERS_API_URL` | `https://{containerAppFqdn}` | verify_deployment.py |
| `ORDERS_API_CONTAINER_APP_NAME` | `ca-orders-api` | azd deploy |
| `APIM_GATEWAY_URL` | APIM gateway URL | postprovision.py, verify_deployment.py |
| `APIM_NAME` | `apim-identity-poc` | verify_deployment.py |
| `APIM_ORDERS_API_PATH` | `orders-api` | verify_deployment.py |
| `APIM_MCP_ENDPOINT` | MCP server endpoint URL | postprovision.py |
| `AI_FOUNDRY_PROJECT_NAME` | `aiproj-identity-poc` | verify_deployment.py |
| `AI_FOUNDRY_PROJECT_ENDPOINT` | Project SDK endpoint | postprovision.py, verify_deployment.py |
| `APIM_OPENAI_ENDPOINT` | `{apimGatewayUrl}/openai` | Consumers of AI Gateway |
| `COGNITIVE_ACCOUNT_NAME` | `aoai-identity-poc3` | verify_deployment.py |
| `AZURE_RESOURCE_GROUP` | `rg-identity-poc` | postprovision.py, verify_deployment.py |
| `MCP_CONNECTION_NAME` | `mcp-entra` | postprovision.py |
| `CHAT_APP_ENTRA_CLIENT_ID` | Chat App SPA app ID | postprovision.py (azd env set) |

---

## 8. Deployment Automation

### How `azd up` Orchestrates Everything

`azd up` = `azd provision` + `azd deploy` + hooks. Here's the full sequence:

```
azd up
 │
 ├── 1. azd provision
 │    │
 │    ├── Bicep deployment (main.bicep at subscription scope)
 │    │    │
 │    │    ├── Tier 1 (parallel): monitoring, registry, cognitive, storage, keyvault
 │    │    ├── Tier 2: container-app (needs registry + monitoring)
 │    │    ├── Tier 3: apim (needs containerApp + cognitive)
 │    │    ├── Tier 3.5: apim-mcp (needs apim operations)
 │    │    └── Tier 4: role-assignment + ai-gateway-connection + mcp-oauth-connection (deploys mcp-entra)
 │    │
 │    ├── Outputs → azd env vars
 │    │
 │    └── postprovision hook (hooks/postprovision.sh)
 │         │
 │         ├── Install Python deps (azure-ai-projects, azure-ai-agents, azure-identity)
 │         └── Run hooks/postprovision.py
 │              ├── Step 1: Create Chat App Entra registration (az CLI)
 │              ├── Step 2: Create Foundry agent (azure-ai-projects SDK)
 │              └── Step 3: Update Chat App container env vars
 │
 ├── 2. azd deploy
 │    │
 │    ├── Build Docker image in ACR (az acr build --no-logs)
 │    │    └── src/orders-api/ → acridentitypoc{unique}.azurecr.io/orders-api:latest
 │    │
 │    └── Update Container App (ca-orders-api)
 │         └── Replace placeholder image with orders-api:latest
 │
 └── 3. Manual: python scripts/test-agent.py
      │
      ├── Turn 1: Agent returns mcp_approval_request for tool call
      ├── User approves tool call
      └── Turn 2: Agent sends Bearer token (UserEntraToken) → APIM validates → order data returned
```

### Verification

After deployment, run the verification script:

```bash
python scripts/verify_deployment.py
```

36 checks across 4 layers:

**Layer 1: Infrastructure (18 checks)**
- Resource group exists and succeeded
- AI Services account (kind=AIServices, allowProjectManagement=true)
- AI Foundry project (provisioningState=Succeeded)
- AzureOpenAI connection (category=AzureOpenAI, authType=AAD)
- gpt-4o deployment (model=gpt-4o, state=Succeeded)
- Container App (external ingress, provisioningState=Succeeded)
- APIM instance (sku=StandardV2)
- APIM managed identity (type=SystemAssigned)
- APIM REST API (orders-api, path=orders-api)
- APIM operations (6/6 present)
- APIM OpenAI API (azure-openai, path=openai)
- APIM OpenAI operations (3/3 present)
- Cognitive Services User role assignment
- AI Gateway connection (apim-gateway, category=ApiManagement)
- Key Vault exists
- Log Analytics workspace (provisioningState=Succeeded)
- Storage account exists
- Container Registry exists

**Layer 2: Post-Provision (2 checks)**
- APIM MCP API (orders-mcp, type=mcp, 6 tools)
- Foundry Agent (orders-agent, model=gpt-4o, has MCP tool)

**Layer 3: OAuth + Security (12 checks)**
- MCP OAuth connection (category=RemoteTool, target=MCP endpoint)
- Entra apps exist (MCP Gateway Audience, Foundry OAuth Client)
- APIM Named Values (McpTenantId, McpAudienceAppId, APIMGatewayURL)
- MCP endpoint returns 401 without token (validate-azure-ad-token working)
- 401 response includes WWW-Authenticate header
- PRM endpoint returns valid RFC 9728 metadata
- PRM resource matches MCP endpoint

**Layer 4: Functional (4 checks)**
- Direct API call (GET /orders → 200, 8+ orders, ORD-001 present)
- APIM proxy (GET /orders-api/orders → 200, 8+ orders, ORD-001 present)
- AI Gateway proxy (POST /openai/... → 401/403, proves route exists)
- Agent MCP round-trip (agent processes prompt, response contains seed data markers or oauth_consent_request)

### Deployment Caveats

| Issue | Workaround |
|-------|-----------|
| CognitiveServices purge/recreate caching | `azd env set COGNITIVE_ACCOUNT_SUFFIX 4` (or next suffix) after `azd down --purge` |
| Managed tenant blocks local auth | Connections must use `authType: 'AAD'` |
| Windows charmap errors in ACR build | Use `az acr build --no-logs` |
| MSYS path conversion in Git Bash | Set `MSYS_NO_PATHCONV=1` before `az` commands with ARM resource IDs |
| APIM policies with C# expressions | Use `rawxml` format (not `xml`) to handle nested double quotes |
| **APIM App Insights breaks MCP SSE** | Response body logging at All APIs scope causes MCP SSE to hang indefinitely. Set frontend/backend response body bytes to `0` in global `applicationinsights` diagnostic. [MS docs](https://learn.microsoft.com/en-us/azure/api-management/export-rest-mcp-server) |
| ACR/Storage naming | No hyphens allowed; use `uniqueString()` suffix for global uniqueness |
| Identifier URI format | Managed tenant requires `api://{appId}` — not custom names |
