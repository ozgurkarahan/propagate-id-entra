# Project Reference — Identity Propagation PoC

This document contains project-specific technical details for the Identity Propagation PoC. Referenced from [`CLAUDE.md`](../CLAUDE.md).

## IaC Principle: Bicep First

Always prioritize Bicep for Azure resource creation. The post-provision hook (`hooks/postprovision.py`) is only for:
- **Foundry Agent** — no ARM resource type; SDK only
- **Entra App Registrations** — Graph Bicep extension requires `Application.ReadWrite.All` on the ARM deployment identity, which is unavailable in managed tenants. Created via `az` CLI (delegated permissions) in the hook instead.

The APIM MCP Server is deployed via Bicep using `@2025-03-01-preview`. BCP037 warnings are expected and safe to ignore.

## Development Notes

### Environment

- **Platform:** Windows 11 + Git Bash
- **Python:** Use `python` not `python3` (Windows)
- **MSYS path fix:** `export MSYS_NO_PATHCONV=1` before `az` commands with resource ID paths
- **ACR builds:** `az acr build --no-logs` avoids charmap encoding errors on Windows

### Foundry SDK (`azure-ai-projects` 2.0.0b3 — Responses API)

- **Upgraded from v1 to v2 beta** — v1 classic API did NOT support `project_connection_id` on MCP tools
- `AIProjectClient` from `azure-ai-projects` — connects to the project endpoint
- Agent creation: `project_client.agents.create_version()` with `PromptAgentDefinition` + `MCPTool`
- Agent execution: `project_client.get_openai_client()` → `openai_client.responses.create()` (Responses API)
- `MCPTool` class: `server_label`, `server_url`, `require_approval`, `allowed_tools`, `project_connection_id`
- `project_connection_id` — references the Foundry project connection name (e.g., `mcp-oauth`)
- `server_label` must match `^[a-zA-Z0-9_]+$` — no hyphens
- `gpt-4o` required — other models do NOT support MCP tools
- Responses API uses `extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}}` to bind agent (the `"agent"` key is deprecated — returns 400 `invalid_payload`)
- `response.output_text` returns the assistant's text response

### APIM MCP Server

- Deployed via Bicep (`infra/modules/apim-mcp.bicep`) using `@2025-03-01-preview`
- Requires both `apiType: 'mcp'` and `type: 'mcp'` in properties
- `mcpTools` array maps tool names to operation ARM resource IDs (via `existing` resource references)
- BCP037 warnings for `apiType`, `type`, `mcpTools` — expected and safe to ignore
- MCP endpoint: `{gateway_url}/{api_path}/mcp`

### Entra App Registrations (Postprovision Hook)

- **Created by postprovision hook** (`hooks/postprovision.py`) using `az` CLI with delegated permissions
- Graph Bicep extension (`infra/modules/entra-apps.bicep`) exists but is unused — ARM deployment identity lacks `Application.ReadWrite.All` in managed tenants
- Hook creates: MCP Gateway Audience app (with `access_as_user` scope), Foundry OAuth Client app (with redirect URI + API permission), service principals, admin consent grant
- Hook also sets: `identifierUris` (`api://{appId}`), client secret, updates OAuth connection with real credentials
- Idempotent — checks by `displayName` before creating; PATCH operations are safe to re-run
- `MCP_AUDIENCE_APP_ID` and `MCP_OAUTH_CLIENT_ID` are set by hook via `azd env set` (not Bicep outputs)

### MCP OAuth Connection

- **Always deployed** — Bicep creates the connection with placeholder values; hook patches with real credentials
- OAuth2 connection on Foundry project: `mcp-oauth-connection.bicep` using `@2025-04-01-preview`
- Connection category: `RemoteTool` with `group: 'GenericProtocol'` and `metadata.type: 'custom_MCP'`
- **IMPORTANT:** `CustomKeys` with `authType: OAuth2` is NOT recognized by Agent Service for OAuth — must use `RemoteTool`
- **IMPORTANT:** Bicep-created RemoteTool connections do NOT register the ApiHub connector — postprovision hook must DELETE the Bicep connection and PUT a fresh one via ARM REST to trigger ApiHub setup
- BCP037 warnings for `group`, `connectorName`, `metadata.type`, `credentials`, `authorizationUrl`, `tokenUrl`, `refreshUrl`, `scopes` — expected and safe to ignore
- Bicep deploys with placeholder secret (`newGuid()`); postprovision hook updates with real secret
- Agent MCP tool: `MCPTool(project_connection_id=...)` references the OAuth connection
- **Consent:** Foundry uses ApiHub (`consent.azure-apihub.net`) for interactive OAuth — multi-turn flow with `oauth_consent_request` → user authenticates → `previous_response_id` to continue
- **Fallback:** `scripts/grant-mcp-consent.py` — device code flow → stores refresh token on connection via ARM REST PUT
- **Re-deployment note:** each `azd up` creates a new client secret — re-run `test-agent-oauth.py` after each deploy

### MCP Auth (APIM Token Validation)

- APIM `validate-azure-ad-token` policy on MCP API (`infra/policies/mcp-api-policy.xml`)
- Validates JWT: `aud` = `api://{audienceAppId}`, issuer = `https://sts.windows.net/{tenantId}/`
- Returns 401 with `WWW-Authenticate` challenge on invalid/missing token
- RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource` (`infra/policies/mcp-prm-policy.xml`)
- 3 APIM Named Values: `McpTenantId`, `McpAudienceAppId` (placeholder → hook patches), `APIMGatewayURL`
- App Insights confirms: `auth.type: bearer-token` with full JWT, user identity propagated

### Scripts

- `scripts/verify_deployment.py` — 37-check deployment verification (ARM API + OAuth + Named Values + MCP 401 + PRM + sign-in audit + agent round-trip)
- `scripts/diagnose-mcp-auth.py` — 8-step MCP OAuth diagnostic (24/24 checks)
- `scripts/test-agent-oauth.py` — Interactive multi-turn agent test (OAuth consent + MCP approval)
- `scripts/grant-mcp-consent.py` — Fallback device code flow for OAuth consent
- `scripts/check-signin-logs.py` — Entra ID sign-in log viewer (Graph API `auditLogs/signIns` for all 3 app registrations)

### APIM Diagnostics (MCP Compatibility)

- **CRITICAL:** Application Insights response body logging at the All APIs scope **breaks MCP SSE streaming**. The response buffering interferes with the SSE transport, causing the MCP endpoint to hang indefinitely.
- Frontend and backend response body bytes MUST be `0` in the global `applicationinsights` diagnostic (`infra/modules/apim.bicep`)
- See: https://learn.microsoft.com/en-us/azure/api-management/export-rest-mcp-server
- Request body logging (8192 bytes) is fine — only response body logging causes issues
- Also: do NOT access `context.Response.Body` in MCP API policies — triggers response buffering that breaks SSE (per MS docs)

### Chat App (Phase 3)

- `src/chat-app/` — FastAPI backend + vanilla JS SPA with MSAL.js
- MSAL.js loaded from CDN: `https://alcdn.msauth.net/browser/2.38.2/js/msal-browser.min.js`
- **IMPORTANT:** Not all NPM versions exist on the CDN — always verify with `curl -sI` before using
- `/api/config` returns MSAL config + App Insights connection string from env vars
- `UserTokenCredential` wraps the user's MSAL token as a `TokenCredential` for the Foundry SDK
- **Token audience:** Foundry endpoint (`*.services.ai.azure.com`) requires `aud=https://ai.azure.com` — resource app ID `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`, scope `user_impersonation` (`1a7925b5-f871-417a-9b8b-303f9f29fa10`)
- **Do NOT call `agents.list()`** in the chat hot path — `UserTokenCredential` serves a single audience, and the agent name is known from the `AGENT_NAME` env var
- **Access token**: sent in `Authorization: Bearer` header (not POST body)
- **Timeout**: `responses.create()` calls wrapped in `asyncio.wait_for(..., timeout=120)` — returns 504 on timeout
- SPA Entra app needs `requiredResourceAccess` for `https://ai.azure.com` — without it, AADSTS650057
- Postprovision hook creates Chat App Entra registration (Step 1b) + updates container env vars (Step 4)
- **Re-authenticate flow:** When MCP tokens expire, frontend detects auth errors (401/`tool_user_error`) and shows a red "Re-authenticate" banner. Clicking it calls `POST /api/reset-mcp-auth` which DELETE+PUTs the MCP connection via ARM REST (managed identity with `Cognitive Services Contributor` role), wiping ApiHub's stale token state. The retry triggers `oauth_consent_request` → existing consent banner handles the rest.
- **Connection config env vars:** Postprovision hook sets `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `COGNITIVE_ACCOUNT_NAME`, `AI_FOUNDRY_PROJECT_NAME`, `APIM_GATEWAY_URL`, `MCP_OAUTH_CLIENT_ID`, `MCP_OAUTH_CLIENT_SECRET` on the chat app container — needed by `/api/reset-mcp-auth`

### Observability

- **All Python apps** (Chat App, Orders API): `azure-monitor-opentelemetry` auto-instruments HTTP requests, exceptions, and logging → App Insights
- **Frontend**: App Insights JS SDK (`ai.3.gbl.min.js`) tracks page views, events (sign-in, chat responses), and exceptions
- **APIM MCP policy**: forwards `X-Request-ID` (from `context.RequestId`) to Orders API backend
- **Correlation IDs**: `session_id` (browser → chat-app), `request_id` (chat-app → response), `X-Request-ID` (APIM → Orders API), `Mcp-Session-Id` (MCP → Orders API)
- **Foundry agent telemetry**: `responsesapi` cloud role emits AI dependency records (LLM calls + MCP tool executions) to App Insights automatically — connected via Foundry portal Tracing page (NOT via Bicep — `appInsightsResourceId` does not exist in the CognitiveServices ARM schema)
- **Foundry SDK is opaque**: no custom headers/trace context possible — correlate via timestamps + user identity across the boundary
- **Workbook**: `infra/workbooks/identity-propagation.json` — 8 tabs (traces, tokens, auth failures, MCP patterns, E2E flow, container logs, errors, OAuth audit)
- **Sign-in audit logs**: Entra ID `auditLogs/signIns` (Graph API) shows token issuance events for all 3 app registrations — fills the Foundry/ApiHub OAuth visibility gap
- **Entra diagnostic settings constraint**: Routing sign-in logs to Log Analytics requires Security Admin role (blocked in managed tenants). Use `check-signin-logs.py` as immediate alternative.
- **`APPLICATIONINSIGHTS_CONNECTION_STRING`**: injected via Bicep env var on all Container Apps (Chat App, Orders API)

### APIM MCP Write Operations (Known Issue)

- APIM REST operations for POST/PUT lack `representations` (request body schema)
- MCP server auto-generates tool schemas from operations — without body schema, the agent guesses field names
- Results in 422 Unprocessable Entity (e.g., agent sends `customerName` but API expects `customer_name`)
- **Fix:** Add `representations` with JSON schema to `create-order` and `update-order` operations in `apim.bicep`

### Deployment Caveats

- After `azd down --purge`, do NOT recreate CognitiveServices with the same name — data plane caching causes "Project not found" errors. Use `azd env set COGNITIVE_ACCOUNT_SUFFIX 2` (or next available suffix)
- Managed tenant forces `disableLocalAuth: true` even if Bicep sets `false`
- `Cognitive Services User` role assignment needed on the account for AAD auth
- Identifier URI format: managed tenant requires `api://{appId}` — not custom names
- Entra app registration changes can take 1-5 minutes to propagate — new `requiredResourceAccess` may not be immediately available
- **CRITICAL — azd parameter mapping:** `main.parameters.json` must explicitly map azd env vars to Bicep parameters using `"${VAR_NAME}"` syntax. azd does NOT auto-map env vars to Bicep params. Missing mappings cause Bicep params to use their empty-string defaults silently, which can break containers at runtime)
- **CRITICAL — `az containerapp update` + Bicep placeholder images:** Container Apps deployed with a Bicep placeholder image (e.g., `containerapps-helloworld:latest`) then updated by `azd deploy` with the real image — if you later run `az containerapp update --set-env-vars`, the new revision inherits the Bicep placeholder image, NOT the azd-deployed image. Always include `--image <real-image>` when using `az containerapp update`.
