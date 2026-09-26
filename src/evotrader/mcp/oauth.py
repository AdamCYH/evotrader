"""Standalone OAuth handler for MCP connections.

Provides browser-based OAuth 2.0 Authorization Code flow with PKCE,
local redirect callback server, and token caching/persistence.

The callback mechanism supports two modes:
1. **Local**: A localhost HTTP server catches the browser redirect directly.
2. **Remote (Tailscale, SSH, etc.)**: The web dashboard lets the user paste
   the redirect URL, which is injected into the waiting Future via
   ``resolve_pending_callback()``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import urllib.parse
import weakref
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template
from typing import Any

import httpx
from mcp.client.auth.oauth2 import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from evotrader import paths

logger = logging.getLogger(__name__)

# ─── Module-level Future for cross-component callback resolution ────────
# The OAuth callback_handler awaits this Future.  It can be resolved by
# either the local HTTP callback server OR the web dashboard API endpoint.
_pending_callback: asyncio.Future[tuple[str, str | None]] | None = None
_pending_loop: asyncio.AbstractEventLoop | None = None
_callback_lock = asyncio.Lock()
_expected_state: str | None = None  # The state from the current auth URL

# Where the browser goes a few seconds after a successful sign-in. The web
# console sets this when it starts; without it (an offline cycle) the page says
# to return to the terminal instead.
_return_url: str | None = None
_RETURN_DELAY_SECONDS = 3


def set_return_url(url: str | None) -> None:
    """Send the browser to ``url`` after a successful sign-in (None: stay on the page)."""
    global _return_url
    _return_url = url


def dismiss_oauth_prompt(event: str = "oauth_complete", detail: str = "") -> None:
    """Clear the dashboard's authorization prompt and tell clients to drop it.

    This MUST run on every exit from an authorization flow, not just the happy
    one. ``web.server`` replays ``pending_oauth`` to every new SSE connection,
    so a prompt left set after a timeout keeps re-appearing on each dashboard
    reload — and authorizing against it is futile, because the flow behind it is
    already gone. That happened live on 2026-09-18: a flow died at 15:35, the
    system re-authenticated itself at 15:40, and the stale prompt was still
    being served at 18:20.
    """
    try:
        import evotrader.web.server as _server

        _server.pending_oauth = None
        _server.broadcast_sse_event(event, {"detail": detail} if detail else {})
    except Exception:  # the dashboard is optional; never fail a flow over it
        logger.debug("Could not dismiss the OAuth prompt in the web UI", exc_info=True)


def get_expected_state() -> str | None:
    """Return the expected OAuth state for the current auth flow."""
    return _expected_state


def resolve_pending_callback(code: str, state: str | None) -> bool | str:
    """Resolve the pending OAuth callback Future with the given code and state.

    Called by the web dashboard ``/api/auth/oauth_callback`` endpoint when
    the user pastes the redirect URL.

    Returns:
        True if resolved successfully.
        A string error message if the state doesn't match (stale URL).
        False if no pending callback exists.
    """
    global _pending_callback
    if _pending_callback is None or _pending_callback.done():
        logger.warning("resolve_pending_callback called but no pending callback Future.")
        return False

    # Validate the state matches the current auth flow
    if _expected_state and state and state != _expected_state:
        logger.warning(
            "Stale OAuth callback URL: state=%s… does not match expected=%s…",
            state[:12],
            _expected_state[:12],
        )
        return (
            "This callback URL is from a previous authorization attempt. "
            "Please click the NEW authorization link shown in the modal, "
            "authorize again, and paste the NEW redirect URL."
        )

    # The local callback server calls this from a worker thread (its
    # handle_request runs under asyncio.to_thread), and Future.set_result is not
    # thread-safe — it schedules the waiter's wakeup with call_soon, which does
    # not reliably wake a loop that is parked in its selector. Hand the result
    # back through the loop instead, which is correct from any thread.
    future, loop = _pending_callback, _pending_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(
            lambda: None if future.done() else future.set_result((code, state))
        )
    else:
        future.set_result((code, state))
    logger.info("✅  OAuth callback resolved (code=%s…)", code[:8])
    return True


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)  # os.open keeps the mode of a file that already existed


class FileTokenStorage(TokenStorage):
    """File-based token and client registration storage.

    Caches OAuth tokens and client metadata in a JSON file to avoid
    re-authenticating on every run.

    Also persists the token expiry timestamp (``_expiry_at``) so we can
    restore it on reload — the MCP SDK's ``_initialize()`` loads tokens
    from storage but does NOT restore ``token_expiry_time``, which
    causes expired access tokens to be treated as valid on restart.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = self.cache_dir / "tokens.json"
        self.client_file = self.cache_dir / "client.json"
        # Persisted expiry — separate from the token file so we don't
        # alter the standard OAuthToken schema that the SDK expects.
        self._expiry_file = self.cache_dir / "expiry.json"

        # The token can place orders on the account: only its owner may read
        # it. Files saved before this rule get tightened here too.
        self.cache_dir.chmod(0o700)
        for saved in (self.token_file, self.client_file, self._expiry_file):
            if saved.exists():
                saved.chmod(0o600)

        # Forensic log: snapshot file state at construction time
        token_exists = self.token_file.exists()
        client_exists = self.client_file.exists()
        logger.info(
            "FileTokenStorage init: cache_dir=%s, token=%s, client=%s",
            cache_dir,
            token_exists,
            client_exists,
        )

        # ── Forensic: write to a file that survives restarts ─────
        forensic_log = paths.signin_dir() / "oauth_forensic.log"

        import atexit

        token_path = self.token_file
        cache_path = self.cache_dir

        def _check_tokens_at_exit():
            import time

            exists = token_path.exists()
            dir_exists = cache_path.exists()
            dir_contents = [p.name for p in cache_path.iterdir()] if dir_exists else []
            forensic_log.parent.mkdir(parents=True, exist_ok=True)
            with open(forensic_log, "a") as f:
                f.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] ATEXIT: "
                    f"token_file exists={exists}, "
                    f"cache_dir exists={dir_exists}, "
                    f"contents={dir_contents}\n"
                )
                f.flush()

        atexit.register(_check_tokens_at_exit)

        # ── Forensic: filesystem watchdog ──────────────────────────
        if token_exists:
            self._start_fs_watchdog(forensic_log)

    def _start_fs_watchdog(self, forensic_log: Path) -> None:
        """Start a daemon thread that polls for token file deletion."""
        import threading
        import time
        import traceback

        token_path = self.token_file
        cache_path = self.cache_dir

        def _watch():
            while True:
                time.sleep(0.5)
                if not token_path.exists():
                    # Token file vanished! Dump all thread stacks.
                    dir_contents = (
                        [p.name for p in cache_path.iterdir()]
                        if cache_path.exists()
                        else "<DIR GONE>"
                    )
                    lines = [
                        f"\n{'!' * 70}",
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WATCHDOG: {token_path.name} DELETED!",
                        f"   cache_dir exists: {cache_path.exists()}",
                        f"   cache_dir contents: {dir_contents}",
                        "   ALL THREAD STACKS:",
                    ]
                    for tid, frame in sys._current_frames().items():
                        tname = "<unknown>"
                        for t in threading.enumerate():
                            if t.ident == tid:
                                tname = t.name
                                break
                        lines.append(f"\n   --- Thread {tname} (id={tid}) ---")
                        lines.append("".join(traceback.format_stack(frame)))
                    lines.append(f"{'!' * 70}\n")
                    with open(forensic_log, "a") as f:
                        f.write("\n".join(lines))
                        f.flush()
                    logger.critical("WATCHDOG: Token file deleted! See %s", forensic_log)
                    return  # Stop watching after detection

        import sys

        t = threading.Thread(target=_watch, name="oauth-token-watchdog", daemon=True)
        t.start()

    async def get_tokens(self) -> OAuthToken | None:
        logger.debug(
            "get_tokens: %s exists=%s, folder contents=%s",
            self.token_file,
            self.token_file.exists(),
            [p.name for p in self.cache_dir.iterdir()] if self.cache_dir.exists() else "<gone>",
        )
        try:
            if self.token_file.exists():
                raw = self.token_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                tokens = OAuthToken.model_validate(data)
                logger.debug(
                    "Loaded tokens: access=%s, refresh=%s, expires_in=%s",
                    bool(tokens.access_token),
                    bool(tokens.refresh_token),
                    tokens.expires_in,
                )
                logger.info(
                    "🔄  Loaded cached Robinhood OAuth credentials (reusing active session)."
                )
                return tokens
            logger.debug("No token file — the broker will ask for a fresh sign-in")
        except Exception as e:
            logger.warning("Failed to load cached OAuth tokens: %s", e)
        return None

    def get_persisted_expiry(self) -> float | None:
        """Read the persisted token expiry timestamp, if available."""
        try:
            if self._expiry_file.exists():
                data = json.loads(self._expiry_file.read_text(encoding="utf-8"))
                return data.get("expiry_at")
        except Exception:
            pass
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        import traceback

        # Never any part of the token itself: this can end up in shared logs.
        logger.debug(
            "set_tokens → %s (refresh token: %s, expires_in: %s), called from:\n%s",
            self.token_file,
            "yes" if tokens.refresh_token else "no",
            tokens.expires_in,
            "".join(traceback.format_stack()[-5:-1]),
        )

        try:
            data = tokens.model_dump(mode="json")
            _write_private(self.token_file, json.dumps(data, indent=2))
            # Verify the write succeeded
            if not self.token_file.exists():
                logger.error(
                    "Token file write FAILED — file does not exist after write: %s", self.token_file
                )
            else:
                size = self.token_file.stat().st_size
                logger.info("✅  Cached OAuth tokens to %s (%d bytes)", self.token_file, size)

            # Persist the expiry timestamp alongside the token so we can
            # restore token_expiry_time on next startup.
            import time

            expiry_at = None
            if tokens.expires_in is not None:
                expiry_at = time.time() + tokens.expires_in
            _write_private(self._expiry_file, json.dumps({"expiry_at": expiry_at}, indent=2))
        except Exception as e:
            logger.error("Failed to cache OAuth tokens: %s", e, exc_info=True)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        try:
            if self.client_file.exists():
                data = json.loads(self.client_file.read_text(encoding="utf-8"))
                return OAuthClientInformationFull.model_validate(data)
        except Exception as e:
            logger.warning("Failed to load cached client info: %s", e)
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        try:
            data = client_info.model_dump(mode="json")
            _write_private(self.client_file, json.dumps(data, indent=2))
            logger.debug("Successfully cached OAuth client info to %s", self.client_file)
        except Exception as e:
            logger.error("Failed to cache OAuth client info: %s", e)


class PersistentOAuthClientProvider(OAuthClientProvider):
    """OAuthClientProvider subclass that properly restores token expiry.

    The base SDK class's ``_initialize()`` loads tokens from storage but
    does NOT restore ``token_expiry_time``.  This means:
    - An expired access token is treated as "valid" (expiry_time is None)
    - The SDK sends the expired token, gets 401, and falls into full
      re-auth instead of trying a refresh first

    This subclass fixes that by:
    1. Restoring the persisted expiry timestamp after loading tokens
    2. If the access token is expired but a refresh token is available,
       marking the token as invalid so the SDK's refresh path kicks in
    """

    def __init__(self, *args, storage: FileTokenStorage, **kwargs):
        super().__init__(*args, storage=storage, **kwargs)
        self._file_storage = storage

    async def _initialize(self) -> None:
        """Load stored tokens, client info, AND token expiry."""
        await super()._initialize()

        # Restore the persisted expiry timestamp
        if self.context.current_tokens and isinstance(self._file_storage, FileTokenStorage):
            import time

            persisted_expiry = self._file_storage.get_persisted_expiry()
            if persisted_expiry is not None:
                self.context.token_expiry_time = persisted_expiry
                remaining = persisted_expiry - time.time()
                if remaining > 0:
                    logger.info(
                        "🔑  Restored token expiry (%.0f seconds remaining).",
                        remaining,
                    )
                else:
                    logger.info(
                        "🔑  Cached access token expired %.0f seconds ago — "
                        "will attempt refresh before re-auth.",
                        abs(remaining),
                    )
                    # Mark token as expired so the SDK tries refresh first.
                    # The refresh path is: is_token_valid() → False,
                    # can_refresh_token() → True → _refresh_token()
                    # We leave current_tokens intact (it has the refresh_token)
                    # but set a past expiry to force the refresh path.
            else:
                # No persisted expiry — token was cached before this fix.
                # Treat the access token as potentially expired so we
                # attempt a refresh rather than sending a stale token.
                if self.context.current_tokens.refresh_token:
                    logger.info(
                        "🔑  No persisted expiry — treating cached access token "
                        "as expired to force refresh."
                    )
                    self.context.token_expiry_time = 0.0  # Forces is_token_valid() → False


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP Request handler to receive the OAuth redirect callback."""

    def log_message(self, format: str, *args: Any) -> None:
        # Redirect HTTP server logging to debug logs
        logger.debug(format, *args)

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == "/callback":
            problem = _callback_problem(urllib.parse.parse_qs(parsed_path.query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_callback_page(problem, _return_url).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def _callback_problem(query: dict[str, list[str]]) -> str | None:
    """Hand the redirect's code to the waiting flow; return why not, or None.

    The page may only say the sign-in succeeded when the code actually reached a
    flow that was waiting for it. A cancelled sign-in, a link from an earlier
    attempt and a flow that already timed out all used to read as success.
    """
    code = query.get("code", [None])[0]
    if not code:
        reason = query.get("error_description", query.get("error", [None]))[0]
        if reason:
            return f"The broker did not authorize the connection ({reason})."
        return "The sign-in response carried no authorization code."
    resolved = resolve_pending_callback(code, query.get("state", [None])[0])
    if resolved is True:
        return None
    if resolved:  # a message: the link belongs to an earlier attempt
        return resolved
    return (
        "No sign-in was waiting for this response; it may have timed out. "
        "Start the connection again."
    )


_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_PAGE_TEMPLATE = _WEB_DIR / "templates" / "oauth_callback.html"
_DESIGN_TOKENS = _WEB_DIR / "static" / "css" / "tokens.css"

_ICON_OK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
)
_ICON_WARN = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M12 7.5v6"/><path d="M12 17h.01"/></svg>'
)


def render_callback_page(problem: str | None, return_url: str | None) -> str:
    """The page shown after the broker's sign-in redirect.

    ``problem`` is None on success. A success returns to ``return_url`` after a
    few seconds; a problem stays put so the message can be read. The page is
    styled with the console's own design tokens, read at render time, so it
    changes whenever the console's look does.
    """
    link = html.escape(return_url, quote=True) if return_url else None
    if problem is None:
        title, tone, icon = "Broker connected", "ok", _ICON_OK
        if link:
            refresh = f'<meta http-equiv="refresh" content="{_RETURN_DELAY_SECONDS};url={link}">'
            body = (
                "<p>Your trading account is linked. Taking you back to the console…</p>\n"
                f'        <div class="actions"><a class="btn" href="{link}">Open the console</a></div>\n'
                '        <div class="countdown" aria-hidden="true"><span></span></div>'
            )
        else:
            refresh = ""
            body = (
                "<p>Your trading account is linked.</p>\n"
                "        <p>You can close this tab and return to the terminal.</p>"
            )
    else:
        title, tone, icon, refresh = "Sign-in didn’t finish", "warn", _ICON_WARN, ""
        next_step = (
            f'<div class="actions"><a class="btn" href="{link}">Back to the console</a></div>'
            if link
            else "<p>Start the sign-in again from the terminal.</p>"
        )
        body = f"<p>{html.escape(problem)}</p>\n        {next_step}"
    try:
        tokens = _DESIGN_TOKENS.read_text(encoding="utf-8")
    except OSError:
        tokens = ""  # every rule in the template carries a fallback value
    return Template(_PAGE_TEMPLATE.read_text(encoding="utf-8")).substitute(
        refresh=refresh,
        title=html.escape(title),
        tone=tone,
        icon=icon,
        body=body,
        tokens=tokens,
        seconds=_RETURN_DELAY_SECONDS,
    )


class CallbackServer(HTTPServer):
    """The local server that receives the browser's sign-in redirect."""

    allow_reuse_address = True
    allow_reuse_port = True


async def _run_local_callback_server(port: int, future: asyncio.Future) -> None:
    """Run a localhost callback server as a fallback for local access.

    Listens on the given port until either:
    - A local HTTP redirect arrives (sets result on the Future), or
    - The Future is resolved externally (by the web dashboard).

    This runs as a background task alongside the Future await.
    """
    try:
        server = CallbackServer(("127.0.0.1", port), CallbackHandler)
        server.timeout = 0.5
    except OSError as e:
        logger.debug("Could not start local callback server on port %d: %s", port, e)
        return

    try:
        while not future.done():
            await asyncio.to_thread(server.handle_request)
    finally:
        server.server_close()
        logger.debug("Local callback server shut down.")


async def wait_for_callback(port: int, timeout: float = 300.0) -> tuple[str, str | None]:
    """Wait for the OAuth callback via either the web dashboard or localhost.

    Creates a module-level Future that can be resolved by:
    1. The localhost callback server (for local access)
    2. ``resolve_pending_callback()`` (for remote/Tailscale access via web dashboard)

    Returns:
        Tuple of (authorization_code, state).
    """
    global _pending_callback, _pending_loop

    async with _callback_lock:
        loop = asyncio.get_running_loop()
        _pending_callback = loop.create_future()
        _pending_loop = loop

        logger.info("⏳  Waiting for OAuth callback (port %d or web dashboard)...", port)
        print(f"⏳  Waiting for authorization callback on port {port} or via web dashboard...")
        print("=" * 70 + "\n")

        # Start the local callback server as a background task (best-effort)
        server_task = asyncio.create_task(_run_local_callback_server(port, _pending_callback))

        succeeded = False
        try:
            code, state = await asyncio.wait_for(_pending_callback, timeout=timeout)
            succeeded = bool(code)
        except TimeoutError:
            print("\n❌  Authorization timed out (5 minute limit reached).\n")
            raise TimeoutError("Authorization timed out. Please try again.") from None
        finally:
            _pending_callback = None
            _pending_loop = None
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
            # Take the prompt down whatever happened. Leaving it up after a
            # failure is worse than never showing it: the dashboard replays it
            # to every new connection, so the operator authorizes against a
            # flow that no longer exists and is told to restart a healthy system.
            if not succeeded:
                dismiss_oauth_prompt(
                    "oauth_failed",
                    "The authorization window closed before it was completed. "
                    "Nothing is broken — the system will ask again the next time "
                    "it needs broker access.",
                )

    if not code:
        print("\n❌  Received invalid or empty authorization code.\n")
        raise ValueError("Received invalid or empty authorization code.")

    print("\n" + "=" * 70)
    print("✨  AUTHORIZATION SUCCESSFUL!")
    print("======================================================================")
    print(f"• Cached credentials to {paths.signin_dir() / 'oauth'}/")
    print("• Initializing connection to Robinhood MCP...")
    print("=" * 70 + "\n")

    dismiss_oauth_prompt("oauth_complete")
    return code, state


_active_providers: weakref.WeakSet[OAuthClientProvider] = weakref.WeakSet()


def clear_oauth_cache(cache_dir: Path | None = None) -> None:
    """Clear cached OAuth tokens from disk and reset in-memory provider state.

    Wipes the on-disk ``oauth/`` cache in :func:`paths.signin_dir` (or the specified ``cache_dir``)
    **and** invalidates every live ``OAuthClientProvider`` so the next HTTP request triggers a
    full OAuth re-authentication flow instead of reusing stale in-memory tokens.
    """
    import shutil
    import sys
    import traceback

    # Forensic: log the call stack so we can trace unexpected clears.
    caller_stack = "".join(traceback.format_stack()[-5:-1])
    logger.warning(
        "🔑  clear_oauth_cache() CALLED — stack trace:\n%s",
        caller_stack,
    )

    if cache_dir is not None:
        oauth_dir = cache_dir
    else:
        # Safeguard: if pytest is running and no explicit cache_dir was passed,
        # never delete the real user's OAuth tokens from ~/.evotrader/oauth!
        if "pytest" in sys.modules:
            logger.warning(
                "Safeguard: skipping deletion of ~/.evotrader/oauth under pytest without explicit cache_dir"
            )
            oauth_dir = None
        else:
            oauth_dir = paths.signin_dir() / "oauth"

    if oauth_dir and oauth_dir.exists():
        # Log what we're about to delete
        try:
            contents = list(oauth_dir.rglob("*"))
            logger.warning(
                "🔑  Deleting oauth dir with %d files: %s",
                len(contents),
                [str(p.relative_to(oauth_dir)) for p in contents],
            )
        except Exception:
            pass
        try:
            shutil.rmtree(oauth_dir)
            logger.info("🔑  Cleared cached OAuth tokens from disk at %s.", oauth_dir)
        except Exception as e:
            logger.error("Failed to clear OAuth cache from disk: %s", e)

    for provider in list(_active_providers):
        try:
            # Use the SDK's own clear method which also resets token_expiry_time
            provider.context.clear_tokens()
            # Force re-initialization so _initialize() re-reads (empty) storage
            provider._initialized = False
            logger.debug("Reset in-memory OAuth provider state.")
        except Exception as e:
            logger.error("Failed to reset in-memory OAuth provider state: %s", e)


def create_oauth_httpx_factory(
    server_url: str,
    port: int = 3893,
    cache_dir: Path | None = None,
) -> Callable[..., httpx.AsyncClient]:
    """Create an httpx client factory that injects OAuth authentication.

    This binds the MCP SDK's OAuthClientProvider using custom callback and
    redirect handlers, linking the server's protocol validation flow to httpx.
    """
    if cache_dir is None:
        cache_dir = paths.signin_dir() / "oauth" / hashlib_url(server_url)

    storage = FileTokenStorage(cache_dir)

    async def redirect_handler(url: str) -> None:
        global _expected_state

        # Extract the state from the authorization URL so we can validate
        # callback URLs submitted via the web dashboard.
        parsed_auth = urllib.parse.urlparse(url)
        auth_qs = urllib.parse.parse_qs(parsed_auth.query)
        _expected_state = auth_qs.get("state", [None])[0]

        print("\n" + "=" * 70)
        print("🔑  ROBINHOOD OAUTH AUTHORIZATION REQUIRED")
        print("=" * 70)
        print("1. Authorize EvoTrader via the web dashboard or navigate to:")
        print(f"   {url}")
        print("2. Log in to your Robinhood account and approve the request.")
        print("-" * 70)
        logger.info("OAuth authorization required — broadcasting to web UI.")

        # Broadcast to connected web clients via SSE (lazy import to avoid circular deps)
        from evotrader.web.server import broadcast_sse_event

        broadcast_sse_event("oauth_required", {"url": url, "port": port})

    async def callback_handler() -> tuple[str, str | None]:
        # Wait for callback via local server OR web dashboard Future
        return await wait_for_callback(port=port)

    client_metadata = OAuthClientMetadata(
        redirect_uris=[f"http://localhost:{port}/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )

    auth_provider = PersistentOAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    _active_providers.add(auth_provider)

    def client_factory(
        headers: dict[str, Any] | None = None,
        auth: httpx.Auth | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.AsyncClient:
        # Wrap/inject our OAuthClientProvider instead of the default auth
        from evotrader.mcp.provider import RetryingAsyncTransport

        return httpx.AsyncClient(
            headers=headers,
            auth=auth_provider,
            timeout=timeout,
            follow_redirects=True,
            transport=RetryingAsyncTransport(),
        )

    return client_factory


def hashlib_url(url: str) -> str:
    """Generate a unique filesystem-safe hash for a given URL."""
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
