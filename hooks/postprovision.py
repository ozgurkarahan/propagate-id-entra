"""Post-provision hook: create Entra apps + configure OAuth + create Foundry agent.

After Bicep deploys Azure resources and the OAuth connection (with placeholders),
this hook:
1. Creates Entra app registrations (az CLI — delegated permissions)
2. Sets identifierUris, creates client secret
3. Updates the OAuth connection with real credentials
4. Creates the Foundry agent with MCP tool

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


def create_and_configure_entra_apps():
    """Create Entra app registrations and configure OAuth for MCP.

    Uses az CLI (delegated permissions) since the managed tenant's ARM
    deployment engine lacks Application.ReadWrite.All for the Graph
    Bicep extension.

    Creates (idempotent — skips if apps already exist by displayName):
    1. MCP Gateway Audience app — exposes access_as_user scope
    2. Foundry OAuth Client app — redirect URI + API permission
    3. Service principals + admin consent grant

    Then configures:
    - identifierUris (api://{appId}) on audience app
    - Client secret on client app
    - Updates OAuth connection with real credentials
    """
    tenant_id = run("az account show --query tenantId -o tsv")
    if not tenant_id:
        print("  ERROR: Could not get tenant ID")
        return

    # Deterministic scope ID — stable across re-runs
    scope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-access-as-user/{tenant_id}"))

    # Include azd env name in display names to avoid collisions in shared tenants
    env_name = os.environ.get("AZURE_ENV_NAME", "default")
    audience_display_name = f"MCP Gateway Audience ({env_name})"
    client_display_name = f"Foundry OAuth Client ({env_name})"

    # --- 1. MCP Gateway Audience App ---
    print("\n  [1/7] MCP Gateway Audience app...")
    audience_app_id = run(
        f"az ad app list --filter \"displayName eq '{audience_display_name}'\" "
        "--query \"[0].appId\" -o tsv"
    )

    if audience_app_id:
        print(f"  Already exists: {audience_app_id}")
    else:
        audience_app_id = run(
            f'az ad app create --display-name "{audience_display_name}" '
            "--sign-in-audience AzureADMyOrg --query appId -o tsv"
        )
        if not audience_app_id:
            print("  ERROR: Failed to create app")
            return
        print(f"  Created: {audience_app_id}")

    # Ensure oauth2PermissionScopes are set (idempotent PATCH)
    audience_obj_id = run(
        f'az ad app show --id "{audience_app_id}" --query id -o tsv'
    )
    _graph_patch(audience_obj_id, {
        "api": {
            "oauth2PermissionScopes": [
                {
                    "id": scope_id,
                    "adminConsentDescription": "Access MCP Gateway as user",
                    "adminConsentDisplayName": "Access MCP Gateway as user",
                    "isEnabled": True,
                    "type": "User",
                    "userConsentDescription": "Access MCP Gateway as user",
                    "userConsentDisplayName": "Access MCP Gateway as user",
                    "value": "access_as_user",
                }
            ]
        }
    })
    print(f"  Scope: access_as_user ({scope_id})")

    # --- 2. Set identifierUris ---
    print("\n  [2/7] Identifier URI...")
    uri = f"api://{audience_app_id}"
    run(f'az ad app update --id "{audience_app_id}" --identifier-uris "{uri}"')
    print(f"  Set: {uri}")

    # --- 3. Foundry OAuth Client App ---
    print("\n  [3/7] Foundry OAuth Client app...")
    client_app_id = run(
        f"az ad app list --filter \"displayName eq '{client_display_name}'\" "
        "--query \"[0].appId\" -o tsv"
    )

    if client_app_id:
        print(f"  Already exists: {client_app_id}")
    else:
        client_app_id = run(
            f'az ad app create --display-name "{client_display_name}" '
            "--sign-in-audience AzureADMyOrg "
            "--is-fallback-public-client true "
            '--web-redirect-uris "https://ai.azure.com/auth/callback" '
            "--query appId -o tsv"
        )
        if not client_app_id:
            print("  ERROR: Failed to create app")
            return
        print(f"  Created: {client_app_id}")

    # Ensure requiredResourceAccess is set (idempotent PATCH)
    client_obj_id = run(
        f'az ad app show --id "{client_app_id}" --query id -o tsv'
    )
    _graph_patch(client_obj_id, {
        "requiredResourceAccess": [
            {
                "resourceAppId": audience_app_id,
                "resourceAccess": [
                    {"id": scope_id, "type": "Scope"}
                ],
            }
        ]
    })
    print("  API permission configured")

    # Ensure redirect URIs include ApiHub consent callback.
    # ApiHub connectorId = {projectInternalId-as-guid}-{connectionName}.
    # The project internalId is a 32-char hex string; format as GUID with hyphens.
    sub_id = run("az account show --query id -o tsv")
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "")
    account = os.environ.get("COGNITIVE_ACCOUNT_NAME", "")
    project_name = os.environ.get("AI_FOUNDRY_PROJECT_NAME", "")
    connection_name = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "mcp-oauth")

    redirect_uris = ["https://ai.azure.com/auth/callback"]
    if sub_id and rg and account and project_name:
        # Query the project's internalId from ARM
        project_url = (
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/resourceGroups/{rg}"
            f"/providers/Microsoft.CognitiveServices/accounts/{account}"
            f"/projects/{project_name}?api-version=2025-04-01-preview"
        )
        project_props = run(
            f'az rest --method GET --url "{project_url}" '
            f'--query "properties.internalId" -o tsv',
        )
        if project_props:
            # Format 32-char hex as GUID: 8-4-4-4-12
            iid = project_props.strip()
            guid = f"{iid[:8]}-{iid[8:12]}-{iid[12:16]}-{iid[16:20]}-{iid[20:]}"
            connector_id = f"{guid}-{connection_name}"
            redirect_uris.append(
                f"https://global.consent.azure-apim.net/redirect/{connector_id}"
            )
            print(f"  ApiHub connectorId: {connector_id}")
        else:
            print("  WARNING: Could not get project internalId — skipping ApiHub redirect URI")

    _graph_patch(client_obj_id, {
        "web": {"redirectUris": redirect_uris}
    })
    print(f"  Redirect URIs: {len(redirect_uris)} configured")

    # --- 4. Service principals ---
    print("\n  [4/7] Service principals...")
    audience_sp_id = run(
        f'az ad sp show --id "{audience_app_id}" --query id -o tsv'
    )
    if not audience_sp_id:
        audience_sp_id = run(
            f'az ad sp create --id "{audience_app_id}" --query id -o tsv'
        )
        print(f"  Audience SP created: {audience_sp_id}")
    else:
        print(f"  Audience SP exists: {audience_sp_id}")

    client_sp_id = run(
        f'az ad sp show --id "{client_app_id}" --query id -o tsv'
    )
    if not client_sp_id:
        client_sp_id = run(
            f'az ad sp create --id "{client_app_id}" --query id -o tsv'
        )
        print(f"  Client SP created: {client_sp_id}")
    else:
        print(f"  Client SP exists: {client_sp_id}")

    if not audience_sp_id or not client_sp_id:
        print("  ERROR: Failed to create service principals")
        return

    # --- 5. Admin consent grant ---
    print("\n  [5/7] Admin consent...")
    consent_file = _write_temp_json({
        "clientId": client_sp_id,
        "consentType": "AllPrincipals",
        "resourceId": audience_sp_id,
        "scope": "access_as_user",
    })
    try:
        result = run(
            'az rest --method POST '
            '--url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" '
            '--headers "Content-Type=application/json" '
            f'--body "@{consent_file}"',
            parse_json=True,
        )
        if result:
            print("  Consent granted")
        else:
            print("  Consent may already exist (or insufficient permissions)")
    finally:
        os.unlink(consent_file)

    # --- 6. Client secret ---
    print("\n  [6/7] Client secret...")
    secret = run(
        f'az ad app credential reset --id "{client_app_id}" '
        "--query password -o tsv"
    )
    if not secret:
        print("  ERROR: Failed to create client secret")
        return
    print(f"  Created (length: {len(secret)})")

    # Set azd env vars (used by grant-mcp-consent.py and verify script)
    azd_env_set("MCP_AUDIENCE_APP_ID", audience_app_id)
    azd_env_set("MCP_OAUTH_CLIENT_ID", client_app_id)
    azd_env_set("MCP_OAUTH_CLIENT_SECRET", secret)

    # --- 7. Update OAuth connection ---
    print("\n  [7/7] OAuth connection...")
    update_oauth_connection_secret(
        client_id=client_app_id,
        client_secret=secret,
        audience_app_id=audience_app_id,
    )


def update_oauth_connection_secret(client_id: str, client_secret: str, audience_app_id: str):
    """Recreate the OAuth connection with real credentials via ARM REST.

    Bicep deploys a RemoteTool connection with placeholder values. However,
    Bicep-created connections do NOT register the ApiHub connector that Foundry
    needs for interactive OAuth consent. The fix is to DELETE the Bicep-created
    connection and PUT a fresh one via ARM REST, which triggers ApiHub setup.
    """
    connection_name = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "mcp-oauth")
    mcp_endpoint = os.environ.get("APIM_MCP_ENDPOINT", "")

    if not mcp_endpoint:
        apim_gateway = os.environ.get("APIM_GATEWAY_URL", "")
        if apim_gateway:
            mcp_endpoint = f"{apim_gateway}/orders-mcp/mcp"
    if not mcp_endpoint:
        print("  WARNING: No MCP endpoint — skipping connection update")
        return

    sub_id = run("az account show --query id -o tsv")
    tenant_id = run("az account show --query tenantId -o tsv")
    if not sub_id or not tenant_id:
        print("  WARNING: Could not get subscription/tenant ID")
        return

    rg = os.environ.get("AZURE_RESOURCE_GROUP", "")
    account = os.environ.get("COGNITIVE_ACCOUNT_NAME", "")
    project = os.environ.get("AI_FOUNDRY_PROJECT_NAME", "")

    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
        f"/projects/{project}/connections/{connection_name}"
        f"?api-version=2025-04-01-preview"
    )

    # Step 1: Delete the Bicep-created connection (ApiHub connector not registered)
    print(f"  Deleting Bicep-created connection '{connection_name}'...")
    delete_result = run(
        f'az rest --method DELETE --url "{url}"',
    )
    if delete_result is None:
        print("  Connection deleted (or did not exist)")
    else:
        print("  Connection deleted")

    # Step 2: Recreate via ARM REST PUT (triggers ApiHub connector registration)
    body = {
        "properties": {
            "authType": "OAuth2",
            "category": "RemoteTool",
            "group": "GenericProtocol",
            "connectorName": connection_name,
            "target": mcp_endpoint,
            "credentials": {
                "clientId": client_id,
                "clientSecret": client_secret,
            },
            "authorizationUrl": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
            "tokenUrl": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "refreshUrl": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "scopes": [f"api://{audience_app_id}/access_as_user"],
            "metadata": {"type": "custom_MCP"},
            "isSharedToAll": True,
        }
    }

    body_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(body, body_file)
    body_file.close()

    try:
        print(f"  Recreating connection '{connection_name}' via ARM REST...")
        result = run(
            f'az rest --method PUT --url "{url}" '
            f'--headers "Content-Type=application/json" '
            f'--body "@{body_file.name}"',
            parse_json=True,
        )
        if result:
            print("  Connection created successfully (ApiHub connector registered)")
        else:
            print("  WARNING: Failed to create connection (may need manual fix)")
    finally:
        os.unlink(body_file.name)


def update_apim_named_value(audience_app_id: str):
    """Update the APIM McpAudienceAppId Named Value with the real app URI.

    Bicep deploys a placeholder value; this function PATCHes it with the real
    api://{appId} value so the validate-azure-ad-token policy works.
    """
    sub_id = run("az account show --query id -o tsv")
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "")
    apim_name = os.environ.get("APIM_NAME", "")

    if not sub_id:
        print("  WARNING: Could not get subscription ID — skipping Named Value update")
        return

    # The audience value must match the token's aud claim (identifierUris format)
    audience_value = f"api://{audience_app_id}"

    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/namedValues/McpAudienceAppId"
        f"?api-version=2024-06-01-preview"
    )

    body = {
        "properties": {
            "displayName": "McpAudienceAppId",
            "value": audience_value,
            "secret": False,
        }
    }

    body_file = _write_temp_json(body)
    try:
        print(f"  Updating APIM Named Value 'McpAudienceAppId' = {audience_value}...")
        result = run(
            f'az rest --method PUT --url "{url}" '
            f'--headers "Content-Type=application/json" '
            f'--body "@{body_file}"',
            parse_json=True,
        )
        if result:
            print("  Named Value updated successfully")
        else:
            print("  WARNING: Failed to update Named Value (may need manual fix)")
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

    audience_app_id = os.environ.get("MCP_AUDIENCE_APP_ID", "")

    agent_name = "orders-assistant"

    # Connection config env vars — needed by /api/reset-mcp-auth endpoint
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    account = os.environ.get("COGNITIVE_ACCOUNT_NAME", "")
    project_name = os.environ.get("AI_FOUNDRY_PROJECT_NAME", "")
    apim_gateway = os.environ.get("APIM_GATEWAY_URL", "")
    mcp_oauth_client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    mcp_oauth_client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")

    print(f"  Updating {chat_app_name} environment variables...")
    result = run(
        f'az containerapp update --name {chat_app_name} --resource-group {rg} '
        f'--set-env-vars '
        f'"CHAT_APP_ENTRA_CLIENT_ID={client_id}" '
        f'"TENANT_ID={tenant_id}" '
        f'"MCP_AUDIENCE_APP_ID={audience_app_id}" '
        f'"AGENT_NAME={agent_name}" '
        f'"AZURE_SUBSCRIPTION_ID={sub_id}" '
        f'"AZURE_RESOURCE_GROUP={rg}" '
        f'"COGNITIVE_ACCOUNT_NAME={account}" '
        f'"AI_FOUNDRY_PROJECT_NAME={project_name}" '
        f'"APIM_GATEWAY_URL={apim_gateway}" '
        f'"MCP_OAUTH_CLIENT_ID={mcp_oauth_client_id}" '
        f'"MCP_OAUTH_CLIENT_SECRET={mcp_oauth_client_secret}"',
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

    # Build Orders MCPTool — always include OAuth connection (apps are always deployed)
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

    oauth_connection = os.environ.get("MCP_OAUTH_CONNECTION_NAME")
    if oauth_connection:
        mcp_tool_kwargs["project_connection_id"] = oauth_connection
        print(f"Orders OAuth connection: {oauth_connection}")

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
    if oauth_connection:
        print("\n--- Smoke test skipped (OAuth configured) ---")
        print("Run the interactive OAuth test to verify end-to-end:")
        print("  python scripts/test-agent-oauth.py")
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

    # Step 1: Create Entra apps + configure OAuth connection
    print("--- Step 1: Create and configure Entra apps ---")
    try:
        create_and_configure_entra_apps()
    except Exception as e:
        print(f"\nWARNING: Entra configuration failed (non-fatal): {e}")
        traceback.print_exc()

    # Step 1b: Create Chat App Entra registration
    print("\n--- Step 1b: Chat App Entra registration ---")
    try:
        create_chat_app_entra_registration()
    except Exception as e:
        print(f"\nWARNING: Chat App Entra registration failed (non-fatal): {e}")
        traceback.print_exc()

    # Step 2: Update APIM Named Value with real audience app ID
    print("\n--- Step 2: Update APIM Named Value ---")
    audience_app_id = os.environ.get("MCP_AUDIENCE_APP_ID", "")
    if audience_app_id:
        try:
            update_apim_named_value(audience_app_id)
        except Exception as e:
            print(f"\nWARNING: APIM Named Value update failed (non-fatal): {e}")
            traceback.print_exc()
    else:
        print("  Skipping — MCP_AUDIENCE_APP_ID not set")

    # Step 3: Create Foundry agent with MCP tool
    print("\n--- Step 3: Create Foundry agent ---")
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

    # Step 4: Update Chat App env vars
    print("\n--- Step 4: Update Chat App settings ---")
    try:
        update_chat_app_settings()
    except Exception as e:
        print(f"\nWARNING: Chat App settings update failed (non-fatal): {e}")
        traceback.print_exc()

    print("\n=== Post-provision hook complete ===")


if __name__ == "__main__":
    main()
