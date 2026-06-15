"""Google OAuth authentication with an explicit email whitelist.

- Sessions are signed cookies (Starlette SessionMiddleware).
- Whitelist is configured via `AUTH_ALLOWED_EMAILS` (comma-separated env var).
- A user not on the whitelist gets a 403 page that doesn't reveal what's behind.
- `/mcp/*` is gated separately by Bearer-token (`MCP_API_KEY`) — Hermes Agent
  uses that, not the cookie session.
- `/static/*`, `/favicon.ico`, `/auth/*` are public.

If `auth_enabled=False` (no OAuth credentials configured), all routes are open
— useful for local dev / before you've set up the Google Cloud project.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import Settings


# Routes that NEVER require authentication.
PUBLIC_PREFIXES = (
    "/auth/",            # login/callback/logout
    "/static/",          # CSS/JS/images
    "/favicon.ico",
    "/favicon.svg",
)
# Routes that require a separate Bearer-token (MCP_API_KEY), not the cookie session.
MCP_PREFIXES = ("/mcp/", "/mcp")


def build_oauth(settings: Settings) -> OAuth | None:
    """Construct the Authlib OAuth client for Google. Returns None if not configured."""
    if not settings.auth_enabled:
        return None
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


def _is_mcp(path: str) -> bool:
    return any(path.startswith(p) for p in MCP_PREFIXES)


def current_user(request: Request) -> dict[str, Any] | None:
    """Return the authenticated user dict from session, or None."""
    user = request.session.get("user") if hasattr(request, "session") else None
    if isinstance(user, dict) and user.get("email"):
        return user
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind a Google OAuth session, except public + MCP paths.

    Layout:
      - /static/*, /auth/*, /favicon.* → always allowed
      - /mcp/* → require Authorization: Bearer <MCP_API_KEY> (if key configured)
      - everything else → require session cookie with whitelisted email
      - if auth_enabled is False (no Google client configured) → open access
    """

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public routes — always allowed.
        if _is_public(path):
            return await call_next(request)

        # MCP routes — Bearer token only (Hermes Agent doesn't carry cookies).
        if _is_mcp(path):
            if self.settings.mcp_api_key:
                auth_header = request.headers.get("authorization", "")
                if not auth_header.startswith("Bearer "):
                    return JSONResponse({"error": "missing bearer token"}, status_code=401)
                token = auth_header.removeprefix("Bearer ").strip()
                if not secrets.compare_digest(token, self.settings.mcp_api_key):
                    return JSONResponse({"error": "invalid bearer token"}, status_code=401)
            return await call_next(request)

        # Auth disabled (no Google client configured) — allow everything.
        if not self.settings.auth_enabled:
            return await call_next(request)

        # Cookie session check.
        user = current_user(request)
        if user is None or not _email_allowed(user.get("email"), self.settings):
            # HTML clients → redirect to login. API clients → 401 JSON.
            if path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"error": "authentication required", "login": "/auth/login"}, status_code=401)
            # Carry the requested URL so we can bounce back after login.
            qs = urlencode({"next": request.url.path or "/"})
            return RedirectResponse(f"/auth/login?{qs}", status_code=302)

        return await call_next(request)


def _email_allowed(email: str | None, settings: Settings) -> bool:
    if not email:
        return False
    return email.strip().lower() in settings.auth_allowed_emails


# ── Routes (registered from app/main.py) ───────────────────────────────────

def register_auth_routes(app, settings: Settings, oauth: OAuth | None) -> None:
    """Register /auth/login, /auth/callback, /auth/logout, /auth/me."""

    @app.get("/auth/login", include_in_schema=False)
    async def login(request: Request):
        if oauth is None:
            return HTMLResponse(_unconfigured_html(), status_code=503)
        # The redirect URI must EXACTLY match a URI registered in Google Cloud
        # Console for this OAuth client. Two ways to land at the right value:
        #   1. set AUTH_REDIRECT_URI in .env
        #   2. let Starlette derive it from the inbound request URL
        if settings.auth_redirect_uri:
            redirect_uri = settings.auth_redirect_uri
        else:
            redirect_uri = str(request.url_for("auth_callback"))
        # Round-trip through Cloudflare proxies sometimes strips the scheme;
        # force https if we're behind a tunnel.
        if request.headers.get("x-forwarded-proto") == "https" and redirect_uri.startswith("http://"):
            redirect_uri = "https://" + redirect_uri[len("http://"):]
        next_path = request.query_params.get("next") or "/dashboard"
        request.session["next"] = next_path
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback", include_in_schema=False, name="auth_callback")
    async def auth_callback(request: Request):
        if oauth is None:
            return HTMLResponse(_unconfigured_html(), status_code=503)
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as exc:  # invalid_grant, state mismatch, etc.
            return HTMLResponse(_error_html(f"OAuth failed: {exc}"), status_code=400)
        userinfo = token.get("userinfo") or {}
        email = (userinfo.get("email") or "").lower()
        if not _email_allowed(email, settings):
            return HTMLResponse(
                _denied_html(email),
                status_code=403,
            )
        request.session["user"] = {
            "email": email,
            "name": userinfo.get("name"),
            "picture": userinfo.get("picture"),
        }
        next_path = request.session.pop("next", "/dashboard")
        # Restrict redirect target to local paths (prevent open-redirect).
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/dashboard"
        return RedirectResponse(next_path, status_code=302)

    @app.post("/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/auth/login", status_code=302)

    @app.get("/auth/logout", include_in_schema=False)
    async def logout_get(request: Request):
        return await logout(request)

    @app.get("/auth/me", include_in_schema=False)
    async def me(request: Request):
        user = current_user(request)
        if user is None:
            return JSONResponse({"authenticated": False}, status_code=401)
        return {"authenticated": True, "email": user["email"], "name": user.get("name")}


def _unconfigured_html() -> str:
    return """<!doctype html><meta charset=utf-8><title>Auth not configured</title>
<style>body{font:14px/1.6 system-ui;max-width:520px;margin:80px auto;padding:0 24px;color:#111}</style>
<h1>Google login not configured</h1>
<p>This dashboard requires <code>GOOGLE_CLIENT_ID</code>, <code>GOOGLE_CLIENT_SECRET</code>,
and <code>AUTH_SESSION_SECRET</code> in <code>.env</code>. See <code>.env.example</code>.</p>
"""


def _denied_html(email: str) -> str:
    return f"""<!doctype html><meta charset=utf-8><title>Not authorized</title>
<style>body{{font:14px/1.6 system-ui;max-width:520px;margin:80px auto;padding:0 24px;color:#111}}
.muted{{color:#666}}</style>
<h1>Not authorized</h1>
<p>The Google account <strong>{email or '(unknown)'}</strong> is not on the access list for this dashboard.</p>
<p class=muted>If this is a mistake, ask the operator to add your email to <code>AUTH_ALLOWED_EMAILS</code>.</p>
<p><a href="/auth/logout">Sign out</a></p>
"""


def _error_html(message: str) -> str:
    return f"""<!doctype html><meta charset=utf-8><title>Login failed</title>
<style>body{{font:14px/1.6 system-ui;max-width:520px;margin:80px auto;padding:0 24px;color:#111}}</style>
<h1>Login failed</h1>
<p>{message}</p>
<p><a href="/auth/login">Try again</a></p>
"""
