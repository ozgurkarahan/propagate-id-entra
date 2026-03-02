"""Interactive test: Agent MCP round-trip with tool approval.

Handles the multi-turn Responses API flow:
  Turn 1: Agent calls MCP tools -> Foundry returns mcp_approval_request
  Turn 2: User approves -> agent executes tools and returns results

Usage:
  python scripts/test-agent.py
"""

import argparse
import json
import os
import subprocess
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


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


def dump_output_items(output_items):
    """Print all output items for debugging."""
    for i, item in enumerate(output_items):
        item_type = getattr(item, "type", "unknown")
        item_id = getattr(item, "id", "")
        print(f"  [{i}] type={item_type}, id={item_id}")
        if item_type == "mcp_approval_request":
            print(f"       server={getattr(item, 'server_label', '')}")
            print(f"       tool={getattr(item, 'name', '')}")
            print(f"       args={getattr(item, 'arguments', {})}")
        elif item_type == "message":
            content = getattr(item, "content", [])
            for c in content:
                if hasattr(c, "text"):
                    print(f"       text: {c.text[:200]}")
        elif hasattr(item, "text"):
            print(f"       text: {str(getattr(item, 'text', ''))[:200]}")


def main():
    print("=" * 60)
    print("  Agent MCP Round-Trip (Orders)")
    print("=" * 60)
    print()

    load_azd_env()

    project_endpoint = os.environ.get("AI_FOUNDRY_PROJECT_ENDPOINT", "")
    if not project_endpoint:
        print("ERROR: AI_FOUNDRY_PROJECT_ENDPOINT not set. Run 'azd env get-values' to load env.")
        sys.exit(1)

    try:
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient
    except ImportError:
        print("ERROR: azure-ai-projects not installed")
        print("  pip install azure-ai-projects azure-identity")
        sys.exit(1)

    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=project_endpoint, credential=credential)
    openai_client = client.get_openai_client()

    # Find agent (may have version suffix)
    agents = list(client.agents.list())
    agent = None
    for a in agents:
        name = getattr(a, "name", "")
        if name == "orders-assistant" or name.startswith("orders-assistant-"):
            agent = a
            break

    if not agent:
        names = [getattr(a, "name", "?") for a in agents]
        print(f"ERROR: orders-assistant not found (agents: {names})")
        sys.exit(1)

    agent_name = getattr(agent, "name", "orders-assistant")
    print(f"Agent: {agent_name}")
    print(f"Endpoint: {project_endpoint}")
    print()

    # === Turn 1: Initial request ===
    print("--- Turn 1: Initial request ---")

    query_text = "List all orders. For each order, include the order ID and customer name."
    print(f"Sending: '{query_text}'")
    print("(this may take 30-60s...)")
    print()

    conversation = openai_client.conversations.create()
    response = openai_client.responses.create(
        conversation=conversation.id,
        input=query_text,
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )

    output_items = getattr(response, "output", [])
    output_types = [getattr(item, "type", "unknown") for item in output_items]
    print(f"Response ID: {response.id}")
    print(f"Output types: {output_types}")
    dump_output_items(output_items)
    print()

    # Check for MCP approval requests
    approval_items = [
        item for item in output_items
        if getattr(item, "type", "") == "mcp_approval_request"
    ]

    if approval_items:
        print(f"MCP approval requested for {len(approval_items)} tool(s):")
        for item in approval_items:
            print(f"  Tool: {getattr(item, 'name', '?')}")
            print(f"  Server: {getattr(item, 'server_label', '?')}")
            print(f"  Args: {getattr(item, 'arguments', {})}")
        print()

        confirm = input("Approve all tool calls? [Y/n] ").strip().lower()
        if confirm in ("", "y", "yes"):
            # Build approval responses
            try:
                from openai.types.responses.response_input_param import McpApprovalResponse
                approval_input = [
                    McpApprovalResponse(
                        type="mcp_approval_response",
                        approve=True,
                        approval_request_id=item.id,
                    )
                    for item in approval_items
                ]
            except ImportError:
                # Fallback: use dict
                approval_input = [
                    {
                        "type": "mcp_approval_response",
                        "approve": True,
                        "approval_request_id": item.id,
                    }
                    for item in approval_items
                ]

            print()
            print("--- Turn 2: Continue after MCP approval ---")
            print("(this may take 30-60s...)")
            print()

            response = openai_client.responses.create(
                previous_response_id=response.id,
                input=approval_input,
                extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
            )

            output_items = getattr(response, "output", [])
            output_types = [getattr(item, "type", "unknown") for item in output_items]
            print(f"Response ID: {response.id}")
            print(f"Output types: {output_types}")
            dump_output_items(output_items)
            print()
        else:
            print("Approval denied. Exiting.")
            openai_client.close()
            sys.exit(0)

    # === Final output ===
    response_text = getattr(response, "output_text", "")
    print("=" * 60)
    if response_text:
        print("  Agent Response:")
        print("=" * 60)
        print()
        print(response_text)
        print()

        markers = ["ORD-001", "ORD-008", "Alice Johnson", "Hank Brown"]
        found = [m for m in markers if m in response_text]
        print(f"Data markers: {len(found)}/{len(markers)} ({', '.join(found)})")
        if len(found) >= 3:
            print()
            print("SUCCESS: Agent MCP round-trip with identity propagation!")
        else:
            missing = [m for m in markers if m not in found]
            print(f"Missing markers: {', '.join(missing)}")
    else:
        print("  No output text in final response")
        print("=" * 60)
        print()
        print("Full output items:")
        dump_output_items(output_items)

    openai_client.close()


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Interactive test: Agent MCP round-trip. "
        "Handles multi-turn Responses API flow (approval)."
    ).parse_args()
    main()
