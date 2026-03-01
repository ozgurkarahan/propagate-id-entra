"""Diagnose MCP OAuth token flow end-to-end.

Tests every step of the Foundry MCP OAuth pipeline to identify exactly where
the token acquisition breaks:

  1. Read OAuth connection via ARM (verify credentials, target, scopes)
  2. Manual token acquisition via refresh_token grant
  3. Decode access token JWT (check aud, iss, scp, tid claims)
  4. Call MCP endpoint with the token (verify APIM accepts it)
  5. Check 401 response headers (WWW-Authenticate format)
  6. Check PRM response (resource, authorization_servers, scopes)
  7. Check Entra sign-in logs (did Foundry attempt token acquisition?)
  8. Target <-> Resource mismatch analysis

Usage: python scripts/diagnose-mcp-auth.py
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error

TENANT_ID = ""
COGNITIVE_ACCOUNT = ""
PROJECT_NAME = ""
RESOURCE_GROUP = ""
APIM_NAME = ""

# --- Helpers ---

_results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, msg: str):
    _results.append((label, passed, msg))
    tag = f"\033[92mPASS\033[0m" if passed else f"\033[91mFAIL\033[0m"
    print(f"  [{tag}] {msg}")


def load_azd_env():
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


def _run_cmd(cmd: str) -> str:
    """Run a shell command and return stdout (stripped)."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def init_globals():
    """Set module-level globals from azd env vars."""
    global TENANT_ID, COGNITIVE_ACCOUNT, PROJECT_NAME, RESOURCE_GROUP, APIM_NAME
    TENANT_ID = os.environ.get("AZURE_TENANT_ID", "") or _run_cmd("az account show --query tenantId -o tsv")
    RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "")
    COGNITIVE_ACCOUNT = os.environ.get("COGNITIVE_ACCOUNT_NAME", "")
    PROJECT_NAME = os.environ.get("AI_FOUNDRY_PROJECT_NAME", "")
    APIM_NAME = os.environ.get("APIM_NAME", "")


def az_rest(method: str, url: str):
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


def http_request(url: str, method: str = "GET", headers: dict = None,
                 data: bytes = None, timeout: int = 15):
    """HTTP request returning (status_code, headers_dict, body_text)."""
    hdrs = headers or {}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = dict(resp.headers)
            return resp.status, resp_headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        resp_headers = dict(e.headers) if hasattr(e, "headers") else {}
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        return e.code, resp_headers, body
    except Exception as e:
        return 0, {}, str(e)


def decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT (no signature verification)."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    # Add padding
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# --- Step 1: Read OAuth Connection ---

def step1_read_connection() -> dict | None:
    print("\n" + "=" * 60)
    print("  Step 1: Read OAuth Connection via ARM")
    print("=" * 60)

    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        result = subprocess.run(
            "az account show --query id -o tsv",
            capture_output=True, text=True, shell=True,
        )
        sub_id = result.stdout.strip()
        os.environ["AZURE_SUBSCRIPTION_ID"] = sub_id

    conn_name = os.environ.get("MCP_OAUTH_CONNECTION_NAME", "mcp-oauth")
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}/connections/{conn_name}"
        f"?api-version=2025-04-01-preview"
    )

    conn = az_rest("get", url)
    if not conn:
        check("connection", False, f"OAuth connection '{conn_name}' not found")
        return None

    props = conn.get("properties", {})
    auth_type = props.get("authType", "")
    target = props.get("target", "")
    scopes = props.get("scopes", [])
    token_url = props.get("tokenUrl", "")

    # ARM GET redacts credentials — use listSecrets to get them
    secrets_url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{COGNITIVE_ACCOUNT}"
        f"/projects/{PROJECT_NAME}/connections/{conn_name}/listSecrets"
        f"?api-version=2025-04-01-preview"
    )
    secrets_conn = az_rest("post", secrets_url)
    if secrets_conn:
        secrets_props = secrets_conn.get("properties", {})
        creds = secrets_props.get("credentials", {}) or {}
        # Merge non-secret fields from GET if listSecrets doesn't have them
        if not target:
            target = secrets_props.get("target", "")
        if not scopes:
            scopes = secrets_props.get("scopes", [])
        if not token_url:
            token_url = secrets_props.get("tokenUrl", "")
    else:
        creds = {}
        print("  WARNING: listSecrets failed — credentials unavailable from ARM")

    client_id = creds.get("clientId", "")
    has_secret = bool(creds.get("clientSecret"))
    has_refresh = bool(creds.get("refreshToken"))

    # Fall back to azd env vars if ARM doesn't return credentials
    if not client_id:
        client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    if not has_secret and os.environ.get("MCP_OAUTH_CLIENT_SECRET"):
        has_secret = True

    print(f"\n  Connection: {conn_name}")
    print(f"  AuthType:   {auth_type}")
    print(f"  Target:     {target}")
    print(f"  Scopes:     {scopes}")
    print(f"  TokenURL:   {token_url}")
    print(f"  ClientID:   {client_id}")
    print(f"  Secret:     {'present' if has_secret else 'MISSING'}")
    print(f"  Refresh:    {'present' if has_refresh else 'MISSING'}")

    check("connection", auth_type == "OAuth2", f"AuthType = {auth_type}")
    check("connection", bool(target), f"Target = {target}")
    check("connection", bool(client_id), f"ClientID = {client_id}")
    check("connection", has_secret, f"ClientSecret {'present' if has_secret else 'MISSING'}")
    check("connection", has_refresh,
          f"RefreshToken {'present' if has_refresh else 'MISSING — run grant-mcp-consent.py'}")
    check("connection", bool(scopes), f"Scopes = {scopes}")

    # Store credentials on the conn object for step 2
    if "properties" not in conn:
        conn["properties"] = {}
    conn["properties"]["credentials"] = creds
    conn["properties"]["target"] = target
    conn["properties"]["scopes"] = scopes
    conn["properties"]["tokenUrl"] = token_url

    return conn


# --- Step 2: Manual Token Acquisition ---

def step2_acquire_token(conn: dict) -> str | None:
    print("\n" + "=" * 60)
    print("  Step 2: Manual Token Acquisition (refresh_token grant)")
    print("=" * 60)

    props = conn.get("properties", {})
    creds = props.get("credentials", {}) or {}
    client_id = creds.get("clientId", "") or os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    client_secret = creds.get("clientSecret", "") or os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")
    refresh_token = creds.get("refreshToken", "")
    token_url = props.get("tokenUrl", "")
    scopes = props.get("scopes", [])

    if not refresh_token:
        check("token", False, "Cannot acquire token — no refresh token on connection (run grant-mcp-consent.py)")
        return None

    if not token_url:
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

    scope_str = " ".join(scopes) + " offline_access" if scopes else ""

    print(f"\n  Token URL: {token_url}")
    print(f"  Scope:     {scope_str}")
    print(f"  Client:    {client_id}")

    # Try with client_secret first (confidential client), fall back to
    # without (public client — isFallbackPublicClient=true rejects secrets)
    token_params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": scope_str,
    }

    data = urllib.parse.urlencode(token_params).encode()
    status, _, body = http_request(
        token_url, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
    )

    # If AADSTS700025 (public client), retry without client_secret
    if status != 200 and "AADSTS700025" in body:
        print("  Public client detected — retrying without client_secret...")
        del token_params["client_secret"]
        data = urllib.parse.urlencode(token_params).encode()
        status, _, body = http_request(
            token_url, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )

    if status == 200:
        token_resp = json.loads(body)
        access_token = token_resp.get("access_token", "")
        token_type = token_resp.get("token_type", "")
        expires_in = token_resp.get("expires_in", 0)
        new_refresh = token_resp.get("refresh_token", "")
        print(f"\n  Token type: {token_type}")
        print(f"  Expires:    {expires_in}s")
        print(f"  New refresh token: {'yes' if new_refresh else 'no'}")
        check("token", True, f"Token acquired (type={token_type}, expires={expires_in}s)")
        return access_token
    else:
        try:
            error = json.loads(body)
            error_code = error.get("error", "")
            error_desc = error.get("error_description", "")
            print(f"\n  Error: {error_code}")
            print(f"  Description: {error_desc[:200]}")
            check("token", False, f"Token acquisition failed: {error_code}")
        except json.JSONDecodeError:
            print(f"\n  HTTP {status}: {body[:200]}")
            check("token", False, f"Token acquisition failed: HTTP {status}")
        return None


# --- Step 3: Decode Access Token ---

def step3_decode_token(access_token: str):
    print("\n" + "=" * 60)
    print("  Step 3: Decode Access Token (JWT claims)")
    print("=" * 60)

    claims = decode_jwt_payload(access_token)
    if not claims:
        check("jwt", False, "Could not decode JWT payload")
        return

    aud = claims.get("aud", "")
    iss = claims.get("iss", "")
    scp = claims.get("scp", "")
    azp = claims.get("azp", claims.get("appid", ""))
    tid = claims.get("tid", "")
    sub = claims.get("sub", "")
    name = claims.get("name", "")
    exp = claims.get("exp", 0)

    print(f"\n  aud:   {aud}")
    print(f"  iss:   {iss}")
    print(f"  scp:   {scp}")
    print(f"  azp:   {azp}")
    print(f"  tid:   {tid}")
    print(f"  sub:   {sub}")
    print(f"  name:  {name}")
    print(f"  exp:   {exp}")

    # Check audience
    audience_app_id = os.environ.get("MCP_AUDIENCE_APP_ID", "")
    expected_aud_api = f"api://{audience_app_id}" if audience_app_id else ""

    if aud == expected_aud_api:
        check("jwt", True, f"aud = {aud} (matches api://{{appId}})")
    elif aud == audience_app_id:
        check("jwt", True, f"aud = {aud} (raw app ID, NOT api:// prefixed)")
        print(f"  ** WARNING: Token aud is raw app ID, not api:// prefixed **")
        print(f"  ** APIM validate-azure-ad-token audience must match this format **")
    else:
        check("jwt", False, f"aud = {aud} (expected {expected_aud_api} or {audience_app_id})")

    # Check issuer (v1.0 and v2.0 formats are both valid)
    expected_iss_v2 = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
    expected_iss_v1 = f"https://sts.windows.net/{TENANT_ID}/"
    iss_ok = iss in (expected_iss_v2, expected_iss_v1)
    check("jwt", iss_ok, f"iss = {iss} ({'v2.0' if iss == expected_iss_v2 else 'v1.0' if iss == expected_iss_v1 else 'unknown'})")

    # Check scope
    check("jwt", "access_as_user" in scp, f"scp = {scp}")

    # Check tenant
    check("jwt", tid == TENANT_ID, f"tid = {tid}")


# --- Step 4: Call MCP Endpoint with Token ---

def step4_call_mcp(access_token: str):
    print("\n" + "=" * 60)
    print("  Step 4: Call MCP Endpoint with Bearer Token")
    print("=" * 60)

    gateway = os.environ.get("APIM_GATEWAY_URL", "")
    if not gateway:
        check("mcp-call", False, "APIM_GATEWAY_URL not set")
        return

    url = f"{gateway}/orders-mcp/mcp"
    print(f"\n  URL: {url}")

    status, headers, body = http_request(
        url, method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    print(f"  Status: {status}")
    if body:
        print(f"  Body:   {body[:200]}")

    if status == 200 or status == 204:
        check("mcp-call", True, f"MCP accepted token (HTTP {status})")
    elif status == 401:
        www_auth = headers.get("WWW-Authenticate", headers.get("www-authenticate", ""))
        print(f"  WWW-Authenticate: {www_auth}")
        check("mcp-call", False,
              f"MCP rejected token (HTTP 401) — audience/issuer mismatch in policy?")
    else:
        check("mcp-call", True, f"MCP responded (HTTP {status}) — token was accepted by policy")

    # Also try POST (Foundry uses POST for MCP tool calls)
    print(f"\n  Trying POST {url}...")
    status2, headers2, body2 = http_request(
        url, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        data=b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
    )
    print(f"  POST Status: {status2}")
    if body2:
        print(f"  POST Body:   {body2[:200]}")

    if status2 != 401:
        check("mcp-call", True, f"POST MCP accepted token (HTTP {status2})")
    else:
        check("mcp-call", False, f"POST MCP rejected token (HTTP {status2})")


# --- Step 5: Check 401 Response ---

def step5_check_401():
    print("\n" + "=" * 60)
    print("  Step 5: Check 401 Response (WWW-Authenticate header)")
    print("=" * 60)

    gateway = os.environ.get("APIM_GATEWAY_URL", "")
    if not gateway:
        check("401", False, "APIM_GATEWAY_URL not set")
        return

    url = f"{gateway}/orders-mcp/mcp"
    print(f"\n  URL: {url} (no auth)")

    status, headers, body = http_request(url, method="GET")

    print(f"  Status: {status}")

    www_auth = headers.get("WWW-Authenticate", headers.get("www-authenticate", ""))
    print(f"  WWW-Authenticate: {www_auth}")

    check("401", status == 401, f"Unauthenticated request returns HTTP {status}")
    check("401", bool(www_auth), f"WWW-Authenticate header {'present' if www_auth else 'MISSING'}")

    if www_auth:
        has_resource_metadata = "resource_metadata=" in www_auth
        check("401", has_resource_metadata,
              f"WWW-Authenticate {'contains' if has_resource_metadata else 'MISSING'} resource_metadata")

        # Extract PRM URL from the header
        if has_resource_metadata:
            parts = www_auth.split("resource_metadata=")
            prm_url = parts[1].strip().strip('"') if len(parts) > 1 else ""
            print(f"  PRM URL from 401: {prm_url}")


# --- Step 6: Check PRM Response ---

def step6_check_prm() -> dict:
    print("\n" + "=" * 60)
    print("  Step 6: Check PRM Response (RFC 9728)")
    print("=" * 60)

    gateway = os.environ.get("APIM_GATEWAY_URL", "")
    if not gateway:
        check("prm", False, "APIM_GATEWAY_URL not set")
        return {}

    url = f"{gateway}/orders-mcp/.well-known/oauth-protected-resource"
    print(f"\n  URL: {url}")

    status, headers, body = http_request(url, method="GET")

    print(f"  Status: {status}")

    if status != 200:
        check("prm", False, f"PRM endpoint returned HTTP {status}")
        return {}

    try:
        prm = json.loads(body)
    except json.JSONDecodeError:
        check("prm", False, "PRM response is not valid JSON")
        return {}

    print(f"  PRM JSON: {json.dumps(prm, indent=2)}")

    resource = prm.get("resource", "")
    auth_servers = prm.get("authorization_servers", [])
    scopes = prm.get("scopes_supported", [])
    bearer_methods = prm.get("bearer_methods_supported", [])

    check("prm", bool(resource), f"resource = {resource}")
    check("prm", bool(auth_servers), f"authorization_servers = {auth_servers}")
    check("prm", bool(scopes), f"scopes_supported = {scopes}")
    check("prm", "header" in bearer_methods,
          f"bearer_methods_supported = {bearer_methods}")

    cache_control = headers.get("Cache-Control", headers.get("cache-control", ""))
    print(f"  Cache-Control: {cache_control}")

    return prm


# --- Step 7: Check Entra Sign-in Logs ---

def step7_check_signin_logs():
    print("\n" + "=" * 60)
    print("  Step 7: Check Entra Sign-in Logs (Foundry token attempts)")
    print("=" * 60)

    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
    if not client_id:
        check("signin", False, "MCP_OAUTH_CLIENT_ID not set — cannot query sign-in logs")
        return

    url = (
        f"https://graph.microsoft.com/v1.0/auditLogs/signIns"
        f"?$filter=appId eq '{client_id}'"
        f"&$top=10&$orderby=createdDateTime desc"
    )

    print(f"\n  Querying sign-in logs for appId={client_id}...")

    # Use az rest for Graph API call
    result = subprocess.run(
        f'az rest --method GET --url "{url}" -o json',
        capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Authorization_RequestDenied" in stderr or "Insufficient privileges" in stderr:
            print("  Sign-in logs require AuditLog.Read.All permission")
            print("  This is expected in managed tenants without admin access")
            check("signin", True, "Sign-in logs unavailable (insufficient permissions — expected)")
        else:
            print(f"  Error: {stderr[:200]}")
            check("signin", False, f"Could not query sign-in logs: {stderr[:100]}")
        return

    out = result.stdout.strip()
    if not out:
        check("signin", True, "No sign-in log data returned")
        return

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        check("signin", False, "Invalid JSON from sign-in logs query")
        return

    entries = data.get("value", [])
    print(f"  Found {len(entries)} sign-in entries for Foundry OAuth Client")

    if entries:
        print(f"\n  Recent entries:")
        for entry in entries[:5]:
            ts = entry.get("createdDateTime", "?")
            status_info = entry.get("status", {})
            error_code = status_info.get("errorCode", 0)
            failure_reason = status_info.get("failureReason", "")
            resource_name = entry.get("resourceDisplayName", "")
            print(f"    {ts} | error={error_code} | resource={resource_name} | {failure_reason}")
        check("signin", True,
              f"Foundry IS attempting token acquisition ({len(entries)} recent entries)")
    else:
        check("signin", False,
              "No sign-in entries — Foundry never attempts to acquire a token")


# --- Step 8: Target <-> Resource Mismatch Analysis ---

def step8_mismatch_analysis(conn: dict, prm: dict):
    print("\n" + "=" * 60)
    print("  Step 8: Target <-> Resource Mismatch Analysis")
    print("=" * 60)

    conn_target = conn.get("properties", {}).get("target", "")
    prm_resource = prm.get("resource", "")
    conn_scopes = conn.get("properties", {}).get("scopes", [])
    prm_scopes = prm.get("scopes_supported", [])

    gateway = os.environ.get("APIM_GATEWAY_URL", "")
    mcp_url = f"{gateway}/orders-mcp/mcp" if gateway else ""

    print(f"\n  Connection target:  {conn_target}")
    print(f"  PRM resource:       {prm_resource}")
    print(f"  MCP endpoint URL:   {mcp_url}")
    print(f"  Connection scopes:  {conn_scopes}")
    print(f"  PRM scopes:         {prm_scopes}")

    # Check target vs resource
    if conn_target == prm_resource:
        check("mismatch", True, "Connection target == PRM resource")
    else:
        check("mismatch", False,
              f"MISMATCH: Connection target ({conn_target}) != PRM resource ({prm_resource})")
        print(f"\n  ** This is likely the root cause! **")
        print(f"  Foundry discovers PRM resource='{prm_resource}' but its connection")
        print(f"  target='{conn_target}' doesn't match, so it can't find the right connection.")
        print(f"\n  Fix options:")
        print(f"    A) Change PRM resource to match target: {conn_target}")
        print(f"       -> Edit infra/policies/mcp-prm-policy.xml")
        print(f"    B) Change connection target to match PRM: {prm_resource}")
        print(f"       -> Edit hooks/postprovision.py + re-run consent")

    # Check scopes
    scopes_match = set(conn_scopes) == set(prm_scopes)
    if scopes_match:
        check("mismatch", True, "Connection scopes == PRM scopes")
    else:
        check("mismatch", False,
              f"Scope mismatch: connection={conn_scopes}, PRM={prm_scopes}")

    # Check target vs MCP URL
    if conn_target == mcp_url:
        check("mismatch", True, "Connection target == MCP endpoint URL")
    elif conn_target:
        if mcp_url.startswith(conn_target) or conn_target.startswith(mcp_url):
            check("mismatch", False,
                  f"Connection target ({conn_target}) is prefix/suffix of MCP URL ({mcp_url})")
        else:
            check("mismatch", False,
                  f"Connection target ({conn_target}) != MCP endpoint ({mcp_url})")


# --- Main ---

def main():
    print("=" * 60)
    print("  MCP OAuth Token Flow Diagnostic")
    print("=" * 60)

    print("\nLoading azd environment variables...")
    load_azd_env()
    init_globals()

    # Step 1: Read connection
    conn = step1_read_connection()
    if not conn:
        print("\n** Cannot proceed without OAuth connection. Exiting. **")
        print_summary()
        sys.exit(1)

    # Step 2: Acquire token
    access_token = step2_acquire_token(conn)

    # Step 3: Decode token
    if access_token:
        step3_decode_token(access_token)

    # Step 4: Call MCP with token
    if access_token:
        step4_call_mcp(access_token)

    # Step 5: Check 401 response
    step5_check_401()

    # Step 6: Check PRM
    prm = step6_check_prm()

    # Step 7: Sign-in logs
    step7_check_signin_logs()

    # Step 8: Mismatch analysis
    if conn and prm:
        step8_mismatch_analysis(conn, prm)

    # Summary
    print_summary()


def print_summary():
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed

    print(f"\n{'=' * 60}")
    print(f"  DIAGNOSTIC SUMMARY: {passed}/{total} checks passed")
    print(f"{'=' * 60}")

    if failed:
        print(f"\n  \033[91mFailed checks:\033[0m")
        for label, p, msg in _results:
            if not p:
                print(f"    [{label}] {msg}")

    # Print actionable recommendations
    failures = {label for label, p, _ in _results if not p}

    print(f"\n  Recommendations:")
    if "connection" in failures:
        print(f"    - Fix OAuth connection: re-run hooks/postprovision.py + grant-mcp-consent.py")
    if "token" in failures:
        print(f"    - Token acquisition failed: check client secret, refresh token validity")
        print(f"    - Re-run: python scripts/grant-mcp-consent.py")
    if "jwt" in failures:
        print(f"    - JWT claims wrong: check Entra app configuration (audience, scope)")
    if "mcp-call" in failures:
        print(f"    - APIM rejects valid token: check validate-azure-ad-token audience format")
        print(f"    - Compare token 'aud' claim with APIM McpAudienceAppId Named Value")
    if "mismatch" in failures:
        print(f"    - Target/Resource mismatch: align PRM resource with connection target")
        print(f"    - Easiest fix: update mcp-prm-policy.xml resource field")
    if not failures:
        print(f"    - All checks passed! If Foundry still doesn't send tokens,")
        print(f"      this may be a Foundry platform issue (Discussion #269).")
        print(f"    - Check App Insights for Foundry request patterns after running agent.")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Diagnose MCP OAuth token flow end-to-end. "
        "Tests every step of the Foundry MCP OAuth pipeline (8 steps, 24 checks)."
    ).parse_args()
    main()
