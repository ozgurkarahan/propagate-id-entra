"""Post-provision hook: create Chat App Entra registration + Foundry agent.

After Bicep deploys Azure resources (including the UserEntraToken MCP
connection), this hook:
1. Creates the Chat App Entra app registration (az CLI — delegated permissions)
2. Creates the Foundry agent with MCP tool
3. Updates the Chat App container with env vars

Uses az CLI for Entra ops because the Graph Bicep extension requires
Application.ReadWrite.All on the ARM deployment identity, which is not
available in managed tenants.

Uses azure-ai-projects v2 SDK for Foundry agent (no ARM resource type).
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid


def run(cmd: str, parse_json: bool = False):
    """Run a shell command and return stdout (or parsed JSON)."""
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


def azd_env_set(key: str, value: str):
    """Set an azd environment variable."""
    subprocess.run(
        f'azd env set {key} "{value}"',
        shell=True, capture_output=True, text=True,
    )
    os.environ[key] = value
    print(f"  azd env set {key}={value[:20]}{'...' if len(value) > 20 else ''}")


def _write_temp_json(data):
    """Write data as JSON to a temp file and return the file path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


def _graph_patch(object_id: str, body: dict):
    """PATCH a Microsoft Graph application resource."""
    body_file = _write_temp_json(body)
    try:
        return run(
            f'az rest --method PATCH '
            f'--url "https://graph.microsoft.com/v1.0/applications/{object_id}" '
            f'--headers "Content-Type=application/json" '
            f'--body "@{body_file}"',
            parse_json=True,
        )
    finally:
        os.unlink(body_file)


def create_chat_app_entra_registration():
    """Create Entra app registration for the Chat App SPA (MSAL.js).

    Creates (idempotent — skips if app exists by displayName):
    - SPA app registration with redirect URIs for localhost + deployed FQDN
    - Service principal
    - Sets CHAT_APP_ENTRA_CLIENT_ID via azd env set
    """
    env_name = os.environ.get("AZURE_ENV_NAME", "default")
    display_name = f"Chat App ({env_name})"

    # Check if already exists
    app_id = run(
        f"az ad app list --filter \"displayName eq '{display_name}'\" "
        "--query \"[0].appId\" -o tsv"
    )

    if app_id:
        print(f"  Already exists: {app_id}")
    else:
        app_id = run(
            f'az ad app create --display-name "{display_name}" '
            "--sign-in-audience AzureADMyOrg "
            "--is-fallback-public-client true "
            "--query appId -o tsv"
        )
        if not app_id:
            print("  ERROR: Failed to create Chat App Entra registration")
            return
        print(f"  Created: {app_id}")

    # Configure SPA redirect URIs
    chat_app_fqdn = os.environ.get("CHAT_APP_FQDN", "")
    redirect_uris = ["http://localhost:8080"]
    if chat_app_fqdn:
        redirect_uris.append(f"https://{chat_app_fqdn}")

    obj_id = run(f'az ad app show --id "{app_id}" --query id -o tsv')
    _graph_patch(obj_id, {
        "spa": {"redirectUris": redirect_uris}
    })
    print(f"  SPA redirect URIs: {redirect_uris}")

    # Declare required resource access for Azure AI Services (https://ai.azure.com)
    # Without this, Entra rejects token requests for https://ai.azure.com/.default
    _graph_patch(obj_id, {
        "requiredResourceAccess": [
            {
                "resourceAppId": "18a66f5f-dbdf-4c17-9dd7-1634712a9cbe",  # Azure AI (ai.azure.com)
                "resourceAccess": [
                    {
                        "id": "1a7925b5-f871-417a-9b8b-303f9f29fa10",  # user_impersonation
                        "type": "Scope",
                    }
                ],
            }
        ]
    })
    print("  Required resource access: Azure AI Services (user_impersonation)")

    # Ensure service principal
    sp_id = run(f'az ad sp show --id "{app_id}" --query id -o tsv')
    if not sp_id:
        sp_id = run(f'az ad sp create --id "{app_id}" --query id -o tsv')
        print(f"  SP created: {sp_id}")
    else:
        print(f"  SP exists: {sp_id}")

    azd_env_set("CHAT_APP_ENTRA_CLIENT_ID", app_id)


def update_chat_app_settings():
    """Update chat Container App with Entra client ID and tenant ID.

    These env vars are needed by the chat app's /api/config endpoint
    to serve MSAL configuration to the browser.
    """
    chat_app_name = os.environ.get("CHAT_APP_CONTAINER_APP_NAME", "ca-chat-app")
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "")
    client_id = os.environ.get("CHAT_APP_ENTRA_CLIENT_ID", "")
    tenant_id = run("az account show --query tenantId -o tsv")

    if not client_id or not tenant_id or not rg:
        print("  WARNING: Missing env vars — skipping chat app settings update")
        return

    agent_name = "orders-assistant"

    print(f"  Updating {chat_app_name} environment variables...")
    result = run(
        f'az containerapp update --name {chat_app_name} --resource-group {rg} '
        f'--set-env-vars '
        f'"CHAT_APP_ENTRA_CLIENT_ID={client_id}" '
        f'"TENANT_ID={tenant_id}" '
        f'"AGENT_NAME={agent_name}"',
    )
    if result is not None:
        print("  Container App env vars updated")
    else:
        print("  WARNING: Failed to update Container App env vars")


def create_agent(mcp_endpoint: str):
    """Create a Foundry agent with MCP tools using the v2 SDK."""
    project_endpoint = os.environ.get("AI_FOUNDRY_PROJECT_ENDPOINT")

    if not project_endpoint or not mcp_endpoint:
        print("WARNING: Missing endpoint vars — skipping agent creation.")
        return

    print(f"\nProject endpoint: {project_endpoint}")
    print(f"MCP endpoint:     {mcp_endpoint}")

    from azure.identity import DefaultAzureCredential
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition, MCPTool

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
    )

    agent_name = "orders-assistant"
    print(f"\nCreating agent '{agent_name}'...")

    # Build Orders MCPTool with UserEntraToken connection
    mcp_tool_kwargs = {
        "server_label": "orders_mcp",
        "server_url": mcp_endpoint,
        "require_approval": "never",
        "allowed_tools": [
            "list-orders",
            "get-order",
            "create-order",
            "update-order",
            "delete-order",
            "health-check",
        ],
    }

    mcp_connection = os.environ.get("MCP_CONNECTION_NAME")
    if mcp_connection:
        mcp_tool_kwargs["project_connection_id"] = mcp_connection
        print(f"Orders connection: {mcp_connection}")

    orders_mcp_tool = MCPTool(**mcp_tool_kwargs)
    tools = [orders_mcp_tool]

    instructions = (
        "You are an assistant with access to backend systems. "
        "Use the Orders MCP tools to list, get, create, update, and delete orders. "
        "Always confirm destructive actions with the user."
    )

    agent = project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model="gpt-4o",
            instructions=instructions,
            tools=tools,
        ),
    )
    print(f"Agent created: name={agent.name}, version={agent.version}, id={agent.id}")
    print(f"  Tools: {len(tools)} MCP tool(s) configured")

    # --- Smoke test ---
    if mcp_connection:
        print("\n--- Smoke test skipped (connection configured) ---")
        print("Run the interactive test to verify end-to-end:")
        print("  python scripts/test-agent.py")
    else:
        print("\n--- Smoke test ---")
        print("Running agent via Responses API (this may take 30-60s)...")

        openai_client = project_client.get_openai_client()
        conversation = openai_client.conversations.create()

        response = openai_client.responses.create(
            conversation=conversation.id,
            input="List all orders. For each order, include the order ID and customer name.",
            extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
        )

        output_text = getattr(response, "output_text", "")
        if output_text and "ORD-" in output_text:
            print(f"Smoke test passed — agent returned order data.")
            print(f"  Preview: {output_text[:200]}...")
        elif output_text:
            print(f"Smoke test completed but no order data found.")
            print(f"  Preview: {output_text[:200]}...")
        else:
            print("Smoke test completed — no output text (check response).")

        openai_client.close()

    print(f"\nAgent: {agent.name} v{agent.version}")


def main():
    print("=== Post-provision hook ===\n")

    # Step 1: Create Chat App Entra registration
    print("--- Step 1: Chat App Entra registration ---")
    try:
        create_chat_app_entra_registration()
    except Exception as e:
        print(f"\nWARNING: Chat App Entra registration failed (non-fatal): {e}")
        traceback.print_exc()

    # Step 2: Create Foundry agent with MCP tool
    print("\n--- Step 2: Create Foundry agent ---")
    mcp_endpoint = os.environ.get("APIM_MCP_ENDPOINT")
    if not mcp_endpoint:
        # Fallback: construct from APIM gateway URL
        apim_gateway_url = os.environ.get("APIM_GATEWAY_URL")
        if apim_gateway_url:
            mcp_endpoint = f"{apim_gateway_url}/orders-mcp/mcp"
            print(f"Using constructed MCP endpoint: {mcp_endpoint}")
        else:
            print("WARNING: No MCP endpoint available — skipping agent creation.")
            mcp_endpoint = None

    if mcp_endpoint:
        print(f"MCP endpoint: {mcp_endpoint}")
        try:
            create_agent(mcp_endpoint)
        except Exception as e:
            print(f"\nWARNING: Agent creation failed (non-fatal): {e}")
            print("Re-run with: python hooks/postprovision.py")
            traceback.print_exc()

    # Step 3: Update Chat App env vars
    print("\n--- Step 3: Update Chat App settings ---")
    try:
        update_chat_app_settings()
    except Exception as e:
        print(f"\nWARNING: Chat App settings update failed (non-fatal): {e}")
        traceback.print_exc()

    print("\n=== Post-provision hook complete ===")


if __name__ == "__main__":
    main()
