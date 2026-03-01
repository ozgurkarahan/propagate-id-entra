"""Generate Azure resource inventory for Identity Propagation PoC.

Queries ARM APIs to discover all deployed resources, their properties,
dependencies, and endpoints. Outputs docs/azure-resources.md.

Usage:
  python scripts/generate_resource_inventory.py

Requires: az CLI logged in, azd environment provisioned.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# ─── Constants ───────────────────────────────────────────────────────────────

BASE_NAME = "identity-poc"
RESOURCE_GROUP = f"rg-{BASE_NAME}"
APIM_NAME = f"apim-{BASE_NAME}"
COGNITIVE_ACCOUNT = f"aoai-{BASE_NAME}3"
PROJECT_NAME = f"aiproj-{BASE_NAME}"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "azure-resources.md")

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


def az_rest(method: str, url: str):
    """Run az rest and return parsed JSON."""
    cmd = f'az rest --method {method} --url "{url}" -o json'
    result = subprocess.run(
        cmd, capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def get_sub_id() -> str:
    account = az("account show")
    return account.get("id", "") if account else ""


# ─── Resource collectors ─────────────────────────────────────────────────────


def collect_resource_group():
    rg = az(f"group show --name {RESOURCE_GROUP}")
    if not rg:
        return None
    return {
        "name": rg.get("name"),
        "type": "Microsoft.Resources/resourceGroups",
        "location": rg.get("location"),
        "state": rg.get("properties", {}).get("provisioningState"),
        "id": rg.get("id"),
    }


def collect_all_resources():
    """List all resources in the resource group."""
    resources = az(f"resource list -g {RESOURCE_GROUP}")
    return resources or []


def collect_cognitive_account():
    acct = az(f"cognitiveservices account show --name {COGNITIVE_ACCOUNT} -g {RESOURCE_GROUP}")
    if not acct:
        return None
    props = acct.get("properties", {})
    return {
        "name": acct.get("name"),
        "type": acct.get("type"),
        "kind": acct.get("kind"),
        "location": acct.get("location"),
        "state": props.get("provisioningState"),
        "endpoint": props.get("endpoint"),
        "openai_endpoint": props.get("endpoints", {}).get("OpenAI Language Model Instance API"),
        "allow_project_management": props.get("allowProjectManagement"),
        "custom_subdomain": props.get("customSubDomainName"),
        "id": acct.get("id"),
    }


def collect_project(sub_id: str):
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}?api-version=2025-04-01-preview"
    )
    proj = az_rest("get", url)
    if not proj:
        return None
    props = proj.get("properties", {})
    return {
        "name": proj.get("name"),
        "type": "Microsoft.CognitiveServices/accounts/projects",
        "location": proj.get("location"),
        "state": props.get("provisioningState"),
        "display_name": props.get("displayName"),
        "id": proj.get("id"),
    }


def collect_connections(sub_id: str):
    """List connections at project level."""
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}/connections?api-version=2025-04-01-preview"
    )
    data = az_rest("get", url)
    if not data:
        return []
    connections = []
    for conn in data.get("value", []):
        props = conn.get("properties", {})
        connections.append({
            "name": conn.get("name"),
            "type": "Microsoft.CognitiveServices/accounts/projects/connections",
            "category": props.get("category"),
            "auth_type": props.get("authType"),
            "target": props.get("target"),
            "is_shared": props.get("isSharedToAll"),
            "id": conn.get("id"),
        })
    return connections


def collect_deployments():
    deps = az(f"cognitiveservices account deployment list --name {COGNITIVE_ACCOUNT} -g {RESOURCE_GROUP}")
    if not deps:
        return []
    result = []
    for dep in deps:
        props = dep.get("properties", {})
        model = props.get("model", {})
        result.append({
            "name": dep.get("name"),
            "type": "Microsoft.CognitiveServices/accounts/deployments",
            "model_name": model.get("name"),
            "model_version": model.get("version"),
            "model_format": model.get("format"),
            "state": props.get("provisioningState"),
            "sku": dep.get("sku", {}).get("name"),
            "capacity": dep.get("sku", {}).get("capacity"),
            "id": dep.get("id"),
        })
    return result


def collect_apim():
    apim = az(f"apim show --name {APIM_NAME} -g {RESOURCE_GROUP}")
    if not apim:
        return None
    return {
        "name": apim.get("name"),
        "type": apim.get("type"),
        "location": apim.get("location"),
        "sku": apim.get("sku", {}).get("name"),
        "gateway_url": apim.get("gatewayUrl"),
        "identity_type": apim.get("identity", {}).get("type"),
        "principal_id": apim.get("identity", {}).get("principalId"),
        "state": apim.get("provisioningState"),
        "id": apim.get("id"),
    }


def collect_apim_apis():
    apis = az(f"apim api list --service-name {APIM_NAME} -g {RESOURCE_GROUP}")
    if not apis:
        return []
    result = []
    for api in apis:
        result.append({
            "name": api.get("name"),
            "display_name": api.get("displayName"),
            "path": api.get("path"),
            "protocols": api.get("protocols"),
            "type": api.get("apiType") or "rest",
            "id": api.get("id"),
        })
    return result


def collect_apim_mcp_api(sub_id: str):
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ApiManagement/service/{APIM_NAME}"
        f"/apis/orders-mcp?api-version=2025-03-01-preview"
    )
    api = az_rest("get", url)
    if not api:
        return None
    props = api.get("properties", {})
    tools = props.get("mcpTools", [])
    return {
        "name": "orders-mcp",
        "type": "mcp",
        "path": props.get("path"),
        "tool_count": len(tools),
        "tool_names": [t.get("name", "") for t in tools],
        "id": api.get("id"),
    }


def collect_container_app():
    app = az(f"containerapp show --name ca-orders-api -g {RESOURCE_GROUP}")
    if not app:
        return None
    props = app.get("properties", {})
    ingress = props.get("configuration", {}).get("ingress", {})
    return {
        "name": app.get("name"),
        "type": app.get("type"),
        "location": app.get("location"),
        "state": props.get("provisioningState"),
        "fqdn": ingress.get("fqdn"),
        "external": ingress.get("external"),
        "url": f"https://{ingress.get('fqdn', '')}" if ingress.get("fqdn") else None,
        "id": app.get("id"),
    }


def collect_role_assignments(sub_id: str):
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/providers/Microsoft.Authorization/roleAssignments"
        f"?api-version=2022-04-01&$filter=atScope()"
    )
    data = az_rest("get", url)
    if not data:
        return []
    cog_user_role = "a97b65f3-24c7-4388-baec-2e87135dc908"
    result = []
    for a in data.get("value", []):
        props = a.get("properties", {})
        role_def = props.get("roleDefinitionId", "")
        if role_def.endswith(cog_user_role):
            result.append({
                "principal_id": props.get("principalId"),
                "principal_type": props.get("principalType"),
                "role": "Cognitive Services User",
                "scope": props.get("scope"),
                "id": a.get("id"),
            })
    return result


def collect_foundry_agent():
    project_endpoint = f"https://{COGNITIVE_ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT_NAME}"
    try:
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient
    except ImportError:
        return None

    try:
        credential = DefaultAzureCredential()
        client = AIProjectClient(endpoint=project_endpoint, credential=credential)
        agents = list(client.agents.list_agents())
        for a in agents:
            if getattr(a, "name", "") == "orders-agent":
                tools = getattr(a, "tools", [])
                tool_types = []
                for t in tools:
                    tt = t.get("type") if isinstance(t, dict) else getattr(t, "type", "")
                    tool_types.append(tt)
                return {
                    "name": getattr(a, "name", ""),
                    "id": a.id,
                    "model": getattr(a, "model", ""),
                    "tool_types": tool_types,
                    "source": "Foundry SDK (not ARM)",
                }
    except Exception:
        return None
    return None


# ─── Markdown generation ─────────────────────────────────────────────────────


def generate_markdown(data: dict) -> str:
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("# Azure Resource Inventory")
    lines.append("")
    lines.append(f"> Auto-generated by `scripts/generate_resource_inventory.py` on {now}")
    lines.append(f"> Resource Group: `{RESOURCE_GROUP}`")
    lines.append("")

    # Summary table
    lines.append("## Resource Summary")
    lines.append("")
    lines.append("| # | Resource | Type | State |")
    lines.append("|---|----------|------|-------|")

    row_num = 0
    rg = data.get("resource_group")
    if rg:
        row_num += 1
        lines.append(f"| {row_num} | `{rg['name']}` | ResourceGroup | {rg['state']} |")

    for res in data.get("all_resources", []):
        row_num += 1
        name = res.get("name", "?")
        rtype = res.get("type", "?").split("/")[-1]
        state = res.get("provisioningState", "?")
        lines.append(f"| {row_num} | `{name}` | {rtype} | {state} |")

    lines.append("")

    # Cognitive Services
    lines.append("## AI Services Account")
    lines.append("")
    acct = data.get("cognitive_account")
    if acct:
        lines.append(f"- **Name:** `{acct['name']}`")
        lines.append(f"- **Kind:** {acct['kind']}")
        lines.append(f"- **Allow Project Management:** {acct['allow_project_management']}")
        lines.append(f"- **Endpoint:** `{acct['endpoint']}`")
        lines.append(f"- **OpenAI Endpoint:** `{acct['openai_endpoint']}`")
        lines.append(f"- **State:** {acct['state']}")
    else:
        lines.append("*Not found*")
    lines.append("")

    # Project
    lines.append("## AI Foundry Project")
    lines.append("")
    proj = data.get("project")
    if proj:
        lines.append(f"- **Name:** `{proj['name']}`")
        lines.append(f"- **Display Name:** {proj['display_name']}")
        lines.append(f"- **State:** {proj['state']}")
    else:
        lines.append("*Not found*")
    lines.append("")

    # Connections
    lines.append("## Project Connections")
    lines.append("")
    conns = data.get("connections", [])
    if conns:
        lines.append("| Name | Category | Auth | Target | Shared |")
        lines.append("|------|----------|------|--------|--------|")
        for c in conns:
            target = c.get("target", "") or ""
            if len(target) > 60:
                target = target[:57] + "..."
            lines.append(f"| `{c['name']}` | {c['category']} | {c['auth_type']} | `{target}` | {c['is_shared']} |")
    else:
        lines.append("*No connections found*")
    lines.append("")

    # Model Deployments
    lines.append("## Model Deployments")
    lines.append("")
    deps = data.get("deployments", [])
    if deps:
        lines.append("| Name | Model | Version | SKU | Capacity | State |")
        lines.append("|------|-------|---------|-----|----------|-------|")
        for d in deps:
            lines.append(f"| `{d['name']}` | {d['model_name']} | {d['model_version']} | {d['sku']} | {d['capacity']} | {d['state']} |")
    else:
        lines.append("*No deployments found*")
    lines.append("")

    # APIM
    lines.append("## API Management")
    lines.append("")
    apim = data.get("apim")
    if apim:
        lines.append(f"- **Name:** `{apim['name']}`")
        lines.append(f"- **SKU:** {apim['sku']}")
        lines.append(f"- **Gateway URL:** `{apim['gateway_url']}`")
        lines.append(f"- **Managed Identity:** {apim['identity_type']} (`{apim['principal_id']}`)")
        lines.append(f"- **State:** {apim['state']}")
    else:
        lines.append("*Not found*")
    lines.append("")

    # APIM APIs
    lines.append("### APIM APIs")
    lines.append("")
    apis = data.get("apim_apis", [])
    mcp = data.get("apim_mcp_api")
    if apis or mcp:
        lines.append("| Name | Display Name | Path | Type |")
        lines.append("|------|-------------|------|------|")
        for a in apis:
            lines.append(f"| `{a['name']}` | {a.get('display_name', '')} | /{a['path']} | {a['type']} |")
        if mcp:
            lines.append(f"| `{mcp['name']}` | Orders MCP | /{mcp.get('path', 'orders-mcp')} | mcp ({mcp['tool_count']} tools) |")
    else:
        lines.append("*No APIs found*")
    lines.append("")

    if mcp:
        lines.append("**MCP Tools:**")
        for t in mcp.get("tool_names", []):
            lines.append(f"- `{t}`")
        lines.append("")

    # Container App
    lines.append("## Container App (Orders API)")
    lines.append("")
    ca = data.get("container_app")
    if ca:
        lines.append(f"- **Name:** `{ca['name']}`")
        lines.append(f"- **FQDN:** `{ca['fqdn']}`")
        lines.append(f"- **URL:** `{ca['url']}`")
        lines.append(f"- **External:** {ca['external']}")
        lines.append(f"- **State:** {ca['state']}")
    else:
        lines.append("*Not found*")
    lines.append("")

    # Role Assignments
    lines.append("## Role Assignments (Cognitive Services User)")
    lines.append("")
    roles = data.get("role_assignments", [])
    if roles:
        lines.append("| Principal ID | Type | Role | Scope |")
        lines.append("|-------------|------|------|-------|")
        for r in roles:
            scope = r.get("scope", "")
            if len(scope) > 60:
                scope = "..." + scope[-57:]
            lines.append(f"| `{r['principal_id']}` | {r['principal_type']} | {r['role']} | `{scope}` |")
    else:
        lines.append("*No Cognitive Services User role assignments found*")
    lines.append("")

    # Foundry Agent
    lines.append("## Foundry Agent (Post-Provision)")
    lines.append("")
    agent = data.get("foundry_agent")
    if agent:
        lines.append(f"- **Name:** `{agent['name']}`")
        lines.append(f"- **Agent ID:** `{agent['id']}`")
        lines.append(f"- **Model:** {agent['model']}")
        lines.append(f"- **Tool Types:** {', '.join(agent['tool_types'])}")
        lines.append(f"- **Source:** {agent['source']}")
    else:
        lines.append("*Not found (run postprovision hook first)*")
    lines.append("")

    # Dependency diagram
    lines.append("## Resource Dependencies")
    lines.append("")
    lines.append("```")
    lines.append("Resource Group (rg-identity-poc)")
    lines.append("├── Tier 1 (parallel, no dependencies)")
    lines.append("│   ├── AI Services Account (aoai-identity-poc3)")
    lines.append("│   │   ├── AI Foundry Project (aiproj-identity-poc)")
    lines.append("│   │   │   ├── AzureOpenAI Connection (aoai-connection)")
    lines.append("│   │   │   └── ApiManagement Connection (apim-gateway)  ← needs APIM")
    lines.append("│   │   └── gpt-4o Deployment")
    lines.append("│   ├── Container Registry (acridentitypoc*)")
    lines.append("│   ├── Log Analytics + App Insights")
    lines.append("│   ├── Storage Account (stidentitypoc*)  [future]")
    lines.append("│   └── Key Vault (kv-identity-poc)  [future]")
    lines.append("├── Tier 2 (needs Registry + Monitoring)")
    lines.append("│   └── Container Apps Env + Orders API (ca-orders-api)")
    lines.append("├── Tier 3 (needs Container App + Cognitive)")
    lines.append("│   └── APIM (apim-identity-poc)")
    lines.append("│       ├── Orders REST API (orders-api)")
    lines.append("│       ├── OpenAI Proxy API (azure-openai)")
    lines.append("│       └── MCP API (orders-mcp)  ← post-provision hook")
    lines.append("├── Tier 4 (needs APIM + Cognitive)")
    lines.append("│   ├── Cognitive Services User → APIM MI (role assignment)")
    lines.append("│   └── ApiManagement Connection on Project (ai-gateway-connection)")
    lines.append("└── Post-Provision (SDK/REST only)")
    lines.append("    ├── APIM MCP API (orders-mcp)")
    lines.append("    └── Foundry Agent (orders-agent, gpt-4o + MCP tool)")
    lines.append("```")
    lines.append("")

    # Data flows
    lines.append("## Data Flows")
    lines.append("")
    lines.append("### MCP Flow (Agent → Orders API)")
    lines.append("```")
    lines.append("User prompt → Foundry Agent (gpt-4o)")
    lines.append("  → APIM MCP endpoint (/orders-mcp/mcp)")
    lines.append("    → APIM REST backend (/orders-api/*)")
    lines.append("      → Container App (ca-orders-api)")
    lines.append("        → Response back through chain")
    lines.append("```")
    lines.append("")
    lines.append("### AI Gateway Flow (Agent → OpenAI via APIM)")
    lines.append("```")
    lines.append("Foundry Agent inference request")
    lines.append("  → APIM AI Gateway (/openai/*)")
    lines.append("    → MI auth + token rate limit + metrics")
    lines.append("      → Azure OpenAI (aoai-identity-poc3)")
    lines.append("        → gpt-4o completion response")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    print("Collecting Azure resource inventory...")
    print(f"  Resource Group: {RESOURCE_GROUP}")

    sub_id = get_sub_id()
    if not sub_id:
        print("ERROR: Not logged in to Azure CLI. Run 'az login' first.")
        sys.exit(2)

    print(f"  Subscription: {sub_id}")

    data = {}

    print("  Querying resource group...")
    data["resource_group"] = collect_resource_group()

    print("  Listing all resources...")
    data["all_resources"] = collect_all_resources()

    print("  Querying AI Services account...")
    data["cognitive_account"] = collect_cognitive_account()

    print("  Querying AI Foundry project...")
    data["project"] = collect_project(sub_id)

    print("  Querying project connections...")
    data["connections"] = collect_connections(sub_id)

    print("  Querying model deployments...")
    data["deployments"] = collect_deployments()

    print("  Querying APIM...")
    data["apim"] = collect_apim()

    print("  Querying APIM APIs...")
    data["apim_apis"] = collect_apim_apis()

    print("  Querying APIM MCP API...")
    data["apim_mcp_api"] = collect_apim_mcp_api(sub_id)

    print("  Querying Container App...")
    data["container_app"] = collect_container_app()

    print("  Querying role assignments...")
    data["role_assignments"] = collect_role_assignments(sub_id)

    print("  Querying Foundry agent...")
    data["foundry_agent"] = collect_foundry_agent()

    print("\nGenerating markdown...")
    md = generate_markdown(data)

    output = os.path.normpath(OUTPUT_PATH)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"  Written to: {output}")

    # Count resources
    total = sum(1 for v in [
        data["resource_group"],
        data["cognitive_account"],
        data["project"],
        data["apim"],
        data["container_app"],
        data["foundry_agent"],
    ] if v)
    total += len(data.get("connections", []))
    total += len(data.get("deployments", []))
    total += len(data.get("apim_apis", []))
    total += len(data.get("role_assignments", []))
    if data.get("apim_mcp_api"):
        total += 1

    print(f"\n  {total} resources documented")
    print("Done.")


if __name__ == "__main__":
    main()
