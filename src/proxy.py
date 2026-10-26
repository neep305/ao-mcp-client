"""
Stdio-to-HTTP MCP Proxy Server for Claude Desktop.
Reads JSON-RPC from stdin, forwards to CX Coworker Gateway with automatic
Adobe IMS token management and session propagation, and outputs JSON-RPC to stdout.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root is in sys.path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.client import CXCoworkerMCPClient, MCPError
from src.oauth import AdobeSSOOAuthClient, TokenStore

# Configure logging to a file in cache dir so it doesn't pollute stdout (critical for stdio MCP)
log_file = PROJECT_ROOT / "proxy.log"
logging.basicConfig(
    filename=str(log_file),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mcp_proxy")


class StdioProxyServer:
    def __init__(
        self,
        gateway_url: str = "https://cx-coworker-gateway.adobe.io",
        manifest_id: str = "public_cx_enterprise",
        server_id: Optional[str] = None,
    ):
        self.gateway_url = gateway_url
        self.manifest_id = manifest_id
        self.server_id = server_id
        self.oauth = AdobeSSOOAuthClient(base_url=gateway_url)
        self.client: Optional[CXCoworkerMCPClient] = None

    def _ensure_client(self) -> CXCoworkerMCPClient:
        # Get or refresh token
        token = self.oauth.get_valid_token(interactive=True)
        if self.client is None or self.client.token != token:
            if self.client:
                self.client.close()
            self.client = CXCoworkerMCPClient(
                token=token,
                base_url=self.gateway_url,
                manifest_id=self.manifest_id,
                server_id=self.server_id,
            )
        return self.client

    def run(self):
        logger.info("Starting CX Coworker Gateway Stdio Proxy...")

        # Unbuffer stdout
        sys.stdout.reconfigure(line_buffering=True)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                logger.error(f"Malformed JSON from stdin: {e}")
                continue

            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params")

            # Notification (no id)
            is_notification = "id" not in request

            logger.info(f"Received method: {method} (id={req_id})")

            try:
                client = self._ensure_client()

                if method == "initialize":
                    # Perform handshake
                    res = client.initialize()
                    self._send_response(req_id, result=res)
                elif method == "notifications/initialized":
                    # Ignore or forward
                    pass
                elif method == "tools/list":
                    tools = client.list_tools(cursor=params.get("cursor") if params else None)
                    self._send_response(req_id, result={"tools": tools})
                elif method == "resources/list":
                    resources = client.list_resources()
                    self._send_response(req_id, result={"resources": resources})
                elif method == "prompts/list":
                    prompts = client.list_prompts()
                    self._send_response(req_id, result={"prompts": prompts})
                elif method == "tools/call":
                    tool_name = params.get("name") if params else ""
                    arguments = params.get("arguments", {}) if params else {}
                    res = client.call_tool(tool_name, arguments)
                    self._send_response(req_id, result=res)
                elif method == "ping":
                    self._send_response(req_id, result={})
                else:
                    # Forward any other method directly
                    res = client._send_rpc(method, params, is_notification=is_notification)
                    if not is_notification:
                        self._send_response(req_id, result=res or {})

            except MCPError as e:
                logger.error(f"MCPError during {method}: {e}")
                if not is_notification:
                    self._send_error(req_id, code=e.code, message=e.message, data=e.data)
            except Exception as e:
                logger.exception(f"Unexpected error during {method}: {e}")
                if not is_notification:
                    self._send_error(req_id, code=-32603, message=str(e))

    def _send_response(self, req_id: Any, result: Any):
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }
        json_str = json.dumps(response, ensure_ascii=False)
        sys.stdout.write(json_str + "\n")
        sys.stdout.flush()

    def _send_error(self, req_id: Any, code: int, message: str, data: Any = None):
        err_obj: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err_obj["data"] = data
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": err_obj,
        }
        json_str = json.dumps(response, ensure_ascii=False)
        sys.stdout.write(json_str + "\n")
        sys.stdout.flush()


def main():
    proxy = StdioProxyServer()
    proxy.run()


if __name__ == "__main__":
    main()
