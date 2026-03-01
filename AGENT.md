# AGENT.md — propagate-id-entra

## Overview

**propagate-id-entra** demonstrates end-to-end identity propagation from a browser through an AI Foundry agent to a backend Orders API using pure Entra ID OAuth. A single `azd up` provisions all Azure resources.

> For comprehensive details: [docs/deep-dive.md](docs/deep-dive.md) (ARM resources, data flows) | [docs/identity-security.md](docs/identity-security.md) (identity & security architecture) | [docs/project-reference.md](docs/project-reference.md) (technical reference)

## Architecture

```
Browser (MSAL.js)
        │  Sign in → Entra ID → access token (aud=ai.azure.com)
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
         │ Orders MCP (Bearer token)
         ▼
┌──────────────────────────────────────────────────┐
│  API Management (APIM)                            │
│  ├─ Orders MCP API  (/orders-mcp)  validate-azure-ad-token
│  ├─ Orders REST API (/orders-api)                 │
│  └─ Azure OpenAI API (/openai)     MI auth + token rate limit
└────────┬─────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  Orders API      │
│  (Container App) │
│  FastAPI · 6 CRUD│
└──────────────────┘
```

**Core principle:** No service accounts in the data path. The user's identity propagates end-to-end from browser through AI agent to backend API.

## Development Quick Reference

### Setup

```bash
azd env new propagate-id-entra
azd up
python scripts/verify_deployment.py
python scripts/grant-mcp-consent.py
python scripts/test-agent-oauth.py
```

### Key Paths

- `src/orders-api/` — FastAPI Orders CRUD backend (6 endpoints, 8 seed orders)
- `src/chat-app/` — FastAPI backend + vanilla JS SPA with MSAL.js
- `infra/main.bicep` — Subscription-scoped Bicep orchestrator
- `infra/modules/` — Bicep modules (14 modules)
- `infra/policies/` — APIM policies (ai-gateway, mcp-api, mcp-prm)
- `hooks/postprovision.py` — Entra app registrations + Foundry agent creation
- `scripts/` — Deployment verification, diagnostics, OAuth consent, sign-in logs

### Key Commands

```bash
# Deploy
azd up

# Verify deployment
python scripts/verify_deployment.py

# Test OAuth flow interactively
python scripts/test-agent-oauth.py

# Grant OAuth consent (headless/device code)
python scripts/grant-mcp-consent.py

# Diagnose MCP auth issues
python scripts/diagnose-mcp-auth.py

# View Entra sign-in logs
python scripts/check-signin-logs.py
```

## Identity & Auth Flows

### 1. User → Chat App → Foundry (Delegated Identity)

Browser signs in via MSAL.js (`aud=https://ai.azure.com`). Chat App wraps the user's token in a `UserTokenCredential` and calls the Foundry Responses API. The user's identity is preserved for downstream MCP tool calls.

### 2. Foundry → APIM MCP (OAuth Delegated)

Foundry Agent uses the `mcp-oauth` RemoteTool connection to acquire a delegated token (`aud=api://{audienceAppId}`, `scp=access_as_user`). APIM validates with `validate-azure-ad-token`. On 401, returns RFC 9728 Protected Resource Metadata.

### 3. APIM → Azure OpenAI (Managed Identity)

APIM AI Gateway uses system-assigned MI with `Cognitive Services User` role. No user tokens involved — pure service-to-service auth.

### Token Audiences

| Token | Audience | Auth Type | User Identity? |
|-------|----------|-----------|----------------|
| Browser → Foundry | `https://ai.azure.com` | Delegated (MSAL.js) | Yes |
| Foundry → APIM MCP | `api://{audienceAppId}` | Delegated (OAuth) | Yes |
| APIM → Azure OpenAI | `https://cognitiveservices.azure.com` | Managed Identity | No |

### Entra App Registrations

All created by postprovision hook (`az` CLI, delegated permissions):

| App | Purpose | Key Config |
|-----|---------|------------|
| **MCP Gateway Audience** | API that MCP tokens authenticate against | `identifierUris: api://{appId}`, scope `access_as_user` |
| **Foundry OAuth Client** | OAuth client for Foundry token acquisition | `isFallbackPublicClient: true`, redirect URIs for ApiHub consent |
| **Chat App SPA** | Browser MSAL.js authentication | SPA redirect URIs, `requiredResourceAccess` for `https://ai.azure.com` |

## IaC Principle

**Bicep first.** The postprovision hook only handles:
- **Entra App Registrations** — ARM identity lacks `Application.ReadWrite.All` in managed tenants
- **Foundry Agent** — No ARM resource type; SDK only

## Bicep Deployment Tiers

| Tier | Modules | Depends On |
|------|---------|------------|
| 1 | cognitive, registry, monitoring, storage, keyvault | — |
| 1.5 | workbook | monitoring |
| 2 | container-app | registry, monitoring |
| 2.5 | chat-app | registry, container-app, cognitive |
| 3 | apim | container-app, cognitive |
| 3.5 | apim-mcp | apim |
| 4 | role-assignment, ai-gateway-connection, mcp-oauth-connection | cognitive, apim-mcp, chat-app |

## Postprovision Hook Steps

1. **Step 1:** Create Entra apps (MCP Gateway Audience + Foundry OAuth Client) + configure OAuth connection
2. **Step 1b:** Create Chat App Entra registration
3. **Step 2:** Update APIM Named Value (`McpAudienceAppId`)
4. **Step 3:** Create Foundry agent (`orders-assistant`) with MCP tool
5. **Step 4:** Update Chat App container env vars

## Environment Variables (azd)

Set by Bicep outputs:
- `AZURE_RESOURCE_GROUP`, `APIM_GATEWAY_URL`, `APIM_MCP_ENDPOINT`, `AI_FOUNDRY_PROJECT_ENDPOINT`, `COGNITIVE_ACCOUNT_NAME`, `AI_FOUNDRY_PROJECT_NAME`, `MCP_OAUTH_CONNECTION_NAME`

Set by postprovision hook:
- `MCP_AUDIENCE_APP_ID`, `MCP_OAUTH_CLIENT_ID`, `MCP_OAUTH_CLIENT_SECRET`, `CHAT_APP_ENTRA_CLIENT_ID`

## Development Notes

- **Platform:** Windows 11 + Git Bash
- **Python:** Use `python` not `python3` (Windows)
- **MSYS path fix:** `export MSYS_NO_PATHCONV=1` before `az` commands with resource ID paths
- **Foundry SDK:** `azure-ai-projects` v2 beta — `MCPTool` with `project_connection_id` for OAuth
- **Agent name:** `orders-assistant`
- **MCP server_label:** Must match `^[a-zA-Z0-9_]+$` (no hyphens)
- **gpt-4o required** — other models do NOT support MCP tools
- **After `azd down --purge`:** Use `azd env set COGNITIVE_ACCOUNT_SUFFIX 2` to avoid data plane caching
