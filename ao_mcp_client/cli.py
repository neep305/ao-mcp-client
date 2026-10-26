"""
Command Line Interface for CX Coworker Gateway MCP Client.
Provides verification tests, interactive SSO login, and tool inspection.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure parent directory is in path when running standalone
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from ao_mcp_client.client import CXCoworkerMCPClient, MCPError
    from ao_mcp_client.oauth import AdobeSSOOAuthClient, TokenStore, decode_jwt_payload
except ImportError:
    try:
        from src.client import CXCoworkerMCPClient, MCPError
        from src.oauth import AdobeSSOOAuthClient, TokenStore, decode_jwt_payload
    except ImportError:
        from client import CXCoworkerMCPClient, MCPError
        from oauth import AdobeSSOOAuthClient, TokenStore, decode_jwt_payload


def print_banner():
    banner = """
======================================================================
  🚀 Adobe CX Coworker Gateway - MCP Client & Verifier
======================================================================
"""
    print(banner)


def categorize_tools(tools: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    categories: Dict[str, List[Dict[str, Any]]] = {
        "AEP (Adobe Experience Platform)": [],
        "Adobe Target": [],
        "Adobe Analytics": [],
        "CJA (Customer Journey Analytics)": [],
        "AJO (Adobe Journey Optimizer)": [],
        "Marketo": [],
        "Platform Core / Gateway": [],
        "Other Tools": [],
    }

    for t in tools:
        name = t.get("name", "").lower()
        desc = t.get("description", "").lower()

        if any(k in name or k in desc for k in ["aep", "dataset", "schema", "xdm", "profile", "identity", "segment"]):
            categories["AEP (Adobe Experience Platform)"].append(t)
        elif "target" in name or "target" in desc:
            categories["Adobe Target"].append(t)
        elif "analytics" in name or "reportsuite" in name or "rsid" in name or "adobe-analytics" in name:
            categories["Adobe Analytics"].append(t)
        elif "cja" in name or "data view" in desc or "dataview" in name:
            categories["CJA (Customer Journey Analytics)"].append(t)
        elif "ajo" in name or "journey" in name or "campaign" in desc:
            categories["AJO (Adobe Journey Optimizer)"].append(t)
        elif "marketo" in name or "lead" in desc:
            categories["Marketo"].append(t)
        elif any(k in name for k in ["api_request", "discovery", "catalog", "ping", "search"]):
            categories["Platform Core / Gateway"].append(t)
        else:
            categories["Other Tools"].append(t)

    return categories


def cmd_status(args):
    """Show current authentication status."""
    store = TokenStore()
    token_data = store.load_token()

    print("\n📋 Token Status:")
    if not token_data:
        print("  ❌ No cached token found. Run 'login' or 'verify' to authenticate with Adobe SSO.")
        return

    access_token = token_data.get("access_token", "")
    expires_at = token_data.get("expires_at", 0)
    now = datetime.datetime.now().timestamp()
    remaining = expires_at - now

    print(f"  • Token Present: ✅ Yes (length: {len(access_token)})")
    if remaining > 0:
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        exp_time = datetime.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  • Token Validity: ✅ Valid for {mins}m {secs}s (Expires at {exp_time})")
    else:
        print(f"  • Token Validity: ⚠️ Expired ({abs(int(remaining))}s ago). Will refresh or re-login.")

    # Inspect claims
    claims = decode_jwt_payload(access_token)
    if claims:
        print("\n👤 User & Org Claims (from JWT):")
        for key in ["email", "preferred_username", "name", "sub", "account_type", "asuid"]:
            if key in claims:
                print(f"  • {key}: {claims[key]}")
        if "org" in claims or "org_id" in claims:
            print(f"  • org: {claims.get('org') or claims.get('org_id')}")


def cmd_login(args):
    """Trigger Adobe IMS SSO login via browser."""
    oauth = AdobeSSOOAuthClient(base_url=args.gateway_url, port=args.port)
    print("Initiating Adobe IMS SSO login flow...")
    token_data = oauth.login_interactive(auto_open_browser=not args.no_browser)
    cmd_status(args)


def cmd_logout(args):
    """Clear cached tokens."""
    store = TokenStore()
    store.clear_token()
    print("✅ Cleared cached tokens from disk.")


def cmd_verify(args):
    """Complete end-to-end verification of CX Coworker Gateway and AEP/Solution connectivity."""
    print_banner()

    # Step 1: OAuth Authentication
    print("STEP 1: Authenticating with Adobe IMS...")
    oauth = AdobeSSOOAuthClient(base_url=args.gateway_url, port=args.port)
    try:
        token = oauth.get_valid_token(interactive=not args.non_interactive)
        print("✅ OAuth Authentication: PASSED")
    except Exception as e:
        print(f"❌ OAuth Authentication Failed: {e}")
        return 1

    # Show User info
    claims = decode_jwt_payload(token)
    if claims:
        user_id = claims.get("email") or claims.get("preferred_username") or claims.get("sub", "Unknown")
        print(f"   👤 Authenticated Identity: {user_id}")

    # Step 2: Wide Route Connection (public_cx_enterprise)
    print("\nSTEP 2: Initializing MCP Handshake with CX Coworker Gateway (Wide Route)...")
    endpoint = f"{args.gateway_url}/mcp?manifest_id={args.manifest_id}"
    print(f"   Connecting to: {endpoint}")

    try:
        with CXCoworkerMCPClient(token=token, base_url=args.gateway_url, manifest_id=args.manifest_id) as client:
            init_res = client.initialize()
            print("✅ MCP Initialize Handshake: PASSED")
            print(f"   • Protocol Version: {init_res.get('protocolVersion', 'default')}")
            server_info = init_res.get("serverInfo", {})
            print(f"   • Server Name: {server_info.get('name', 'CX Coworker Gateway')}")
            print(f"   • Server Version: {server_info.get('version', 'unknown')}")
            if client.session_id:
                print(f"   • Session ID: {client.session_id}")

            # Step 3: Fetching Tools List
            print("\nSTEP 3: Fetching Available Tools from CX Coworker Gateway...")
            tools = client.list_tools()
            print(f"✅ Tools List Received: {len(tools)} tools available")

            # Categorize tools
            categorized = categorize_tools(tools)
            print("\n📊 Adobe Solutions Integration Summary:")
            for cat, tool_list in categorized.items():
                if tool_list:
                    print(f"  • {cat}: {len(tool_list)} tools")
                    for t in tool_list[:5]:
                        print(f"      - {t.get('name')}: {t.get('description', '')[:70]}...")
                    if len(tool_list) > 5:
                        print(f"      ... and {len(tool_list) - 5} more")

            # Step 4: Check Resources and Prompts
            print("\nSTEP 4: Checking MCP Resources & Prompts...")
            resources = client.list_resources()
            print(f"   • Resources available: {len(resources)}")
            prompts = client.list_prompts()
            print(f"   • Prompts available: {len(prompts)}")

    except MCPError as e:
        print(f"❌ MCP Operation Failed: {e}")
        return 1
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")
        return 1

    # Step 5: Test Server-Scoped Route (/mcp/server/aep)
    if not args.skip_server_route:
        print("\nSTEP 5: Testing Server-Scoped Route for AEP (/mcp/server/aep)...")
        try:
            with CXCoworkerMCPClient(token=token, base_url=args.gateway_url, server_id="aep") as aep_client:
                aep_init = aep_client.initialize()
                print("✅ AEP Server-Scoped Handshake: PASSED")
                aep_tools = aep_client.list_tools()
                print(f"   • AEP Direct Tools: {len(aep_tools)} tools returned directly")
        except MCPError as e:
            print(f"ℹ️ AEP Server-Scoped Route Note: {e}")
        except Exception as e:
            print(f"ℹ️ AEP Server-Scoped Route Note: {e}")

    print("\n" + "=" * 70)
    print("🎉 VERIFICATION SUMMARY:")
    print("  1. Adobe IMS SSO Authentication: ✅ SUCCESS")
    print("  2. CX Coworker Gateway MCP Connectivity: ✅ SUCCESS")
    print(f"  3. Total Adobe Tools Accessible: ✅ {len(tools)} tools")
    print("=" * 70 + "\n")
    return 0


def cmd_list_tools(args):
    """List all available tools in detail."""
    oauth = AdobeSSOOAuthClient(base_url=args.gateway_url, port=args.port)
    token = oauth.get_valid_token(interactive=True)

    with CXCoworkerMCPClient(
        token=token,
        base_url=args.gateway_url,
        server_id=args.server,
        manifest_id=args.manifest_id,
    ) as client:
        client.initialize()
        tools = client.list_tools()

        print(f"\n📦 Available Tools ({len(tools)} total):\n")
        for i, t in enumerate(tools, 1):
            name = t.get("name", "unnamed")
            desc = t.get("description", "No description provided")
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])

            print(f"{i}. {name}")
            print(f"   Description: {desc}")
            if props:
                param_strs = []
                for p_name, p_info in props.items():
                    req_star = "*" if p_name in required else ""
                    param_strs.append(f"{p_name}{req_star} ({p_info.get('type', 'any')})")
                print(f"   Parameters: {', '.join(param_strs)}")
            print()


def cmd_call_tool(args):
    """Call an MCP tool with JSON arguments."""
    oauth = AdobeSSOOAuthClient(base_url=args.gateway_url, port=args.port)
    token = oauth.get_valid_token(interactive=True)

    try:
        arguments = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in --args: {e}")
        return 1

    with CXCoworkerMCPClient(
        token=token,
        base_url=args.gateway_url,
        server_id=args.server,
        manifest_id=args.manifest_id,
    ) as client:
        client.initialize()
        # In wide route, tools/list must be called before tools/call per gateway spec
        if not args.server:
            client.list_tools()

        print(f"Calling tool '{args.name}' with arguments: {json.dumps(arguments, indent=2)}")
        try:
            result = client.call_tool(args.name, arguments)
            print("\n✅ Result:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except MCPError as e:
            print(f"❌ Tool Execution Error: {e}")
            return 1


def cmd_sync_claude(args):
    """Sync the authenticated Adobe IMS token with Claude Code MCP configuration."""
    import subprocess

    oauth = AdobeSSOOAuthClient(base_url=args.gateway_url, port=args.port)
    token = oauth.get_valid_token(interactive=True)

    endpoint = (
        f"{args.gateway_url}/mcp/server/{args.server}"
        if args.server
        else f"{args.gateway_url}/mcp?manifest_id={args.manifest_id}"
    )
    server_name = f"cx-{args.server}" if args.server else "cx-coworker-gateway"
    scope = args.scope  # local or user

    print(f"🔄 Syncing '{server_name}' to Claude Code (scope: {scope})...")

    # 1. Remove existing entry if present
    subprocess.run(["claude", "mcp", "remove", server_name, "-s", scope], capture_output=True)

    # 2. Add with Bearer token
    cmd = [
        "claude", "mcp", "add", "-s", scope, "--transport", "http",
        server_name, endpoint,
        "--header", f"Authorization: Bearer {token}"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"❌ Failed to register in Claude Code: {res.stderr}")
        return res.returncode

    print(f"✅ Successfully registered in Claude Code!")
    print(f"   • Server Name: {server_name}")
    print(f"   • Scope: {scope}")
    print(f"   • Endpoint: {endpoint}")
    print("\n🔍 Verifying Claude Code MCP status:")
    check_res = subprocess.run(["claude", "mcp", "get", server_name], capture_output=True, text=True)
    print(check_res.stdout.strip())
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Adobe CX Coworker Gateway MCP Client & Verification Tool"
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("CX_GATEWAY_URL", "https://cx-coworker-gateway.adobe.io"),
        help="CX Coworker Gateway base URL",
    )
    parser.add_argument(
        "--manifest-id",
        default="public_cx_enterprise",
        help="Manifest ID playlist for Wide Route (default: public_cx_enterprise)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local redirect callback port for OAuth (default: 8765)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # test / verify
    verify_p = subparsers.add_parser("verify", help="Run end-to-end verification")
    verify_p.add_argument("--non-interactive", action="store_true", help="Fail if no token cached")
    verify_p.add_argument("--skip-server-route", action="store_true", help="Skip /mcp/server/aep test")
    verify_p.set_defaults(func=cmd_verify)

    # alias test to verify
    test_p = subparsers.add_parser("test", help="Alias for verify")
    test_p.add_argument("--non-interactive", action="store_true")
    test_p.add_argument("--skip-server-route", action="store_true")
    test_p.set_defaults(func=cmd_verify)

    # login
    login_p = subparsers.add_parser("login", help="Authenticate with Adobe IMS SSO via browser")
    login_p.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    login_p.set_defaults(func=cmd_login)

    # status
    status_p = subparsers.add_parser("status", help="Show token status and user claims")
    status_p.set_defaults(func=cmd_status)

    # logout
    logout_p = subparsers.add_parser("logout", help="Clear cached tokens")
    logout_p.set_defaults(func=cmd_logout)

    # list-tools
    list_p = subparsers.add_parser("list-tools", help="List all available tools")
    list_p.add_argument("--server", help="Specific backend server (e.g. aep, target, cja)")
    list_p.set_defaults(func=cmd_list_tools)

    # call-tool
    call_p = subparsers.add_parser("call-tool", help="Call a specific MCP tool")
    call_p.add_argument("name", help="Tool name")
    call_p.add_argument("--args", default="{}", help="Tool input arguments in JSON string")
    call_p.add_argument("--server", help="Specific backend server (e.g. aep)")
    call_p.set_defaults(func=cmd_call_tool)

    # sync-claude
    sync_p = subparsers.add_parser("sync-claude", help="Register/sync valid token to Claude Code")
    sync_p.add_argument("-s", "--scope", choices=["local", "user"], default="local", help="Config scope (local=project, user=global)")
    sync_p.add_argument("--server", help="Optional specific backend (e.g. aep)")
    sync_p.set_defaults(func=cmd_sync_claude)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
