"""
Lightweight OAuth2 Authorization Code flow for MCP.
ChatGPT connector requires: /oauth/authorize, /oauth/token endpoints.
mcp-remote requires: /register (RFC 7591 dynamic client registration).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Dict, Optional
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

# In-memory stores (fine for single-instance server)
_auth_codes: Dict[str, dict] = {}   # code -> {client_id, redirect_uri, expires}
_tokens: Dict[str, dict] = {}       # token -> {client_id, expires}
_dynamic_clients: Dict[str, dict] = {}  # client_id -> {secret, redirect_uris}

TOKEN_TTL = 3600 * 24 * 30  # 30 days
CODE_TTL = 300               # 5 minutes


def _load_clients() -> Dict[str, dict]:
    base = {
        os.getenv("OAUTH_CLIENT_ID", "chatgpt-mcp-client"): {
            "secret": os.getenv("OAUTH_CLIENT_SECRET", "change-this-secret"),
            "redirect_uris": [
                u.strip()
                for u in os.getenv(
                    "OAUTH_REDIRECT_URIS",
                    "https://chatgpt.com/aip/mcp/oauth/callback",
                ).split(",")
                if u.strip()
            ],
        }
    }
    return {**base, **_dynamic_clients}


def _verify_client(client_id: str, redirect_uri: str) -> None:
    clients = _load_clients()
    client = clients.get(client_id)
    if not client:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown client_id.")
    # localhost her zaman izinli (mcp-remote random port kullanir)
    if redirect_uri and redirect_uri.startswith("http://localhost"):
        return
    if redirect_uri and redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"redirect_uri not allowed: {redirect_uri}")


def authorize_get(request: Request) -> HTMLResponse:
    """GET /oauth/authorize — show login/approve page."""
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    scope = request.query_params.get("scope", "")
    resource = request.query_params.get("resource", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")

    try:
        _verify_client(client_id, redirect_uri)
    except HTTPException as e:
        return HTMLResponse(f"<h2>Error</h2><p>{e.detail}</p>", status_code=400)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cloud MCP – Authorize</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f0f0f; color: #f0f0f0; display: flex;
            align-items: center; justify-content: center; min-height: 100vh; }}
    .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 16px;
             padding: 40px 36px; max-width: 420px; width: 100%; text-align: center; }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
    p {{ color: #aaa; font-size: 14px; margin-bottom: 28px; line-height: 1.5; }}
    .client {{ background: #252525; border-radius: 8px; padding: 10px 14px;
               font-size: 13px; color: #ccc; margin-bottom: 24px; word-break: break-all; }}
    input[type=password] {{ width: 100%; padding: 12px 16px; border-radius: 10px;
                            border: 1px solid #444; background: #252525; color: #fff;
                            font-size: 15px; margin-bottom: 16px; outline: none; }}
    input[type=password]:focus {{ border-color: #666; }}
    button {{ width: 100%; padding: 13px; border-radius: 10px; border: none;
              background: #2d7aff; color: #fff; font-size: 16px; font-weight: 600;
              cursor: pointer; transition: background .2s; }}
    button:hover {{ background: #1a66e8; }}
    .err {{ color: #ff6b6b; font-size: 13px; margin-bottom: 12px; display: none; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔐</div>
    <h1>Cloud MCP</h1>
    <p>The following application is requesting access:</p>
    <div class="client">{client_id}</div>
    <form method="POST" action="{base_url}/oauth/authorize">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="scope" value="{scope}">
      <input type="hidden" name="resource" value="{resource}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <input type="password" name="password" placeholder="Master password" autofocus>
      <div class="err" id="err">Wrong password.</div>
      <button type="submit">Authorize and Connect</button>
    </form>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


def authorize_post(request: Request) -> RedirectResponse:
    """POST /oauth/authorize — validate password, issue code."""
    return RedirectResponse("/oauth/authorize", status_code=303)


async def authorize_post_handler(request: Request) -> RedirectResponse:
    form = await request.form()
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    scope = str(form.get("scope", ""))
    resource = str(form.get("resource", ""))
    password = str(form.get("password", ""))

    code_challenge = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", ""))
    master = os.getenv("OAUTH_MASTER_PASSWORD", os.getenv("OAUTH_CLIENT_SECRET", ""))
    if password != master:
        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "error": "1",
        })
        return RedirectResponse(f"{base_url}/oauth/authorize?{params}", status_code=303)

    try:
        _verify_client(client_id, redirect_uri)
    except HTTPException:
        return RedirectResponse(f"{redirect_uri}?error=access_denied&state={state}", status_code=303)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "expires": time.time() + CODE_TTL,
        "scope": scope,
        "resource": resource,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    params = urlencode({"code": code, "state": state})
    return RedirectResponse(f"{redirect_uri}?{params}", status_code=303)


async def token_handler(request: Request) -> JSONResponse:
    """POST /oauth/token — exchange code for token."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")
    code = body.get("code", "")
    redirect_uri = body.get("redirect_uri", "")
    scope = body.get("scope", "")
    resource = body.get("resource", "")
    code_verifier = body.get("code_verifier", "")

    clients = _load_clients()
    client = clients.get(client_id)
    if not client or client["secret"] != client_secret:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        entry = _auth_codes.pop(code, None)
        if not entry or entry["expires"] < time.time() or entry["client_id"] != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # PKCE (RFC 7636) validation
        stored_challenge = entry.get("code_challenge", "")
        stored_method = entry.get("code_challenge_method", "")
        if stored_challenge:
            if not code_verifier:
                return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier required"}, status_code=400)
            if stored_method == "S256":
                import base64 as _b64
                digest = __import__("hashlib").sha256(code_verifier.encode()).digest()
                computed = _b64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if computed != stored_challenge:
                    return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier mismatch"}, status_code=400)
            elif stored_method == "plain":
                if code_verifier != stored_challenge:
                    return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier mismatch"}, status_code=400)

        # RFC 8707 (resource indicators): if client asked for a resource, bind it to the code/token.
        code_resource = entry.get("resource", "")
        if resource and code_resource and resource != code_resource:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        token = secrets.token_urlsafe(48)
        _tokens[token] = {
            "client_id": client_id,
            "expires": time.time() + TOKEN_TTL,
            "resource": (resource or code_resource),
        }

        resp = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
        }
        # Echo back scope/resource for stricter clients (Claude connector).
        code_scope = entry.get("scope", "")
        if scope or code_scope:
            resp["scope"] = scope or code_scope
        if resource or code_resource:
            resp["resource"] = resource or code_resource
        return JSONResponse(resp)

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def register_client_handler(request: Request) -> JSONResponse:
    """POST /register — RFC 7591 dynamic client registration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)
    redirect_uris = body.get("redirect_uris", [])

    _dynamic_clients[client_id] = {
        "secret": client_secret,
        "redirect_uris": redirect_uris,
    }

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


def verify_token(authorization: Optional[str]) -> str:
    """Returns client_id if token valid, raises 401 otherwise."""
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Authorization header.")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Authorization header.")
    token = parts[1]
    entry = _tokens.get(token)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.")
    return entry["client_id"]
