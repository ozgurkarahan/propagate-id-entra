"""Grant OAuth consent for the MCP OAuth connection via device code flow.

This script completes the OAuth authorization flow that populates the Foundry
connection with a user's refresh token, enabling the agent to acquire delegated
OAuth tokens when calling MCP tools.

One-time manual step after `azd up` with OAuth configured.

Flow:
1. Load azd env vars (MCP_OAUTH_CLIENT_ID, MCP_OAUTH_CLIENT_SECRET, etc.)
2. Device code flow: user authenticates and authorizes access_as_user scope
3. PUT to CognitiveServices connection to store the refresh token
4. Print success + next steps

Usage: python scripts/grant-mcp-consent.py
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

TENANT_ID = ""
COGNITIVE_ACCOUNT = ""
PROJECT_NAME = ""
RESOURCE_GROUP = ""


def load_azd_env():
    """Load azd env vars into os.environ."""
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


def init_globals():
    """Set module-level globals from azd env vars."""
    global TENANT_ID, COGNITIVE_ACCOUNT, PROJECT_NAME, RESOURCE_GROUP
    TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
    if not TENANT_ID:
        result = subprocess.run(
            "az account show --query tenantId -o tsv",
            capture_output=True, text=True, shell=True,
        )
        TENANT_ID = result.stdout.strip()
    RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "")
    COGNITIVE_ACCOUNT = os.environ.get("COGNITIVE_ACCOUNT_NAME", "")
    PROJECT_NAME = os.environ.get("AI_FOUNDRY_PROJECT_NAME", "")


def device_code_flow(client_id: str, client_secret: str, scope: str) -> dict:
    """Run device code flow and return the token response."""
    token_base = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0"

    # Step 1: Request device code
    print("Requesting device code...")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": scope,
    }).encode()
    req = urllib.request.Request(
        f"{token_base}/devicecode",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req).read())

    device_code = resp["device_code"]
    interval = resp.get("interval", 5)
    expires_in = resp.get("expires_in", 900)

    print()
    print("=" * 60)
    print(f"  {resp['message']}")
    print("=" * 60)
    print()

    # Step 2: Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        try:
            # Build token poll params — include client_secret only if present
            # (public clients don't need it; confidential clients require it)
            poll_params = {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            }
            if client_secret:
                poll_params["client_secret"] = client_secret
            poll_data = urllib.parse.urlencode(poll_params).encode()
            poll_req = urllib.request.Request(
                f"{token_base}/token",
                data=poll_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp = json.loads(urllib.request.urlopen(poll_req).read())
            print("\nAuthentication successful!")
            return token_resp
        except urllib.error.HTTPError as e:
            try:
                error_body = json.loads(e.read().decode())
            except Exception:
                print(f"\nHTTP Error {e.code}")
                continue
            error_code = error_body.get("error", "")
            error_desc = error_body.get("error_description", "")
            if error_code == "authorization_pending":
                print(".", end="", flush=True)
                continue
            elif error_code == "slow_down":
                interval += 5
                continue
            elif error_code == "expired_token":
                print("\nDevice code expired. Please try again.")
                return {}
            elif "AADSTS7000218" in error_desc and "client_secret" in poll_params:
                # Confidential client error — retry without secret (public client)
                print("\n  Retrying as public client (without client_secret)...")
                del poll_params["client_secret"]
                continue
            elif "AADSTS700025" in error_desc and "client_secret" not in poll_params:
                # Public client error — retry with secret (confidential client)
                print("\n  Retrying as confidential client (with client_secret)...")
                poll_params["client_secret"] = client_secret
                continue
            else:
                print(f"\nError: {error_desc}")
                return {}
        except Exception:
            print("x", end="", flush=True)
            continue

    print("\nTimeout waiting for authentication.")
    return {}


def get_arm_token() -> str:
    """Get ARM management token via DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential
    credential = DefaultAzureCredential()
    token = credential.get_token("https://management.azure.com/.default")
    return token.token


def update_connection(
    arm_token: str,
    connection_name: str,
    client_id: str,
    client_secret: str,
    audience_app_id: str,
    mcp_endpoint: str,
    refresh_token: str,
) -> bool:
    """Update the Foundry connection with the refresh token via ARM REST."""
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        account = subprocess.run(
            "az account show --query id -o tsv",
            capture_output=True, text=True, shell=True,
        )
        sub_id = account.stdout.strip()

    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}/connections/{connection_name}"
        f"?api-version=2025-04-01-preview"
    )

    body = json.dumps({
        "properties": {
            "authType": "OAuth2",
            "category": "RemoteTool",
            "group": "GenericProtocol",
            "connectorName": connection_name,
            "target": mcp_endpoint,
            "credentials": {
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
            },
            "authorizationUrl": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize",
            "tokenUrl": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            "refreshUrl": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            "scopes": [f"api://{audience_app_id}/access_as_user"],
            "metadata": {"type": "custom_MCP"},
            "isSharedToAll": True,
        }
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {arm_token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )

    try:
        resp = json.loads(urllib.request.urlopen(req).read())
        print(f"Connection '{connection_name}' updated with refresh token.")
        props = resp.get("properties", {})
        print(f"  AuthType: {props.get('authType')}")
        print(f"  Target: {props.get('target')}")
        return True
    except urllib.error.HTTPError as e:
        error = e.read().decode()
        print(f"Failed to update connection: {e.code}")
        print(error[:500])
        return False


def main():
    print("=" * 60)
    print("  Grant OAuth Consent for MCP Connection")
    print("=" * 60)
    print()

    # Load azd env vars
    load_azd_env()
    init_globals()

    # Read required env vars
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")
    audience_app_id = os.environ.get("MCP_AUDIENCE_APP_ID", "")
    connection_name = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "mcp-oauth")
    mcp_endpoint = os.environ.get("APIM_MCP_ENDPOINT", "")

    if not client_id or not client_secret or not audience_app_id:
        print("ERROR: Missing OAuth env vars. Run 'azd up' with OAuth configured first.")
        print(f"  MCP_OAUTH_CLIENT_ID:     {'set' if client_id else 'MISSING'}")
        print(f"  MCP_OAUTH_CLIENT_SECRET: {'set' if client_secret else 'MISSING'}")
        print(f"  MCP_AUDIENCE_APP_ID:     {'set' if audience_app_id else 'MISSING'}")
        sys.exit(1)

    if not mcp_endpoint:
        apim_gateway = os.environ.get("APIM_GATEWAY_URL", "")
        if apim_gateway:
            mcp_endpoint = f"{apim_gateway}/orders-mcp/mcp"
        else:
            print("ERROR: Cannot determine MCP endpoint. Run 'azd up' first.")
            sys.exit(1)

    print(f"Connection:  {connection_name}")
    print(f"Client ID:   {client_id}")
    print(f"Audience:    {audience_app_id}")
    print(f"MCP target:  {mcp_endpoint}")
    print()

    # Step 1: Device code flow
    scope = f"api://{audience_app_id}/access_as_user offline_access"
    tokens = device_code_flow(client_id, client_secret, scope)
    if not tokens:
        print("\nFailed to authenticate.")
        sys.exit(1)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("\nNo refresh token received. Ensure 'offline_access' scope is granted.")
        sys.exit(1)

    print(f"\nGot refresh token (length: {len(refresh_token)})")

    # Step 2: Get ARM token
    print("\nGetting ARM management token...")
    arm_token = get_arm_token()
    print("Got ARM token.")

    # Step 3: Update connection with refresh token
    print(f"\nUpdating connection '{connection_name}'...")
    success = update_connection(
        arm_token=arm_token,
        connection_name=connection_name,
        client_id=client_id,
        client_secret=client_secret,
        audience_app_id=audience_app_id,
        mcp_endpoint=mcp_endpoint,
        refresh_token=refresh_token,
    )

    if success:
        print()
        print("=" * 60)
        print("  Consent granted successfully!")
        print("  The MCP OAuth connection can now acquire tokens.")
        print("=" * 60)
        print()
        print("Next steps:")
        print("  1. Test the agent: python scripts/test-agent.py")
        print("  2. Verify deployment: python scripts/verify_deployment.py")
    else:
        print("\nFailed to update connection.")
        sys.exit(1)


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Grant OAuth consent for the MCP connection via device code flow. "
        "One-time manual step after 'azd up' to populate the connection with a refresh token."
    ).parse_args()
    main()
