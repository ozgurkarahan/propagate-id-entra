"""Entra ID sign-in log viewer for Identity Propagation PoC.

Queries Microsoft Graph API `auditLogs/signIns` to show token issuance
events for the Chat App SPA Entra app registration.

Requirements:
  - `az login` with a user that has `AuditLog.Read.All` delegated permission
  - azd environment configured (loads app IDs from azd env vars)

Usage:
  python scripts/check-signin-logs.py
  python scripts/check-signin-logs.py --hours 48
  python scripts/check-signin-logs.py --app-filter all --hours 72
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ─── AADSTS Error Code Mapping ──────────────────────────────────────────────

AADSTS_ERRORS = {
    "0": "Success",
    "50076": "MFA required — user did not complete MFA",
    "50079": "MFA registration required",
    "50105": "User not assigned to application",
    "50126": "Invalid username or password",
    "50140": "Keep Me Signed In interrupt",
    "50158": "External security challenge not satisfied",
    "53003": "Conditional Access — blocked by policy",
    "530003": "Conditional Access — blocked by policy",
    "65001": "User or admin has not consented to use the application",
    "650057": "Invalid resource — app has not been granted required permissions",
    "70011": "Invalid scope — app has not been granted required permissions",
    "700016": "Application not found in tenant",
    "700025": "Client is public — cannot use client secret",
    "7000218": "Request body must contain client_assertion or client_secret",
}

# ─── Colors ──────────────────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def color_status(code: int) -> str:
    if code == 0:
        return f"{GREEN}Success{RESET}"
    else:
        desc = AADSTS_ERRORS.get(str(code), "")
        label = f"AADSTS{code}"
        if desc:
            label += f" ({desc})"
        return f"{RED}{label}{RESET}"


def color_ca(status: str) -> str:
    if status == "success":
        return f"{GREEN}{status}{RESET}"
    elif status == "failure":
        return f"{RED}{status}{RESET}"
    elif status == "notApplied":
        return f"{DIM}{status}{RESET}"
    return status


# ─── Helpers ─────────────────────────────────────────────────────────────────

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


def graph_get(url: str):
    """Call Microsoft Graph API via az rest."""
    cmd = f'az rest --method get --url "{url}" -o json'
    result = subprocess.run(
        cmd, capture_output=True, text=True, shell=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if result.returncode != 0:
        return None, result.stderr
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "Invalid JSON response"


def get_app_ids() -> dict:
    """Return mapping of filter name to (app_id, display_name)."""
    apps = {}
    chat_id = os.environ.get("CHAT_APP_ENTRA_CLIENT_ID", "")

    if chat_id:
        apps["chat-spa"] = (chat_id, "Chat App SPA")

    return apps


def format_timestamp(ts: str) -> str:
    """Format ISO timestamp to readable local form."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        return ts or "?"


def truncate(s: str, length: int) -> str:
    if len(s) <= length:
        return s
    return s[:length - 3] + "..."


# ─── Main Query ──────────────────────────────────────────────────────────────

def query_signin_logs(app_id: str, app_name: str, hours: int) -> list:
    """Query Graph auditLogs/signIns for a specific appId."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        "https://graph.microsoft.com/v1.0/auditLogs/signIns"
        f"?$filter=appId eq '{app_id}' and createdDateTime ge {start_iso}"
        "&$top=50"
        "&$orderby=createdDateTime desc"
        "&$select=createdDateTime,userDisplayName,userPrincipalName,"
        "appDisplayName,resourceDisplayName,status,ipAddress,"
        "conditionalAccessStatus,location,clientAppUsed"
    )

    data, err = graph_get(url)
    if err:
        if "Authorization_RequestDenied" in str(err) or "Insufficient privileges" in str(err):
            print(f"  {YELLOW}PERMISSION DENIED{RESET} — AuditLog.Read.All required for sign-in logs")
            print(f"  {DIM}Current az login identity may lack this permission.{RESET}")
            print(f"  {DIM}Ask your admin to grant AuditLog.Read.All (delegated) or use a privileged account.{RESET}")
            return []
        if "InvalidAuthenticationToken" in str(err):
            print(f"  {RED}AUTH ERROR{RESET} — az login token is expired or invalid. Run 'az login' again.")
            return []
        print(f"  {RED}ERROR{RESET} querying sign-in logs for {app_name}: {err[:200]}")
        return []

    if not data:
        print(f"  {YELLOW}No data{RESET} returned for {app_name}")
        return []

    return data.get("value", [])


def print_signin_table(events: list, app_name: str):
    """Print formatted table of sign-in events."""
    if not events:
        print(f"  {DIM}No sign-in events found for {app_name}{RESET}")
        return

    print(f"\n  {BOLD}{app_name}{RESET} — {len(events)} event(s):")
    print(f"  {'-' * 110}")
    print(f"  {'Timestamp':<24} {'User':<25} {'Resource':<25} {'Status':<12} {'IP':<16} {'CA Status'}")
    print(f"  {'-' * 110}")

    for event in events:
        ts = format_timestamp(event.get("createdDateTime", ""))
        user = truncate(event.get("userDisplayName", "") or event.get("userPrincipalName", "?"), 24)
        resource = truncate(event.get("resourceDisplayName", "?"), 24)
        status_obj = event.get("status", {})
        error_code = status_obj.get("errorCode", 0)
        status_str = color_status(error_code)
        ip = event.get("ipAddress", "?") or "?"
        ca = color_ca(event.get("conditionalAccessStatus", "?") or "?")

        print(f"  {ts:<24} {user:<25} {resource:<25} {status_str:<35} {ip:<16} {ca}")

    print(f"  {'─' * 110}")


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Entra ID sign-in logs for the Identity Propagation PoC app registrations."
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="How many hours back to query (default: 24)"
    )
    parser.add_argument(
        "--app-filter", choices=["chat-spa", "all"],
        default="all",
        help="Which app registration to filter (default: all)"
    )
    args = parser.parse_args()

    print(f"{BOLD}Entra ID Sign-in Log Viewer{RESET}")
    print(f"{'=' * 40}")

    print("Loading azd environment variables...")
    load_azd_env()

    apps = get_app_ids()
    if not apps:
        print(f"{RED}ERROR:{RESET} No app IDs found in azd environment.")
        print("  Set CHAT_APP_ENTRA_CLIENT_ID")
        print("  Run: azd env get-values | grep CHAT_APP")
        sys.exit(1)

    # Filter apps
    if args.app_filter != "all":
        if args.app_filter in apps:
            apps = {args.app_filter: apps[args.app_filter]}
        else:
            print(f"{YELLOW}WARNING:{RESET} App filter '{args.app_filter}' not found in azd env.")
            print(f"  Available: {', '.join(apps.keys())}")
            sys.exit(1)

    print(f"\nQuerying sign-in logs for the last {CYAN}{args.hours}h{RESET} across {len(apps)} app(s)...")

    total_events = 0
    for filter_name, (app_id, display_name) in apps.items():
        print(f"\n{CYAN}[{filter_name}]{RESET} {display_name} (appId: {app_id[:8]}...)")
        events = query_signin_logs(app_id, display_name, args.hours)
        print_signin_table(events, display_name)
        total_events += len(events)

    # Summary
    print(f"\n{'=' * 40}")
    print(f"Total: {BOLD}{total_events}{RESET} sign-in event(s) across {len(apps)} app(s) in the last {args.hours}h")

    if total_events == 0:
        print(f"\n{DIM}No events found. This could mean:{RESET}")
        print(f"  {DIM}1. No sign-in activity in the time window{RESET}")
        print(f"  {DIM}2. Entra sign-in logs have a ~15 minute ingestion delay{RESET}")
        print(f"  {DIM}3. Try --hours 48 for a wider window{RESET}")


if __name__ == "__main__":
    main()
