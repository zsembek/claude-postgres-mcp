"""
PostgreSQL MCP Server with OAuth 2.1 (PKCE)
===========================================
Transport : SSE (legacy) + StreamableHTTP /mcp (MCP 2025-03-26)
Auth      : OAuth 2.1 + PKCE — built-in authorization server
Spec      : MCP 2025-03-26
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import jwt
import mcp.types as types
from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from pydantic_settings import BaseSettings

# StreamableHTTP transport (MCP 2025-03-26, preferred by Claude Desktop)
try:
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    _HAS_HTTP_TRANSPORT = True
except ImportError:
    _HAS_HTTP_TRANSPORT = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pg-mcp")


# ── Configuration ──────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    database_url: str
    jwt_secret: str = secrets.token_urlsafe(32)
    server_url: str = "http://localhost:8000"
    admin_token: str = secrets.token_urlsafe(32)
    max_rows: int = 1000
    port: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()


# ── In-memory stores ───────────────────────────────────────────────────────────

_auth_codes: dict[str, dict] = {}    # code → {client_id, redirect_uri, code_challenge, expires}
_refresh_tokens: dict[str, dict] = {}  # token → {client_id, issued_at}
_db_pool: Optional[asyncpg.Pool] = None


# ── JWT helpers ────────────────────────────────────────────────────────────────

def create_access_token(client_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": client_id, "iat": now, "exp": now + 3600, "scope": "mcp:read"},
        settings.jwt_secret,
        algorithm="HS256",
    )

def verify_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


# ── PKCE helper ────────────────────────────────────────────────────────────────

def verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))

_AUTHORIZE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PostgreSQL MCP — Authorize</title>
  <style>
    *{{box-sizing:border-box}}
    body{{font-family:system-ui,sans-serif;max-width:440px;margin:80px auto;padding:0 24px;color:#111}}
    h1{{font-size:1.3rem;margin-bottom:6px}}
    p{{color:#555;font-size:.9rem;line-height:1.5}}
    label{{display:block;font-size:.85rem;font-weight:600;margin-bottom:6px;margin-top:20px}}
    input[type=password]{{width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:1rem}}
    input[type=password]:focus{{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}}
    button{{margin-top:18px;width:100%;padding:12px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer}}
    button:hover{{background:#1d4ed8}}
    .err{{color:#dc2626;font-size:.85rem;margin-top:8px}}
    .badge{{display:inline-block;background:#f0fdf4;color:#166534;border:1px solid #bbf7d0;border-radius:6px;padding:2px 8px;font-size:.8rem;margin-bottom:16px}}
  </style>
</head>
<body>
  <span class="badge">Read-only access</span>
  <h1>Authorize PostgreSQL MCP</h1>
  <p>Claude Code is requesting read-only access to your PostgreSQL database.
     Enter the <strong>admin token</strong> shown in your server logs to approve.</p>
  <form method="POST">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <label for="tok">Admin Token</label>
    <input type="password" id="tok" name="admin_token" placeholder="Paste token here" autofocus>
    {error}
    <button type="submit">Authorize Access</button>
  </form>
</body>
</html>"""


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_pool
    _db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    log.info("✓ Database pool ready")
    log.info("Admin token: %s", settings.admin_token)
    yield
    await _db_pool.close()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="PostgreSQL MCP Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id"],
)


# ── OAuth metadata ─────────────────────────────────────────────────────────────

@app.get("/.well-known/oauth-authorization-server")
async def oauth_as_metadata():
    base = settings.server_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": ["mcp:read"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }

@app.get("/.well-known/oauth-protected-resource")
async def oauth_resource_metadata():
    base = settings.server_url.rstrip("/")
    return {
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp:read"],
        "bearer_methods_supported": ["header"],
    }


# ── Dynamic client registration ────────────────────────────────────────────────

@app.post("/oauth/register")
async def oauth_register(request: Request):
    body = await request.json()
    log.info("DCR request body: %s", body)
    client_id = f"client_{secrets.token_urlsafe(12)}"
    now = int(time.time())
    response = {
        "client_id": client_id,
        "client_id_issued_at": now,
        "client_secret_expires_at": 0,
        "client_name": body.get("client_name", "Claude"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
        "scope": body.get("scope", "mcp:read"),
        "application_type": body.get("application_type", "web"),
    }
    log.info("DCR response: %s", response)
    return JSONResponse(response)


# ── Authorization endpoint ─────────────────────────────────────────────────────

@app.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    response_type: str = Query("code"),
):
    if response_type != "code" or code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    return HTMLResponse(_AUTHORIZE_HTML.format(
        state=_esc(state), client_id=_esc(client_id),
        redirect_uri=_esc(redirect_uri), code_challenge=_esc(code_challenge),
        error="",
    ))

@app.post("/oauth/authorize", response_class=HTMLResponse)
async def authorize_post(
    state: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    admin_token: str = Form(...),
):
    if not secrets.compare_digest(admin_token.encode(), settings.admin_token.encode()):
        return HTMLResponse(_AUTHORIZE_HTML.format(
            state=_esc(state), client_id=_esc(client_id),
            redirect_uri=_esc(redirect_uri), code_challenge=_esc(code_challenge),
            error='<p class="err">Invalid admin token — please try again.</p>',
        ), status_code=403)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "expires": time.time() + 600,
    }
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


# ── Token endpoint ─────────────────────────────────────────────────────────────

@app.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    if grant_type == "authorization_code":
        if not code or code not in _auth_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        info = _auth_codes.pop(code)
        if time.time() > info["expires"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)
        if not code_verifier or not verify_pkce(code_verifier, info["code_challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        if redirect_uri and redirect_uri != info["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        rt = secrets.token_urlsafe(32)
        _refresh_tokens[rt] = {"client_id": info["client_id"], "issued_at": time.time()}
        return {
            "access_token": create_access_token(info["client_id"]),
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": rt,
        }

    if grant_type == "refresh_token":
        if not refresh_token or refresh_token not in _refresh_tokens:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        info = _refresh_tokens[refresh_token]
        if time.time() - info["issued_at"] > 86400 * 30:
            del _refresh_tokens[refresh_token]
            return JSONResponse({"error": "invalid_grant", "error_description": "Refresh token expired"}, status_code=400)
        return {
            "access_token": create_access_token(info["client_id"]),
            "token_type": "bearer",
            "expires_in": 3600,
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ── MCP Server ─────────────────────────────────────────────────────────────────

mcp_server = Server("pg-mcp")

@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="execute_query",
            description="Execute a read-only SQL SELECT/WITH query against PostgreSQL. Returns tab-separated rows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "SQL SELECT query"},
                    "params": {
                        "type": "array",
                        "items": {},
                        "description": "Positional parameters for $1, $2, …",
                        "default": [],
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="list_tables",
            description="List all tables in a PostgreSQL schema.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "description": "Schema name", "default": "public"},
                },
            },
        ),
        types.Tool(
            name="describe_table",
            description="Show columns, types, nullability and defaults for a table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name"},
                    "schema": {"type": "string", "description": "Schema name", "default": "public"},
                },
                "required": ["table"],
            },
        ),
        types.Tool(
            name="list_schemas",
            description="List all user-defined schemas in the PostgreSQL database.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if _db_pool is None:
        return [types.TextContent(type="text", text="Error: database not connected")]
    try:
        if name == "execute_query":
            return await _execute_query(arguments)
        if name == "list_tables":
            return await _list_tables(arguments)
        if name == "describe_table":
            return await _describe_table(arguments)
        if name == "list_schemas":
            return await _list_schemas()
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        log.exception("Tool %s failed", name)
        return [types.TextContent(type="text", text=f"Error: {exc}")]


# ── Tool implementations ───────────────────────────────────────────────────────

_BLOCKED = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|EXECUTE|COPY|GRANT|REVOKE)\b',
    re.IGNORECASE,
)

async def _execute_query(args: dict) -> list[types.TextContent]:
    query = args.get("query", "").strip()
    params = args.get("params", [])

    upper = query.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return [types.TextContent(type="text", text="Error: only SELECT / WITH queries are permitted")]

    if _BLOCKED.search(query):
        return [types.TextContent(type="text", text="Error: query contains a disallowed keyword")]

    async with _db_pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(query, *params, timeout=30)

    if not rows:
        return [types.TextContent(type="text", text="Query returned 0 rows.")]

    display = rows[: settings.max_rows]
    cols = list(rows[0].keys())
    lines = ["\t".join(cols)]
    lines += ["\t".join("" if r[c] is None else str(r[c]) for c in cols) for r in display]
    if len(rows) > settings.max_rows:
        lines.append(f"\n(Showing {settings.max_rows} of {len(rows)} rows)")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _list_tables(args: dict) -> list[types.TextContent]:
    schema = args.get("schema", "public")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_schema = $1 ORDER BY table_name",
            schema,
        )
    if not rows:
        return [types.TextContent(type="text", text=f"No tables found in schema '{schema}'")]
    lines = [f"{r['table_name']}  ({r['table_type']})" for r in rows]
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _describe_table(args: dict) -> list[types.TextContent]:
    table = args.get("table", "")
    schema = args.get("schema", "public")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 "
            "ORDER BY ordinal_position",
            schema, table,
        )
    if not rows:
        return [types.TextContent(type="text", text=f"Table '{schema}.{table}' not found")]
    lines = [f"Table: {schema}.{table}\n", f"{'Column':<30} {'Type':<22} {'Nullable':<10} Default"]
    lines.append("-" * 82)
    for r in rows:
        dtype = r["data_type"]
        if r["character_maximum_length"]:
            dtype += f"({r['character_maximum_length']})"
        lines.append(f"{r['column_name']:<30} {dtype:<22} {r['is_nullable']:<10} {r['column_default'] or ''}")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _list_schemas() -> list[types.TextContent]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema','pg_catalog','pg_toast') "
            "ORDER BY schema_name"
        )
    return [types.TextContent(type="text", text="\n".join(r["schema_name"] for r in rows) or "No schemas found")]


# ── Transports ────────────────────────────────────────────────────────────────

sse_transport = SseServerTransport("/messages/")

def _bearer_valid(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        verify_token(auth[7:])
        return True
    except Exception:
        return False

def _unauthorized() -> Response:
    base = settings.server_url.rstrip("/")
    # MCP 2025-03-26 / RFC 9728: resource_metadata MUST be inside WWW-Authenticate
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{base}", '
                f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
            ),
        },
    )

@app.get("/sse")
async def sse_endpoint(request: Request):
    if not _bearer_valid(request):
        return _unauthorized()
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())

@app.post("/messages/")
async def messages_endpoint(request: Request):
    if not _bearer_valid(request):
        return _unauthorized()
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ── StreamableHTTP transport (MCP 2025-03-26) ─────────────────────────────────

@app.api_route("/v2/mcp", methods=["GET", "POST", "DELETE"])
@app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_http_endpoint(request: Request):
    if not _bearer_valid(request):
        return _unauthorized()
    if not _HAS_HTTP_TRANSPORT:
        return JSONResponse({"error": "StreamableHTTP not available"}, status_code=501)
    transport = StreamableHTTPServerTransport(mcp_session_id=secrets.token_hex(16))
    async with transport.connect(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    db_ok = _db_pool is not None and not _db_pool._closed
    return {"status": "ok" if db_ok else "degraded", "db_pool": db_ok}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, log_level="info")
