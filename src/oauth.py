"""
OAuth 2.0 PKCE & Adobe IMS SSO Authentication for CX Coworker Gateway.
Supports RFC 7591 Dynamic Client Registration, PKCE S256, local callback server,
and automatic token lifecycle management (caching & refreshing).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("mcp_oauth")

GATEWAY_BASE_URL = os.environ.get("CX_GATEWAY_URL", "https://cx-coworker-gateway.adobe.io")
DEFAULT_PORT = 8765
DEFAULT_REDIRECT_URI = f"http://localhost:{DEFAULT_PORT}/callback"
CACHE_DIR = Path(__file__).resolve().parent.parent


class TokenStore:
    """Manages cached client registration and OAuth tokens on disk."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.token_file = self.cache_dir / ".token_cache.json"
        self.client_file = self.cache_dir / ".client_cache.json"

    def load_token(self) -> Optional[Dict[str, Any]]:
        if not self.token_file.exists():
            return None
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read token cache: {e}")
            return None

    def save_token(self, token_data: Dict[str, Any]) -> None:
        # Calculate absolute expires_at timestamp
        if "expires_in" in token_data and "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + float(token_data["expires_in"])
        try:
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(token_data, f, indent=2)
            os.chmod(self.token_file, 0o600)
        except Exception as e:
            logger.warning(f"Failed to save token cache: {e}")

    def clear_token(self) -> None:
        if self.token_file.exists():
            self.token_file.unlink()

    def load_client(self) -> Optional[Dict[str, Any]]:
        if not self.client_file.exists():
            return None
        try:
            with open(self.client_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save_client(self, client_data: Dict[str, Any]) -> None:
        try:
            with open(self.client_file, "w", encoding="utf-8") as f:
                json.dump(client_data, f, indent=2)
            os.chmod(self.client_file, 0o600)
        except Exception as e:
            logger.warning(f"Failed to save client cache: {e}")


def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """Decode JWT payload without verifying signature (for identity inspect)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode())
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return None


def generate_pkce_pair() -> Tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).decode().rstrip("=")
    sha256_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(sha256_hash).decode().rstrip("=")
    return code_verifier, code_challenge


class CallbackServer:
    """Local HTTP Server that captures the OAuth redirect callback."""

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error: Optional[str] = None
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[Thread] = None

    def start(self, expected_state: str) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass  # Suppress default HTTP logging

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not Found")
                    return

                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]
                error_desc = params.get("error_description", ["Unknown error"])[0]

                if error:
                    parent.error = f"{error}: {error_desc}"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        self._render_html(
                            title="Adobe SSO Authentication Failed",
                            header="❌ 인증 오류 (Authentication Error)",
                            message=f"<p style='color: #d9383a;'><b>오류:</b> {parent.error}</p>",
                            is_error=True,
                        ).encode()
                    )
                    return

                if state != expected_state:
                    parent.error = "State mismatch (possible CSRF attack)"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        self._render_html(
                            title="CSRF State Mismatch",
                            header="❌ 보안 상태 불일치",
                            message="<p style='color: #d9383a;'>요청 상태 토큰이 일치하지 않습니다.</p>",
                            is_error=True,
                        ).encode()
                    )
                    return

                parent.code = code
                parent.state = state

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    self._render_html(
                        title="Adobe SSO Login Successful",
                        header="✅ Adobe IMS SSO 인증 성공",
                        message="<p>인증이 완료되었습니다. <b>이 브라우저 창을 닫고 터미널로 돌아가세요.</b></p>"
                        "<p style='color: #666; font-size: 13px;'>CX Coworker Gateway와 연결이 곧 시작됩니다.</p>",
                        is_error=False,
                    ).encode()
                )

            def _render_html(self, title: str, header: str, message: str, is_error: bool) -> str:
                color = "#d9383a" if is_error else "#207e4c"
                return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f4f5f8;
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100vh;
      margin: 0;
    }}
    .card {{
      background: #ffffff;
      padding: 36px 48px;
      border-radius: 12px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.08);
      max-width: 500px;
      text-align: center;
    }}
    h1 {{
      color: {color};
      font-size: 22px;
      margin-bottom: 16px;
    }}
    p {{
      color: #333333;
      font-size: 15px;
      line-height: 1.6;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 12px;
      background: #f0f0f0;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      color: #555;
      margin-top: 12px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">Adobe CX Coworker Gateway</div>
    <h1>{header}</h1>
    {message}
  </div>
</body>
</html>"""

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def wait_for_code(self, timeout: int = 180) -> str:
        start = time.time()
        while time.time() - start < timeout:
            if self.error:
                self.stop()
                raise RuntimeError(f"OAuth Error: {self.error}")
            if self.code:
                self.stop()
                return self.code
            time.sleep(0.5)
        self.stop()
        raise TimeoutError("Adobe SSO login timed out (180s elapsed).")

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None


class AdobeSSOOAuthClient:
    """Handles OAuth 2.0 PKCE flow with CX Coworker Gateway and Adobe IMS."""

    def __init__(
        self,
        base_url: str = GATEWAY_BASE_URL,
        port: int = DEFAULT_PORT,
        client_name: str = "demo-mcp-client",
    ):
        self.base_url = base_url.rstrip("/")
        self.port = port
        self.redirect_uri = f"http://localhost:{port}/callback"
        self.client_name = client_name
        self.store = TokenStore()

    def get_or_register_client(self) -> str:
        """Register client dynamically with RFC 7591 or use cached client_id."""
        cached = self.store.load_client()
        if cached and cached.get("client_id"):
            return cached["client_id"]

        reg_url = f"{self.base_url}/register"
        payload = {
            "client_name": self.client_name,
            "redirect_uris": [self.redirect_uri],
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(reg_url, json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Dynamic client registration failed ({resp.status_code}): {resp.text}"
                )
            data = resp.json()
            self.store.save_client(data)
            return data["client_id"]

    def login_interactive(self, auto_open_browser: bool = True) -> Dict[str, Any]:
        """Perform interactive Adobe IMS SSO login via browser."""
        client_id = self.get_or_register_client()
        code_verifier, code_challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(16)

        server = CallbackServer(port=self.port)
        server.start(expected_state=state)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{self.base_url}/authorize?" + urllib.parse.urlencode(params)

        print("\n" + "=" * 70)
        print("🔐 Adobe IMS SSO Authentication Required")
        print("=" * 70)
        print(f"Opening browser for Adobe SSO Sign-In...\n")
        print(f"👉 If your browser does not open automatically, visit this URL:\n\n{auth_url}\n")
        print("=" * 70)

        if auto_open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception as e:
                print(f"(Note: Could not open browser automatically: {e})")

        print("⏳ Waiting for browser SSO completion on localhost callback...")
        code = server.wait_for_code(timeout=180)
        print("✅ Received authorization code! Exchanging for tokens...")

        token_data = self._exchange_code_for_token(client_id, code, code_verifier)
        self.store.save_token(token_data)
        print("🎉 Successfully acquired Adobe IMS tokens from CX Coworker Gateway!")
        return token_data

    def _exchange_code_for_token(
        self, client_id: str, code: str, code_verifier: str
    ) -> Dict[str, Any]:
        token_url = f"{self.base_url}/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(token_url, data=data)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Token exchange failed ({resp.status_code}): {resp.text}"
                )
            return resp.json()

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        client_id = self.get_or_register_client()
        token_url = f"{self.base_url}/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(token_url, data=data)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Token refresh failed ({resp.status_code}): {resp.text}"
                )
            refreshed = resp.json()
            if "refresh_token" not in refreshed:
                refreshed["refresh_token"] = refresh_token
            self.store.save_token(refreshed)
            return refreshed

    def get_valid_token(self, interactive: bool = True) -> str:
        """Get a valid access token: env var -> cache -> refresh -> interactive SSO."""
        # 1. Environment variable override
        env_token = os.environ.get("ADOBE_IMS_TOKEN")
        if env_token:
            return env_token

        # 2. Check token cache
        cached = self.store.load_token()
        if cached and "access_token" in cached:
            expires_at = cached.get("expires_at", 0)
            # Check if token is still valid (with 60-second grace buffer)
            if time.time() < (expires_at - 60):
                return cached["access_token"]

            # Try refresh
            if "refresh_token" in cached:
                try:
                    refreshed = self.refresh_token(cached["refresh_token"])
                    return refreshed["access_token"]
                except Exception as e:
                    logger.info(f"Token refresh failed, will re-authenticate: {e}")

        # 3. Interactive SSO
        if interactive:
            tokens = self.login_interactive()
            return tokens["access_token"]

        raise RuntimeError("No valid Adobe token found and interactive login is disabled.")
