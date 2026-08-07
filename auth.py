"""
auth.py — OAuth for the Robinhood MCP endpoint.

The daemon needs its own grant. A Claude connector authorizes Claude, not a
process running unattended on your VPS, so gexbot registers itself as an OAuth
client and holds its own tokens.

Two modes:

    login   (interactive, once)  browser flow -> tokens on disk
    daemon  (unattended, always) reads those tokens, refreshes them silently

The MCP SDK implements the protocol — discovery, dynamic client registration,
PKCE, refresh. This module supplies the two things it can't know: where to
persist credentials, and how to get a human in front of a browser.

Headless VPS flow: run `--login` ON the VPS and forward the callback port from
your laptop.

    ssh -L 8788:127.0.0.1:8788 root@your-vps
    /opt/gexbot/venv/bin/python /opt/gexbot/gexbot.py --login

The authorize URL gets printed; open it in your laptop browser. The redirect
comes back to localhost:8788, down the tunnel, into the waiting server on the
VPS. Tokens are written on the VPS and never travel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from aiohttp import web
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (AuthorizationCodeResult, OAuthClientInformationFull,
                             OAuthClientMetadata, OAuthToken)

log = logging.getLogger("gexbot.auth")

CALLBACK_PORT = int(os.environ.get("GEXBOT_OAUTH_PORT", "8788"))
CALLBACK_PATH = "/callback"
CLIENT_NAME = "gexbot"

# Robinhood does not publish its MCP scope list. Leaving this unset lets the
# server apply its default grant, which is what the connector flow does too.
# Override if you learn the exact scopes.
SCOPE = os.environ.get("GEXBOT_OAUTH_SCOPE") or None


class NeedsLogin(RuntimeError):
    """No usable credentials. A human has to authorize once."""


def redirect_uri(port: int = CALLBACK_PORT) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def client_metadata(port: int = CALLBACK_PORT) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[redirect_uri(port)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        scope=SCOPE,
    )


# ─────────────────────────── storage ───────────────────────────


class FileTokenStorage(TokenStorage):
    """
    Tokens and registered-client info on disk, 0600.

    Keyed by a hash of the server URL so pointing at a different MCP endpoint
    doesn't silently reuse a grant issued for another one.

    A refresh token here is a bearer credential for a brokerage account. It is
    written with restrictive permissions, lives under GEXBOT_HOME (which
    .gitignore excludes), and the systemd unit keeps that directory as the
    process's only writable path.
    """

    def __init__(self, home: Path, server_url: str):
        slug = hashlib.sha256(server_url.encode()).hexdigest()[:12]
        self.dir = home / "oauth"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.tokens_file = self.dir / f"{slug}.tokens.json"
        self.client_file = self.dir / f"{slug}.client.json"
        self.server_url = server_url

    @staticmethod
    def _write(path: Path, payload: str) -> None:
        # Write-then-rename so a crash mid-write can't leave a truncated
        # credential file that reads as "not logged in".
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload)
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("%s unreadable (%s) — treating as absent", path.name, e)
            return None

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read(self.tokens_file)
        if not raw:
            return None
        raw.pop("obtained_at", None)          # our bookkeeping, not the model's
        try:
            return OAuthToken(**raw)
        except Exception as e:
            log.warning("stored tokens rejected (%s) — re-login required", e)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        payload = tokens.model_dump(exclude_none=True)
        payload["obtained_at"] = int(time.time())
        self._write(self.tokens_file, json.dumps(payload, indent=2))
        log.info("credentials saved to %s", self.tokens_file)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read(self.client_file)
        if not raw:
            return None
        try:
            return OAuthClientInformationFull(**raw)
        except Exception as e:
            log.warning("stored client registration rejected (%s)", e)
            return None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._write(self.client_file, json.dumps(info.model_dump(exclude_none=True,
                                                                mode="json"), indent=2))

    # ── inspection, for the dashboard and `--auth-status` ──

    def status(self) -> dict:
        raw = self._read(self.tokens_file)
        if not raw:
            return {"logged_in": False, "detail": "no credentials — run --login"}
        obtained = raw.get("obtained_at")
        expires_in = raw.get("expires_in")
        out = {"logged_in": True, "has_refresh": bool(raw.get("refresh_token")),
               "detail": "ok"}
        if obtained and expires_in:
            left = int(obtained + expires_in - time.time())
            out["expires_in_s"] = left
            if left <= 0:
                # Not fatal: the refresh token is what keeps the daemon alive.
                out["detail"] = ("access token expired — refreshes on next call"
                                 if raw.get("refresh_token")
                                 else "expired and no refresh token — run --login")
        return out


# ─────────────────────────── the browser flow ───────────────────────────


class CallbackServer:
    """
    Catches the redirect. Bound to loopback only — the authorization code is
    single-use and PKCE-bound, but it is still a credential in a URL.
    """

    def __init__(self, port: int = CALLBACK_PORT):
        self.port = port
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()

        async def handle(request: web.Request) -> web.Response:
            q = request.query
            if "error" in q:
                msg = q.get("error_description") or q["error"]
                if not self.result.done():
                    self.result.set_exception(RuntimeError(f"authorization denied: {msg}"))
                return web.Response(status=400, text=f"Authorization failed: {msg}",
                                    content_type="text/plain")
            code = q.get("code")
            if not code:
                return web.Response(status=400, text="no code in callback",
                                    content_type="text/plain")
            if not self.result.done():
                self.result.set_result(AuthorizationCodeResult(
                    code=code, state=q.get("state"), iss=q.get("iss")))
            return web.Response(
                text="gexbot is authorized. You can close this tab and return "
                     "to the terminal.",
                content_type="text/plain")

        app.router.add_get(CALLBACK_PATH, handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def wait(self, timeout: int = 300) -> AuthorizationCodeResult:
        try:
            return await asyncio.wait_for(self.result, timeout)
        except asyncio.TimeoutError:
            raise NeedsLogin(
                f"no callback within {timeout}s. If you are on a VPS, check the "
                f"tunnel: ssh -L {self.port}:127.0.0.1:{self.port} root@your-vps"
            ) from None

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()


def _print_authorize_url(url: str) -> None:
    print("\n" + "=" * 72)
    print("Open this URL in a browser and approve access:\n")
    print(f"  {url}\n")
    print(f"The redirect returns to {redirect_uri()} — if you are on a VPS,")
    print("that must be forwarded from the machine running the browser:")
    print(f"  ssh -L {CALLBACK_PORT}:127.0.0.1:{CALLBACK_PORT} root@your-vps")
    print("=" * 72 + "\n", flush=True)


def build_provider(server_url: str, home: Path, *, interactive: bool,
                   callback: "CallbackServer | None" = None) -> OAuthClientProvider:
    """
    interactive=True  -> may prompt a human (used by --login)
    interactive=False -> silent; refreshes if it can, otherwise raises NeedsLogin

    The daemon must never block on a browser prompt. Under systemd that would
    hang the process indefinitely with no way to answer it.
    """
    storage = FileTokenStorage(home, server_url)

    async def redirect_handler(url: str) -> None:
        if not interactive:
            raise NeedsLogin(
                "the broker requires authorization and this process cannot "
                "prompt. Run:  gexbot.py --login"
            )
        _print_authorize_url(url)

    async def callback_handler() -> AuthorizationCodeResult:
        if not interactive or callback is None:
            raise NeedsLogin("no interactive callback available — run --login")
        return await callback.wait()

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata(callback.port if callback else CALLBACK_PORT),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def storage_for(server_url: str, home: Path) -> FileTokenStorage:
    return FileTokenStorage(home, server_url)


def logout(server_url: str, home: Path) -> None:
    st = FileTokenStorage(home, server_url)
    for f in (st.tokens_file, st.client_file):
        if f.exists():
            f.unlink()
            print(f"removed {f}")
