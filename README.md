# pg-mcp

PostgreSQL MCP server for Claude Code with OAuth 2.1 (PKCE) authorization.

**Access mode:** read-only — SELECT and WITH queries only.  
**Auth:** OAuth 2.1 Authorization Code + PKCE (built-in, no external IdP required).  
**Transport:** SSE (Server-Sent Events) over HTTP/HTTPS.

---

## Quick start

### 0. Install Docker on Ubuntu (if not installed)

```bash
# Install Docker Engine + Compose plugin in one step
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker compose version   # should print v2.x.x
```

### 1. Configure the server

```bash
cd server
cp .env.example .env
# Edit .env: set DATABASE_URL, SERVER_URL, JWT_SECRET, ADMIN_TOKEN
```

### 2. Deploy (Docker)

```bash
docker compose up -d
```

The first log line will print your admin token:

```
INFO  Admin token: <token>
```

Copy this token — you'll need it to authorize Claude Code.

### 3. Configure the plugin in Claude Code

Set the environment variable `PG_MCP_SERVER_URL` to your server's public URL:

```bash
# macOS / Linux
export PG_MCP_SERVER_URL=https://pg-mcp.yourdomain.com

# Windows PowerShell
$env:PG_MCP_SERVER_URL = "https://pg-mcp.yourdomain.com"
```

Or add it to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "env": {
    "PG_MCP_SERVER_URL": "https://pg-mcp.yourdomain.com"
  }
}
```

### 4. Install the plugin

Install the `pg-mcp.plugin` file in Claude Code (or Claude Cowork).

### 5. Authorize

When Claude Code first connects to the server it will open a browser tab to the
authorization page. Paste the admin token and click **Authorize Access**.  
Claude Code will receive a JWT and reuse it automatically until it expires.

---

## OAuth 2.1 flow (how it works)

```
Claude Code                    pg-mcp server
     │                               │
     │  GET /sse                     │
     │ ────────────────────────────► │
     │  401 WWW-Authenticate: Bearer │
     │ ◄──────────────────────────── │
     │                               │
     │  GET /.well-known/oauth-...   │  discover metadata
     │ ────────────────────────────► │
     │  200 {authorization_endpoint} │
     │ ◄──────────────────────────── │
     │                               │
     │  open browser /oauth/authorize│  user enters admin token
     │ ────────────────────────────► │
     │  302 redirect with ?code=     │
     │ ◄──────────────────────────── │
     │                               │
     │  POST /oauth/token            │  exchange code + PKCE verifier
     │ ────────────────────────────► │
     │  200 {access_token, ...}      │
     │ ◄──────────────────────────── │
     │                               │
     │  GET /sse + Bearer token      │  authenticated MCP session
     │ ────────────────────────────► │
```

---

## MCP tools

| Tool | Description |
|------|-------------|
| `execute_query` | Run a parameterised SELECT or WITH query |
| `list_tables` | List tables in a schema (default: `public`) |
| `describe_table` | Show columns, types, nullability, defaults |
| `list_schemas` | List all user-defined schemas |

---

## Production checklist

- [ ] Run behind a reverse proxy with **HTTPS** (nginx / Caddy / Traefik)
- [ ] Set `JWT_SECRET` and `ADMIN_TOKEN` to strong random values in `.env`
- [ ] Set `DATABASE_URL` to a **read-only** PostgreSQL role, not the owner
- [ ] Set `SERVER_URL` to your public HTTPS domain
- [ ] Keep the container updated (`docker compose pull && docker compose up -d`)

### Minimal nginx snippet

```nginx
server {
    listen 443 ssl;
    server_name pg-mcp.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/pg-mcp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pg-mcp.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "keep-alive";
        proxy_set_header Host $host;
        proxy_buffering off;          # required for SSE
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

---

## Security notes

- The server enforces **read-only transactions** at the PostgreSQL level — not just keyword filtering.
- SQL queries are validated against a blocklist of DML/DDL keywords as a second layer of defence.
- JWT access tokens expire after **1 hour**; refresh tokens after **30 days**.
- Auth codes expire after **10 minutes** and are single-use.
- PKCE (S256) is required — authorization codes stolen in transit cannot be exchanged.
