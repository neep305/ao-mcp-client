"""
Streamable HTTP MCP Client for Adobe CX Coworker Gateway.
Implements the Model Context Protocol (MCP) over Streamable HTTP / JSON-RPC 2.0
with session header propagation and support for Wide & Server-scoped routes.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("mcp_client")


class MCPError(Exception):
    """Exception raised when an MCP server returns a JSON-RPC error or HTTP error."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"MCP Error ({code}): {message}")
        self.code = code
        self.message = message
        self.data = data


class CXCoworkerMCPClient:
    """Client for CX Coworker Gateway supporting Wide Route and Server-scoped Route."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://cx-coworker-gateway.adobe.io",
        server_id: Optional[str] = None,
        manifest_id: Optional[str] = "public_cx_enterprise",
        custom_endpoint: Optional[str] = None,
        client_name: str = "demo-mcp-client",
        client_version: str = "1.0.0",
        timeout: float = 30.0,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.server_id = server_id
        self.manifest_id = manifest_id
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._request_id = 0

        if custom_endpoint:
            self.endpoint = custom_endpoint
        elif server_id:
            # Server-scoped route (e.g. /mcp/server/aep)
            self.endpoint = f"{self.base_url}/mcp/server/{server_id}"
        else:
            # Wide route (aggregated catalog)
            query = f"?manifest_id={manifest_id}" if manifest_id else ""
            self.endpoint = f"{self.base_url}/mcp{query}"

        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _send_rpc(
        self, method: str, params: Optional[Dict[str, Any]] = None, is_notification: bool = False
    ) -> Optional[Dict[str, Any]]:
        headers = self._build_headers()
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if not is_notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params

        response = self._http.post(self.endpoint, headers=headers, json=payload)

        # Capture session ID from response headers if present
        for key in ("mcp-session-id", "Mcp-Session-Id"):
            if key in response.headers:
                self.session_id = response.headers[key]
                logger.debug(f"Captured MCP session ID: {self.session_id}")

        if response.status_code == 401:
            raise MCPError(401, f"Unauthorized: Invalid or expired Adobe token. ({response.text})")
        if response.status_code == 403:
            raise MCPError(403, f"Forbidden: Insufficient entitlement or permissions. ({response.text})")
        if response.status_code == 429:
            raise MCPError(429, "Rate limit exceeded (300/min org, 150/min user).")
        if response.status_code >= 400:
            raise MCPError(response.status_code, f"HTTP Error: {response.text}")

        if is_notification or not response.content:
            return None

        # Handle text/event-stream or application/json responses
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # Parse last SSE event containing JSON-RPC
            lines = response.text.split("\n")
            data_str = ""
            for line in lines:
                if line.startswith("data: "):
                    data_str = line[6:]
            if data_str:
                res_json = json.loads(data_str)
            else:
                return None
        else:
            res_json = response.json()

        if "error" in res_json and res_json["error"]:
            err = res_json["error"]
            raise MCPError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown MCP error"),
                data=err.get("data"),
            )

        return res_json.get("result")

    def initialize(self) -> Dict[str, Any]:
        """Perform MCP initialize handshake."""
        params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts": {"listChanged": True},
            },
            "clientInfo": {
                "name": self.client_name,
                "version": self.client_version,
            },
        }
        result = self._send_rpc("initialize", params) or {}
        # Send initialized notification per protocol spec
        self._send_rpc("notifications/initialized", is_notification=True)
        return result

    def list_tools(self, cursor: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all available tools from the Gateway or server-scoped endpoint."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        result = self._send_rpc("tools/list", params) or {}
        return result.get("tools", [])

    def list_resources(self) -> List[Dict[str, Any]]:
        """Fetch available MCP resources from the Gateway."""
        try:
            result = self._send_rpc("resources/list", {}) or {}
            return result.get("resources", [])
        except MCPError as e:
            logger.debug(f"resources/list not supported or empty: {e}")
            return []

    def list_prompts(self) -> List[Dict[str, Any]]:
        """Fetch available MCP prompts from the Gateway."""
        try:
            result = self._send_rpc("prompts/list", {}) or {}
            return result.get("prompts", [])
        except MCPError as e:
            logger.debug(f"prompts/list not supported or empty: {e}")
            return []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a specific MCP tool through the Gateway."""
        params = {
            "name": name,
            "arguments": arguments or {},
        }
        result = self._send_rpc("tools/call", params)
        return result
