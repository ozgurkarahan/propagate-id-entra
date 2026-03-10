# Propagate ID Entra

**End-to-end identity propagation from browser through AI agents to backend APIs — no service accounts in the data path.**

[![azd compatible](https://img.shields.io/badge/azd-compatible-blue)](https://learn.microsoft.com/azure/developer/azure-developer-cli/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![Bicep IaC](https://img.shields.io/badge/IaC-Bicep-orange)](https://learn.microsoft.com/azure/azure-resource-manager/bicep/)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> A proof-of-concept showing how a user's Entra ID token can flow from a browser, through an AI Foundry agent with MCP tools, through API Management, all the way to a backend API — preserving the caller's identity at every hop. Deployed with a single `azd up`.

## Architecture

```mermaid
flowchart TD
    Browser["Browser<br/>(MSAL.js SPA)"]
    Entra["Entra ID"]
    ChatApp["Chat App<br/>(FastAPI + Container App)"]
    Agent["AI Foundry Agent<br/>(gpt-4o)"]
    APIM["API Management"]
    MCP["/orders-mcp<br/>validate-jwt"]
    REST["/orders-api"]
    OAI["/openai<br/>AI Gateway"]
    Orders["Orders API<br/>(FastAPI + Container App)"]
    AOI["Azure OpenAI"]

    Browser -- "1 Sign in" --> Entra
    Entra -- "access token" --> Browser
    Browser -- "2 POST /api/chat<br/>{message, token}" --> ChatApp
    ChatApp -- "3 Responses API<br/>(UserTokenCredential)" --> Agent
    Agent -- "4 MCP tool call<br/>(UserEntraToken)" --> APIM
    APIM --- MCP
    APIM --- REST
    APIM --- OAI
    MCP --> Orders
    REST --> Orders
    OAI -- "Managed Identity" --> AOI
```

## How Identity Flows

```mermaid
sequenceDiagram
    participant B as Browser
    participant E as Entra ID
    participant C as Chat App
    participant F as Foundry Agent
    participant A as APIM
    participant O as Orders API

    B->>E: Sign in (MSAL.js)
    E-->>B: Access token (aud=ai.azure.com)
    B->>C: POST /api/chat {message, access_token}
    Note over C: Wraps token in<br/>UserTokenCredential
    C->>F: responses.create()
    Note over F: Agent decides to<br/>call MCP tool
    F->>A: MCP request + Bearer token<br/>(UserEntraToken passthrough)
    Note over A: validate-jwt<br/>(aud, issuer, signature)
    A->>O: Forward request + JWT claims
    O-->>A: Order data
    A-->>F: MCP response
    F-->>C: Agent response
    C-->>B: Chat reply
```

| Hop | Auth Type | User Identity Preserved? |
|-----|-----------|--------------------------|
| Browser → Chat App → Foundry | Delegated (MSAL.js access token) | Yes |
| Foundry → APIM MCP | UserEntraToken passthrough | Yes |
| APIM → Azure OpenAI | Managed Identity (service-to-service) | No |

> [!IMPORTANT]
> No OAuth2 client credentials, no consent prompts, no client secrets, no refresh token expiry. The user's existing Entra token is passed directly at every hop via a **UserEntraToken** connection.

## Quick Start

> [!TIP]
> `azd up` does everything: provisions Azure resources via Bicep, builds and deploys containers to ACR, then runs a post-provision hook to create the Entra app registration and Foundry agent.

### Prerequisites

- **Azure subscription** with Owner/Contributor access
- **Azure CLI** (`az`) — logged in
- **Azure Developer CLI** (`azd`)
- **Python** 3.9+
- **Git**

Docker is not required locally — container builds run remotely on ACR.

### Deploy

```bash
git clone https://github.com/ozgurkarahan/propagate-id-entra.git
cd propagate-id-entra
azd env new propagate-id-entra
azd up
```

### Verify

```bash
python scripts/verify_deployment.py
python scripts/test-agent.py
```

## What `azd up` Does

```mermaid
flowchart LR
    P["azd provision"]
    D["azd deploy"]
    H["postprovision hook"]

    P -- "Bicep modules<br/>(13 modules, 4 tiers)" --> D
    D -- "Build + deploy<br/>Orders API & Chat App" --> H
    H -- "1. Entra app registration<br/>2. Foundry agent creation<br/>3. Chat App env vars" --> Done["Ready"]
```

## Project Structure

<details>
<summary>Click to expand</summary>

| Path | Description |
|------|-------------|
| `infra/main.bicep` | Subscription-scoped Bicep orchestrator |
| `infra/modules/` | 13 Bicep modules (APIM, Cognitive, Container Apps, etc.) |
| `infra/policies/` | APIM policies (JWT validation, AI Gateway, RFC 9728 PRM) |
| `src/orders-api/` | FastAPI Orders CRUD backend (6 endpoints, 8 seed orders) |
| `src/chat-app/` | FastAPI backend + vanilla JS SPA with MSAL.js |
| `hooks/postprovision.py` | Entra app registration + Foundry agent creation |
| `scripts/` | Deployment verification, diagnostics, agent testing |
| `docs/` | Deep-dive architecture, identity & security, reference docs |

</details>

## Learn More

- [**Identity & Security Architecture**](docs/identity-security.md) — Entra app registration, managed identities, JWT validation, RFC 9728, security design decisions
- [**Deep Dive**](docs/deep-dive.md) — ARM resource details, data flows, step-by-step build guide
- [**AGENT.md**](AGENT.md) — Architecture diagrams, auth flow details, IaC principles, development reference

## License

[MIT](LICENSE)
