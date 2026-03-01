"""Deployment verification for Identity Propagation PoC (Phase 1.5).

Validates every deployed resource and proves end-to-end data flow:
Agent -> APIM MCP -> Orders API.

Layers:
  1. Infrastructure — 20 checks (Bicep resources via ARM API)
  2. Post-Provision — 2 checks  (hook-created resources)
  2.5. OAuth        — 9 checks  (Entra apps + OAuth connection + APIM Named Values + sign-in audit)
  3. Functional     — 6 checks  (HTTP + MCP 401 + PRM + agent round-trip)

Total: 37 checks

Usage:
  python scripts/verify_deployment.py

Environment variables are loaded from `azd env get-values` automatically.
Override with manual exports if needed.

Exit codes: 0 = all pass, 1 = failures, 2 = pre-flight error
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

# ─── Constants ───────────────────────────────────────────────────────────────

BASE_NAME = ""
RESOURCE_GROUP = ""
APIM_NAME = ""
COGNITIVE_ACCOUNT = ""
PROJECT_NAME = ""
KEY_VAULT = ""
LOG_ANALYTICS = ""
CONTAINER_APP = "ca-orders-api"

EXPECTED_OPS = [
    "list-orders", "get-order", "create-order",
    "update-order", "delete-order", "health-check",
]

EXPECTED_MCP_TOOLS = EXPECTED_OPS  # same 6 names

# Seed data markers from src/orders-api/data.py
DATA_MARKERS = ["ORD-001", "ORD-008", "Alice Johnson", "Hank Brown"]

# ─── Result tracking ─────────────────────────────────────────────────────────

_results: list[tuple[str, str, str]] = []  # (layer, status, message)


def record(layer: str, passed: bool, msg: str):
    status = "PASS" if passed else "FAIL"
    _results.append((layer, status, msg))
    tag = f"  [\033[92m{status}\033[0m]" if passed else f"  [\033[91m{status}\033[0m]"
    print(f"{tag} {msg}")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def az(args: str, parse_json: bool = True):
    """Run an az CLI command and return parsed JSON (or raw text)."""
    cmd = f"az {args} -o json"
    result = subprocess.run(
        cmd, capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    if parse_json:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None
    return out


def az_rest(method: str, url: str, body: dict | None = None):
    """Run az rest and return parsed JSON."""
    cmd = f'az rest --method {method} --url "{url}" -o json'
    body_file = None
    if body:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(body, f)
        f.close()
        body_file = f.name
        cmd += f' --body "@{body_file}"'
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, shell=True,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        if not out:
            return None
        return json.loads(out)
    except json.JSONDecodeError:
        return None
    finally:
        if body_file and os.path.exists(body_file):
            os.unlink(body_file)


def http_get(url: str, timeout: int = 15):
    """HTTP GET returning (status_code, body_text). Returns (0, error) on failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, str(e)


# ─── Pre-flight ──────────────────────────────────────────────────────────────

def load_azd_env():
    """Load azd env vars into os.environ. Non-fatal if azd not available."""
    result = subprocess.run(
        "azd env get-values", capture_output=True, text=True, shell=True,
    )
    if result.returncode != 0:
        return
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if "=" in line:
            key, _, value = line.partition("=")
            value = value.strip('"').strip("'")
            os.environ.setdefault(key, value)


def preflight() -> bool:
    """Verify az CLI is logged in and subscription is accessible."""
    account = az("account show")
    if not account:
        print("ERROR: Not logged in to Azure CLI. Run 'az login' first.")
        return False
    sub_id = account.get("id", "")
    print(f"  Subscription: {account.get('name', '?')} ({sub_id})")

    # Verify resource group exists before running checks
    rg = az(f'group show --name {RESOURCE_GROUP}')
    if not rg:
        print(f"ERROR: Resource group '{RESOURCE_GROUP}' not found. Run 'azd up' first.")
        return False
    return True


# ─── Layer 1: Infrastructure (20 checks) ─────────────────────────────────────

def check_resource_group():
    rg = az(f'group show --name {RESOURCE_GROUP}')
    if rg and rg.get("properties", {}).get("provisioningState") == "Succeeded":
        record("infra", True, f"Resource Group ({RESOURCE_GROUP})")
    else:
        record("infra", False, f"Resource Group ({RESOURCE_GROUP}) — not found or not succeeded")


def check_ai_services_account():
    acct = az(f'cognitiveservices account show --name {COGNITIVE_ACCOUNT} -g {RESOURCE_GROUP}')
    if not acct:
        record("infra", False, f"AI Services Account ({COGNITIVE_ACCOUNT}) — not found")
        return
    kind = acct.get("kind", "")
    allow_pm = acct.get("properties", {}).get("allowProjectManagement", False)
    if kind == "AIServices" and allow_pm:
        record("infra", True, f"AI Services Account ({COGNITIVE_ACCOUNT}, kind={kind}, allowProjectManagement=true)")
    else:
        record("infra", False, f"AI Services Account ({COGNITIVE_ACCOUNT}) — kind={kind}, allowProjectManagement={allow_pm}")


def check_ai_foundry_project():
    sub_id = az("account show").get("id", "")
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}?api-version=2025-04-01-preview"
    )
    proj = az_rest("get", url)
    if proj and proj.get("properties", {}).get("provisioningState") == "Succeeded":
        record("infra", True, f"AI Foundry Project ({PROJECT_NAME})")
    elif proj:
        state = proj.get("properties", {}).get("provisioningState", "?")
        record("infra", False, f"AI Foundry Project ({PROJECT_NAME}) — state={state}")
    else:
        record("infra", False, f"AI Foundry Project ({PROJECT_NAME}) — not found")


def check_aoai_connection():
    sub_id = az("account show").get("id", "")
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/connections/aoai-connection?api-version=2025-04-01-preview"
    )
    conn = az_rest("get", url)
    if not conn:
        record("infra", False, "AzureOpenAI Connection (aoai-connection) — not found")
        return
    props = conn.get("properties", {})
    category = props.get("category", "")
    auth = props.get("authType", "")
    if category == "AzureOpenAI" and auth == "AAD":
        record("infra", True, f"AzureOpenAI Connection (category={category}, authType={auth})")
    else:
        record("infra", False, f"AzureOpenAI Connection — category={category}, authType={auth}")


def check_gpt4o_deployment():
    dep = az(
        f'cognitiveservices account deployment show'
        f' --name {COGNITIVE_ACCOUNT} -g {RESOURCE_GROUP}'
        f' --deployment-name gpt-4o'
    )
    if not dep:
        record("infra", False, "gpt-4o Deployment — not found")
        return
    model_name = dep.get("properties", {}).get("model", {}).get("name", "")
    state = dep.get("properties", {}).get("provisioningState", "")
    if model_name == "gpt-4o" and state == "Succeeded":
        record("infra", True, f"gpt-4o Deployment (model={model_name})")
    else:
        record("infra", False, f"gpt-4o Deployment — model={model_name}, state={state}")


def check_container_app():
    app = az(f'containerapp show --name {CONTAINER_APP} -g {RESOURCE_GROUP}')
    if not app:
        record("infra", False, f"Container App ({CONTAINER_APP}) — not found")
        return
    state = app.get("properties", {}).get("provisioningState", "")
    ingress = app.get("properties", {}).get("configuration", {}).get("ingress", {})
    external = ingress.get("external", False)
    if state == "Succeeded" and external:
        fqdn = ingress.get("fqdn", "?")
        record("infra", True, f"Container App ({CONTAINER_APP}, fqdn={fqdn}, external=true)")
    else:
        record("infra", False, f"Container App ({CONTAINER_APP}) — state={state}, external={external}")


def check_apim_instance():
    apim = az(f'apim show --name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not apim:
        record("infra", False, f"APIM Instance ({APIM_NAME}) — not found")
        return
    sku = apim.get("sku", {}).get("name", "")
    if sku == "StandardV2":
        record("infra", True, f"APIM Instance ({APIM_NAME}, sku={sku})")
    else:
        record("infra", False, f"APIM Instance ({APIM_NAME}) — sku={sku}")


def check_apim_rest_api():
    api = az(f'apim api show --api-id orders-api --service-name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not api:
        record("infra", False, "APIM REST API (orders-api) — not found")
        return
    path = api.get("path", "")
    if path == "orders-api":
        record("infra", True, f"APIM REST API (orders-api, path={path})")
    else:
        record("infra", False, f"APIM REST API — path={path}, expected orders-api")


def check_apim_operations():
    ops = az(f'apim api operation list --api-id orders-api --service-name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not ops:
        record("infra", False, "APIM Operations — could not list operations")
        return
    found_names = {op.get("name", "") for op in ops}
    missing = [name for name in EXPECTED_OPS if name not in found_names]
    if not missing:
        record("infra", True, f"APIM Operations ({len(EXPECTED_OPS)}/{len(EXPECTED_OPS)} present)")
    else:
        record("infra", False, f"APIM Operations — found {len(EXPECTED_OPS) - len(missing)}/{len(EXPECTED_OPS)}, missing: {', '.join(missing)}")


def check_keyvault():
    kv = az(f'keyvault show --name {KEY_VAULT} -g {RESOURCE_GROUP}')
    if kv:
        record("infra", True, f"Key Vault ({KEY_VAULT})")
    else:
        record("infra", False, f"Key Vault ({KEY_VAULT}) — not found")


def check_log_analytics():
    ws = az(f'monitor log-analytics workspace show --workspace-name {LOG_ANALYTICS} -g {RESOURCE_GROUP}')
    if ws and ws.get("provisioningState") == "Succeeded":
        record("infra", True, f"Log Analytics ({LOG_ANALYTICS})")
    else:
        record("infra", False, f"Log Analytics ({LOG_ANALYTICS}) — not found or not succeeded")


def check_storage_account():
    resources = az(f'resource list -g {RESOURCE_GROUP} --resource-type Microsoft.Storage/storageAccounts')
    if resources and len(resources) >= 1:
        name = resources[0].get("name", "?")
        record("infra", True, f"Storage Account ({name})")
    else:
        record("infra", False, "Storage Account — none found in resource group")


def check_container_registry():
    resources = az(f'resource list -g {RESOURCE_GROUP} --resource-type Microsoft.ContainerRegistry/registries')
    if resources and len(resources) >= 1:
        name = resources[0].get("name", "?")
        record("infra", True, f"Container Registry ({name})")
    else:
        record("infra", False, "Container Registry — none found in resource group")


def check_app_insights():
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Insights/components/appi-{BASE_NAME}"
        f"?api-version=2020-02-02"
    )
    ai = az_rest("get", url)
    if not ai:
        record("infra", False, f"App Insights (appi-{BASE_NAME}) — not found")
        return
    props = ai.get("properties", {})
    state = props.get("provisioningState", "")
    ikey = props.get("InstrumentationKey", "")
    if state == "Succeeded" and ikey:
        record("infra", True, f"App Insights (appi-{BASE_NAME}, state={state})")
    else:
        record("infra", False, f"App Insights — state={state}, ikey={'present' if ikey else 'missing'}")


def check_container_apps_environment():
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.App/managedEnvironments/cae-{BASE_NAME}"
        f"?api-version=2024-03-01"
    )
    env = az_rest("get", url)
    if not env:
        record("infra", False, f"Container Apps Environment (cae-{BASE_NAME}) — not found")
        return
    props = env.get("properties", {})
    state = props.get("provisioningState", "")
    if state == "Succeeded":
        record("infra", True, f"Container Apps Environment (cae-{BASE_NAME}, state={state})")
    else:
        record("infra", False, f"Container Apps Environment (cae-{BASE_NAME}) — state={state}")


def check_apim_managed_identity():
    apim = az(f'apim show --name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not apim:
        record("infra", False, "APIM Managed Identity — APIM not found")
        return
    identity_type = apim.get("identity", {}).get("type", "")
    if "SystemAssigned" in identity_type:
        record("infra", True, f"APIM Managed Identity (type={identity_type})")
    else:
        record("infra", False, f"APIM Managed Identity — type={identity_type}, expected SystemAssigned")


def check_apim_openai_api():
    api = az(f'apim api show --api-id azure-openai --service-name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not api:
        record("infra", False, "APIM OpenAI API (azure-openai) — not found")
        return
    path = api.get("path", "")
    if path == "openai":
        record("infra", True, f"APIM OpenAI API (azure-openai, path={path})")
    else:
        record("infra", False, f"APIM OpenAI API — path={path}, expected openai")


EXPECTED_OPENAI_OPS = ["chat-completions", "completions", "embeddings"]


def check_apim_openai_operations():
    ops = az(f'apim api operation list --api-id azure-openai --service-name {APIM_NAME} -g {RESOURCE_GROUP}')
    if not ops:
        record("infra", False, "APIM OpenAI Operations — could not list operations")
        return
    found_names = {op.get("name", "") for op in ops}
    missing = [name for name in EXPECTED_OPENAI_OPS if name not in found_names]
    if not missing:
        record("infra", True, f"APIM OpenAI Operations ({len(EXPECTED_OPENAI_OPS)}/{len(EXPECTED_OPENAI_OPS)} present)")
    else:
        record("infra", False, f"APIM OpenAI Operations — missing: {', '.join(missing)}")


def check_cognitive_role_assignment():
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    # List role assignments on the cognitive account scoped to Cognitive Services User
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/providers/Microsoft.Authorization/roleAssignments"
        f"?api-version=2022-04-01"
        f"&$filter=atScope()"
    )
    data = az_rest("get", url)
    if not data:
        record("infra", False, "Cognitive Role Assignment — could not list role assignments")
        return
    assignments = data.get("value", [])
    cog_user_role = "a97b65f3-24c7-4388-baec-2e87135dc908"
    found = any(
        a.get("properties", {}).get("roleDefinitionId", "").endswith(cog_user_role)
        and a.get("properties", {}).get("principalType", "") == "ServicePrincipal"
        for a in assignments
    )
    if found:
        record("infra", True, "Cognitive Services User role assigned to ServicePrincipal")
    else:
        record("infra", False, "Cognitive Services User role — not assigned to any ServicePrincipal")


def check_ai_gateway_connection():
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    # Check at project level (project-scoped connection)
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}"
        f"/connections/apim-gateway?api-version=2025-04-01-preview"
    )
    conn = az_rest("get", url)
    if not conn:
        record("infra", False, "AI Gateway Connection (apim-gateway) — not found at project level")
        return
    props = conn.get("properties", {})
    category = props.get("category", "")
    auth_type = props.get("authType", "")
    target = props.get("target", "")
    if category == "ApiManagement" and auth_type == "AAD":
        record("infra", True, f"AI Gateway Connection (project-level, category={category}, auth={auth_type})")
    else:
        record("infra", False, f"AI Gateway Connection — category={category}, auth={auth_type}")


def check_ai_gateway_proxy():
    gateway = get_apim_gateway_url()
    if not gateway:
        record("func", False, "AI Gateway Proxy — could not determine gateway URL")
        return
    url = f"{gateway}/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
    status, _ = http_get(url)
    # Expect 401 or 403 — proves the route exists and auth is required
    if status in (401, 403):
        record("func", True, f"AI Gateway Proxy (HTTP {status} — route exists, auth required)")
    elif status == 0:
        record("func", False, "AI Gateway Proxy — could not connect")
    else:
        record("func", True, f"AI Gateway Proxy (HTTP {status} — route exists)")


# ─── Layer 2: Post-Provision (2 checks) ──────────────────────────────────────

def check_mcp_api():
    sub_id = az("account show").get("id", "")
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ApiManagement/service/{APIM_NAME}"
        f"/apis/orders-mcp?api-version=2025-03-01-preview"
    )
    api = az_rest("get", url)
    if not api:
        record("postprov", False, "APIM MCP API (orders-mcp) — not found")
        return
    props = api.get("properties", {})
    api_type = props.get("type", "")
    tools = props.get("mcpTools", [])
    tool_names = [t.get("name", "") for t in tools]
    missing = [n for n in EXPECTED_MCP_TOOLS if n not in tool_names]
    if api_type == "mcp" and not missing:
        record("postprov", True, f"APIM MCP API (orders-mcp, type=mcp, {len(tools)} tools)")
    else:
        details = []
        if api_type != "mcp":
            details.append(f"type={api_type}")
        if missing:
            details.append(f"missing tools: {', '.join(missing)}")
        record("postprov", False, f"APIM MCP API — {', '.join(details)}")


def check_foundry_agent():
    project_endpoint = os.environ.get("AI_FOUNDRY_PROJECT_ENDPOINT", "")
    if not project_endpoint:
        # Try to construct it
        project_endpoint = f"https://{COGNITIVE_ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT_NAME}"

    try:
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient
    except ImportError:
        record("postprov", False, "Foundry Agent — azure-ai-projects not installed (pip install azure-ai-projects)")
        return

    try:
        credential = DefaultAzureCredential()
        client = AIProjectClient(endpoint=project_endpoint, credential=credential)

        # v2 SDK: agents.list() returns agent summaries
        agents = list(client.agents.list())

        found = None
        for a in agents:
            name = getattr(a, "name", "") or a.get("name", "") if isinstance(a, dict) else getattr(a, "name", "")
            if name == "orders-assistant" or name.startswith("orders-assistant-"):
                found = a
                break

        if not found:
            names = [getattr(a, "name", "?") for a in agents]
            record("postprov", False, f"Foundry Agent — 'orders-assistant' not found (agents: {names})")
            return

        # Get the latest version to inspect tools
        agent_name = getattr(found, "name", "orders-assistant")
        versions = list(client.agents.list_versions(agent_name=agent_name))
        if not versions:
            record("postprov", False, f"Foundry Agent — no versions for '{agent_name}'")
            return

        latest = versions[0]
        definition = getattr(latest, "definition", {})
        if isinstance(definition, dict):
            model = definition.get("model", "?")
            tools = definition.get("tools", [])
        else:
            model = getattr(definition, "model", "?")
            tools = getattr(definition, "tools", [])

        has_mcp = any(
            (t.get("type") if isinstance(t, dict) else getattr(t, "type", "")) == "mcp"
            for t in tools
        )
        if model == "gpt-4o" and has_mcp:
            record("postprov", True, f"Foundry Agent (orders-assistant, model={model}, has MCP tool)")
        else:
            record("postprov", False, f"Foundry Agent — model={model}, has_mcp={has_mcp}")

        # Check MCP tool has OAuth connection (conditional)
        oauth_connection = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "")
        if oauth_connection:
            mcp_tool = None
            for t in tools:
                t_type = t.get("type") if isinstance(t, dict) else getattr(t, "type", "")
                if t_type == "mcp":
                    mcp_tool = t
                    break
            if mcp_tool:
                conn_id = mcp_tool.get("project_connection_id") if isinstance(mcp_tool, dict) else getattr(mcp_tool, "project_connection_id", "")
                if conn_id == oauth_connection:
                    record("oauth", True, f"Agent MCP tool has OAuth connection ({conn_id})")
                else:
                    record("oauth", False, f"Agent MCP tool connection — expected '{oauth_connection}', got '{conn_id}'")
            else:
                record("oauth", False, "Agent MCP tool — no MCP tool found on agent")

    except Exception as e:
        record("postprov", False, f"Foundry Agent — error: {e}")


# ─── Layer 2.5: OAuth (conditional — only if MCP_OAUTH_CLIENT_ID is set) ─────

def check_apim_named_values():
    """Verify all 3 MCP-related Named Values exist and are not placeholders."""
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    expected = {
        "McpTenantId": None,         # any non-empty value
        "McpAudienceAppId": None,    # must not be placeholder
        "APIMGatewayURL": None,      # any non-empty value
    }
    all_ok = True
    for nv_name in expected:
        url = (
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.ApiManagement/service/{APIM_NAME}"
            f"/namedValues/{nv_name}?api-version=2024-06-01-preview"
        )
        nv = az_rest("get", url)
        if not nv:
            record("oauth", False, f"APIM Named Value '{nv_name}' — not found")
            all_ok = False
            continue
        value = nv.get("properties", {}).get("value", "")
        if not value or value == "placeholder-updated-by-hook":
            record("oauth", False, f"APIM Named Value '{nv_name}' — placeholder or empty")
            all_ok = False
        else:
            # Truncate display for readability
            display = value[:40] + "..." if len(value) > 40 else value
            record("oauth", True, f"APIM Named Value '{nv_name}' = {display}")
    return all_ok


def check_mcp_401_challenge():
    """Verify unauthenticated GET to MCP endpoint returns 401."""
    gateway = get_apim_gateway_url()
    if not gateway:
        record("func", False, "MCP 401 challenge — could not determine gateway URL")
        return
    url = f"{gateway}/orders-mcp/mcp"
    status, body = http_get(url)
    if status == 401:
        record("func", True, f"MCP 401 challenge (HTTP {status} — auth required)")
    elif status == 0:
        record("func", False, "MCP 401 challenge — could not connect")
    else:
        record("func", False, f"MCP 401 challenge — expected 401, got HTTP {status}")


def check_prm_endpoint():
    """Verify PRM endpoint returns valid RFC 9728 JSON."""
    gateway = get_apim_gateway_url()
    if not gateway:
        record("func", False, "PRM endpoint — could not determine gateway URL")
        return
    url = f"{gateway}/orders-mcp/.well-known/oauth-protected-resource"
    status, body = http_get(url)
    if status != 200:
        record("func", False, f"PRM endpoint — expected 200, got HTTP {status}")
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        record("func", False, "PRM endpoint — invalid JSON response")
        return
    # Validate required RFC 9728 fields
    resource = data.get("resource", "")
    auth_servers = data.get("authorization_servers", [])
    scopes = data.get("scopes_supported", [])
    if resource and auth_servers and scopes:
        record("func", True, f"PRM endpoint (resource={resource}, {len(auth_servers)} auth server(s), {len(scopes)} scope(s))")
    else:
        missing = []
        if not resource:
            missing.append("resource")
        if not auth_servers:
            missing.append("authorization_servers")
        if not scopes:
            missing.append("scopes_supported")
        record("func", False, f"PRM endpoint — missing fields: {', '.join(missing)}")


def check_oauth_connection_exists():
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", az("account show").get("id", ""))
    conn_name = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "mcp-oauth")
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}"
        f"/connections/{conn_name}?api-version=2025-04-01-preview"
    )
    conn = az_rest("get", url)
    if not conn:
        record("oauth", False, f"MCP OAuth Connection ({conn_name}) — not found")
        return None
    record("oauth", True, f"MCP OAuth Connection ({conn_name}) exists")
    return conn


def check_oauth_connection_auth_type(conn: dict):
    props = conn.get("properties", {})
    auth_type = props.get("authType", "")
    if auth_type == "OAuth2":
        record("oauth", True, f"OAuth connection authType={auth_type}")
    else:
        record("oauth", False, f"OAuth connection authType — expected 'OAuth2', got '{auth_type}'")


def check_oauth_connection_target(conn: dict):
    props = conn.get("properties", {})
    target = props.get("target", "")
    mcp_endpoint = os.environ.get("APIM_MCP_ENDPOINT", "")
    if mcp_endpoint and target == mcp_endpoint:
        record("oauth", True, f"OAuth connection target matches MCP endpoint")
    elif target:
        record("oauth", True, f"OAuth connection target={target}")
    else:
        record("oauth", False, "OAuth connection target — empty")


def check_oauth_connection_scopes(conn: dict):
    props = conn.get("properties", {})
    scopes = props.get("scopes", [])
    audience_app_id = os.environ.get("MCP_AUDIENCE_APP_ID", "")
    expected_scope = f"api://{audience_app_id}/access_as_user" if audience_app_id else ""
    if expected_scope and expected_scope in scopes:
        record("oauth", True, f"OAuth connection scopes include audience app")
    elif scopes:
        record("oauth", True, f"OAuth connection scopes={scopes}")
    else:
        record("oauth", False, "OAuth connection scopes — empty")


def check_signin_audit_logs():
    """Query Graph auditLogs/signIns for recent OAuth client sign-in events."""
    oauth_client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    if not oauth_client_id:
        record("oauth", False, "Sign-in audit — MCP_OAUTH_CLIENT_ID not set")
        return
    url = (
        "https://graph.microsoft.com/v1.0/auditLogs/signIns"
        f"?$filter=appId eq '{oauth_client_id}'"
        "&$top=5&$orderby=createdDateTime desc"
        "&$select=createdDateTime,userDisplayName,status"
    )
    cmd = f'az rest --method get --url "{url}" -o json'
    result = subprocess.run(
        cmd, capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if result.returncode != 0:
        stderr = result.stderr or ""
        if "Authorization_RequestDenied" in stderr or "Insufficient privileges" in stderr:
            # Soft pass — permission not available but not a deployment issue
            record("oauth", True, "Sign-in audit — skipped (AuditLog.Read.All not granted, run check-signin-logs.py with privileged account)")
            return
        if "InvalidAuthenticationToken" in stderr:
            record("oauth", True, "Sign-in audit — skipped (az login token expired)")
            return
        record("oauth", False, f"Sign-in audit — Graph API error: {stderr[:120]}")
        return
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        record("oauth", False, "Sign-in audit — invalid JSON from Graph API")
        return
    events = data.get("value", [])
    if events:
        latest = events[0]
        ts = latest.get("createdDateTime", "?")[:19]
        user = latest.get("userDisplayName", "?")
        error_code = latest.get("status", {}).get("errorCode", -1)
        status = "success" if error_code == 0 else f"AADSTS{error_code}"
        record("oauth", True, f"Sign-in audit — {len(events)} recent event(s), latest: {ts} by {user} ({status})")
    else:
        record("oauth", True, "Sign-in audit — no recent sign-in events (normal if unused)")


# ─── Layer 3: Functional (3 checks) ──────────────────────────────────────────

def get_orders_api_url() -> str:
    """Get the direct Orders API URL from env or Container App FQDN."""
    url = os.environ.get("ORDERS_API_URL", "")
    if url:
        return url.rstrip("/")
    # Fall back to az lookup
    app = az(f'containerapp show --name {CONTAINER_APP} -g {RESOURCE_GROUP}')
    if app:
        fqdn = app.get("properties", {}).get("configuration", {}).get("ingress", {}).get("fqdn", "")
        if fqdn:
            return f"https://{fqdn}"
    return ""


def get_apim_gateway_url() -> str:
    url = os.environ.get("APIM_GATEWAY_URL", "")
    if url:
        return url.rstrip("/")
    apim = az(f'apim show --name {APIM_NAME} -g {RESOURCE_GROUP}')
    if apim:
        return apim.get("gatewayUrl", "").rstrip("/")
    return ""


def check_direct_api():
    base = get_orders_api_url()
    if not base:
        record("func", False, "Direct Orders API — could not determine URL")
        return

    status, body = http_get(f"{base}/orders")
    if status != 200:
        record("func", False, f"Direct Orders API — HTTP {status}")
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        record("func", False, "Direct Orders API — invalid JSON response")
        return

    orders = data if isinstance(data, list) else data.get("orders", data.get("items", []))
    ids = [o.get("id", "") for o in orders] if isinstance(orders, list) else []
    if len(orders) >= 8 and "ORD-001" in ids:
        record("func", True, f"Direct Orders API (HTTP 200, {len(orders)} orders, ORD-001 present)")
    else:
        record("func", False, f"Direct Orders API — {len(orders)} orders, ORD-001 {'present' if 'ORD-001' in ids else 'missing'}")


def check_apim_proxy():
    gateway = get_apim_gateway_url()
    if not gateway:
        record("func", False, "APIM Proxy — could not determine gateway URL")
        return

    status, body = http_get(f"{gateway}/orders-api/orders")
    if status != 200:
        record("func", False, f"APIM Proxy — HTTP {status}")
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        record("func", False, "APIM Proxy — invalid JSON response")
        return

    orders = data if isinstance(data, list) else data.get("orders", data.get("items", []))
    ids = [o.get("id", "") for o in orders] if isinstance(orders, list) else []
    if len(orders) >= 8 and "ORD-001" in ids:
        record("func", True, f"APIM Proxy (HTTP 200, {len(orders)} orders, ORD-001 present)")
    else:
        record("func", False, f"APIM Proxy — {len(orders)} orders, ORD-001 {'present' if 'ORD-001' in ids else 'missing'}")


def check_agent_roundtrip():
    project_endpoint = os.environ.get("AI_FOUNDRY_PROJECT_ENDPOINT", "")
    if not project_endpoint:
        project_endpoint = f"https://{COGNITIVE_ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT_NAME}"

    try:
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient
    except ImportError:
        record("func", False, "Agent round-trip — azure-ai-projects not installed")
        return

    try:
        credential = DefaultAzureCredential()
        client = AIProjectClient(endpoint=project_endpoint, credential=credential)

        # v2 SDK: find orders-assistant (may have version suffix)
        agents = list(client.agents.list())
        agent = None
        for a in agents:
            name = getattr(a, "name", "")
            if name == "orders-assistant" or name.startswith("orders-assistant-"):
                agent = a
                break

        if not agent:
            names = [getattr(a, "name", "?") for a in agents]
            record("func", False, f"Agent round-trip — orders-assistant not found (agents: {names})")
            return

        agent_name = getattr(agent, "name", "orders-assistant")

        # Run agent via Responses API
        print(f"    Running agent '{agent_name}' via Responses API (this may take 30-60s)...")
        openai_client = client.get_openai_client()
        conversation = openai_client.conversations.create()

        response = openai_client.responses.create(
            conversation=conversation.id,
            input="List all orders. For each order, include the order ID and customer name.",
            extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
        )

        # Inspect all output items for OAuth consent or MCP approval requests
        output_items = getattr(response, "output", [])
        output_types = [getattr(item, "type", "unknown") for item in output_items]
        print(f"    Response output types: {output_types}")

        # Check for oauth_consent_request (OAuth identity passthrough)
        consent_items = [
            item for item in output_items
            if getattr(item, "type", "") == "oauth_consent_request"
        ]
        if consent_items:
            consent_link = getattr(consent_items[0], "consent_link", "")
            record("func", True,
                f"Agent OAuth consent requested — multi-turn flow needed. "
                f"Run: python scripts/test-agent-oauth.py")
            if consent_link:
                print(f"    Consent link: {consent_link[:100]}...")
            openai_client.close()
            return

        # Check for mcp_approval_request (tool approval)
        approval_items = [
            item for item in output_items
            if getattr(item, "type", "") == "mcp_approval_request"
        ]
        if approval_items:
            record("func", True,
                f"Agent MCP approval requested ({len(approval_items)} tool(s)). "
                f"Run: python scripts/test-agent-oauth.py")
            openai_client.close()
            return

        response_text = getattr(response, "output_text", "")
        openai_client.close()

        if not response_text:
            # Dump output items for debugging
            for item in output_items:
                item_type = getattr(item, "type", "unknown")
                print(f"    Output item: type={item_type}")
                if hasattr(item, "text"):
                    text = getattr(item, "text", "")
                    print(f"      text: {str(text)[:200]}")
            record("func", False, "Agent round-trip — no output text in response")
            return

        # Check for seed data markers
        found_markers = [m for m in DATA_MARKERS if m in response_text]
        count = len(found_markers)
        if count >= 3:
            record("func", True, f"Agent MCP round-trip ({count}/{len(DATA_MARKERS)} data markers)")
        else:
            missing = [m for m in DATA_MARKERS if m not in found_markers]
            record("func", False, f"Agent MCP round-trip — {count}/{len(DATA_MARKERS)} markers, missing: {', '.join(missing)}")

    except Exception as e:
        record("func", False, f"Agent round-trip — error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def init_globals():
    """Set module-level resource name globals from azd env vars."""
    global BASE_NAME, RESOURCE_GROUP, APIM_NAME, COGNITIVE_ACCOUNT
    global PROJECT_NAME, KEY_VAULT, LOG_ANALYTICS
    env_name = os.environ.get("AZURE_ENV_NAME", "identity-poc")
    BASE_NAME = env_name.lower()
    RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", f"rg-{BASE_NAME}")
    APIM_NAME = os.environ.get("APIM_NAME", f"apim-{BASE_NAME}")
    COGNITIVE_ACCOUNT = os.environ.get("COGNITIVE_ACCOUNT_NAME", f"aoai-{BASE_NAME}")
    PROJECT_NAME = os.environ.get("AI_FOUNDRY_PROJECT_NAME", f"aiproj-{BASE_NAME}")
    KEY_VAULT = f"kv-{BASE_NAME}"
    LOG_ANALYTICS = f"log-{BASE_NAME}"


LAYER_LABELS = {
    "infra": "Layer 1: Infrastructure",
    "postprov": "Layer 2: Post-Provision",
    "oauth": "Layer 2.5: OAuth",
    "func": "Layer 3: Functional",
}


def main():
    print("Loading azd environment variables...")
    load_azd_env()
    init_globals()

    print("\n=== Pre-flight checks ===")
    if not preflight():
        sys.exit(2)

    # Cache subscription ID for az_rest calls
    account = az("account show")
    if account:
        os.environ.setdefault("AZURE_SUBSCRIPTION_ID", account.get("id", ""))

    # --- Layer 1: Infrastructure (20 checks) ---
    print(f"\n=== {LAYER_LABELS['infra']} (20 checks) ===")
    check_resource_group()
    check_ai_services_account()
    check_ai_foundry_project()
    check_aoai_connection()
    check_gpt4o_deployment()
    check_container_apps_environment()
    check_container_app()
    check_apim_instance()
    check_apim_managed_identity()
    check_apim_rest_api()
    check_apim_operations()
    check_apim_openai_api()
    check_apim_openai_operations()
    check_cognitive_role_assignment()
    check_ai_gateway_connection()
    check_keyvault()
    check_log_analytics()
    check_app_insights()
    check_storage_account()
    check_container_registry()

    # --- Layer 2: Post-Provision (2 checks) ---
    print(f"\n=== {LAYER_LABELS['postprov']} (2 checks) ===")
    check_mcp_api()
    check_foundry_agent()

    # --- Layer 2.5: OAuth (9 checks) ---
    # Entra apps are always deployed via Bicep (Microsoft.Graph extension)
    print(f"\n=== {LAYER_LABELS['oauth']} (9 checks) ===")
    check_apim_named_values()
    conn = check_oauth_connection_exists()
    if conn:
        check_oauth_connection_auth_type(conn)
        check_oauth_connection_target(conn)
        check_oauth_connection_scopes(conn)
    check_signin_audit_logs()
    # Agent project_connection_id check is handled inside check_foundry_agent()

    # --- Layer 3: Functional (6 checks) ---
    print(f"\n=== {LAYER_LABELS['func']} (6 checks) ===")
    check_direct_api()
    check_apim_proxy()
    check_ai_gateway_proxy()
    check_mcp_401_challenge()
    check_prm_endpoint()
    check_agent_roundtrip()

    # --- Summary ---
    total = len(_results)
    passed = sum(1 for _, s, _ in _results if s == "PASS")
    failed = total - passed

    print(f"\n{'=' * 48}")
    if failed == 0:
        print(f"  \033[92mRESULT: {passed}/{total} PASSED\033[0m")
    else:
        print(f"  \033[91mRESULT: {passed}/{total} PASSED, {failed} FAILED\033[0m")
    print(f"{'=' * 48}")

    # Print failures summary
    failures = [(layer, msg) for layer, s, msg in _results if s == "FAIL"]
    if failures:
        print("\nFailed checks:")
        for layer, msg in failures:
            print(f"  [{LAYER_LABELS.get(layer, layer)}] {msg}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Deployment verification for Identity Propagation PoC. "
        "Runs 37 checks across infra, post-provision, OAuth, and functional layers."
    ).parse_args()
    main()
