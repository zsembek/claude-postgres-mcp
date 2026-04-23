# pg-mcp

Self-hosted **PostgreSQL MCP server** with built-in OAuth 2.1 (PKCE) authorization.

- **Access mode:** read-only — `SELECT` / `WITH` queries only
- **Auth:** OAuth 2.1 Authorization Code + PKCE (no external IdP required)
- **Transports:** StreamableHTTP (`/mcp`, MCP 2025-03-26) + legacy SSE (`/sse`)
- **Tools:** `execute_query`, `list_schemas`, `list_tables`, `describe_table`

---

## ⚠️ Important: Claude Desktop Custom Connectors are broken

Since **December 18, 2025**, Claude Desktop's built-in "Custom Connectors" OAuth flow is broken for self-hosted MCP servers ([claude-ai-mcp#5](https://github.com/anthropics/claude-ai-mcp/issues/5), [claude-code#11814](https://github.com/anthropics/claude-code/issues/11814)).
When a user clicks **Connect**, the OAuth proxy on claude.ai does not call the server's `/oauth/register` or `/oauth/authorize`, and the flow dies at `step=start_error`.

**Therefore `pg-mcp` works via two officially supported paths only:**

1. **Claude Desktop via the `mcp-remote` npm bridge** (stdio → HTTP + OAuth locally)
2. **Claude Code CLI** (`claude mcp add --transport http ...`) — native OAuth support

Both paths talk to the server directly and **skip** the broken `claude.ai` OAuth proxy.

---

# Part 1 · Deploy the server

## 0. Install Docker on Ubuntu (if not installed)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version   # v2.x.x
```

## 1. Clone and configure

```bash
git clone https://github.com/zsembek/claude-postgres-mcp.git pg-mcp
cd pg-mcp/server
cp .env.example .env
```

Edit `.env`:

```ini
DATABASE_URL=postgresql://user:password@host:5432/dbname
SERVER_URL=https://your.domain:19000
JWT_SECRET=<run: openssl rand -base64 48>
ADMIN_TOKEN=<run: openssl rand -base64 24>
```

`ADMIN_TOKEN` is what employees paste on the authorization screen to get a JWT.

## 2. DNS + TLS

Point your domain (e.g. `ai.brands.kz`) at the server's public IP. The compose stack uses:
- nginx on port **19000 → 443 SSL** with Let's Encrypt certs
- certbot sidecar for automatic renewal

Initial cert issuance is done once by `server/init-cert.sh` (standalone mode — port 80 must be free during run).

```bash
sudo ./init-cert.sh your.domain
```

## 3. Launch

```bash
docker compose up -d --build
docker compose logs pg-mcp --tail 30
# expect:
#   ✓ Database pool ready
#   ✓ StreamableHTTP session manager ready
#   Application startup complete.
```

## 4. Verify

```bash
curl -i https://your.domain:19000/mcp
# HTTP/1.1 401 Unauthorized
# www-authenticate: Bearer realm="...", resource_metadata="..."

curl -s https://your.domain:19000/.well-known/oauth-authorization-server | jq
# { "issuer": "https://your.domain:19000", ... }

curl -s https://your.domain:19000/health
# {"status":"ok","db_pool":true}
```

If all three pass — the server is ready.

---

# Part 2 · Connect Claude Desktop (via `mcp-remote`)

This is the setup every employee runs locally. It uses Node.js to bridge Claude Desktop ↔ our remote OAuth-protected MCP server.

## Prerequisites

**Install Node.js LTS** on Windows / macOS / Linux from <https://nodejs.org>.
Verify:

```powershell
node --version   # v20+ recommended
npx --version
```

## Configure `claude_desktop_config.json`

Location:

| OS      | Path                                                |
|---------|-----------------------------------------------------|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`       |
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux   | `~/.config/Claude/claude_desktop_config.json`       |

Content (merge `mcpServers` with whatever else you have):

```json
{
  "mcpServers": {
    "pg-mcp": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://your.domain:19000/mcp"
      ]
    }
  }
}
```

**Windows PowerShell one-liner** (overwrites the file — back it up first if you have other entries):

```powershell
@'
{
  "mcpServers": {
    "pg-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your.domain:19000/mcp"]
    }
  }
}
'@ | Set-Content -Path "$env:APPDATA\Claude\claude_desktop_config.json" -Encoding UTF8
```

## Launch + first-time OAuth

1. **Remove** any old `pg-mcp` entry from `Settings → Connectors` (Custom Connectors) inside Claude Desktop — the broken path.
2. **Fully quit** Claude Desktop (system tray → Quit, confirm no `Claude.exe` in Task Manager).
3. **Launch** Claude Desktop again.
4. On first tool call (or at MCP startup), `mcp-remote` opens your browser at the server's `/oauth/authorize` page.
5. Paste the **admin token** from `.env` → click **Authorize**.
6. Browser shows a success page; close it. Token is cached in `~/.mcp-auth/`.
7. You'll see the `pg-mcp` server in the MCP sidebar with tools `execute_query`, `list_schemas`, `list_tables`, `describe_table`.

## Smoke test inside Claude Desktop

> Show me the schemas in the database.

Claude should call `list_schemas` and return the list.

---

# Part 3 · Connect Claude Code CLI (optional, for power users)

Native OAuth support, no Node.js bridge needed.

```powershell
claude mcp add pg-mcp --transport http https://your.domain:19000/mcp
claude mcp list
```

Inside any `claude` session:

```
/mcp
```

Pick `pg-mcp` → **Authenticate** → browser opens → paste admin token → done.

---

# Part 4 · Rolling out to employees

Each employee needs to do **only three things**:

1. Install Node.js LTS.
2. Paste the JSON snippet above into their `claude_desktop_config.json` (URL is the same for everyone).
3. At first launch, paste the **admin token** in the browser authorization screen.

You can send one internal message containing:
- The JSON snippet (above)
- The admin token from `.env`
- Link to Node.js download page

That's it — no per-user certs, no credentials setup, no server-side config.

### Revoking access

To revoke everyone at once: change `JWT_SECRET` in `server/.env` and `docker compose up -d pg-mcp`.
All issued tokens become invalid; users go through OAuth again with the admin token (or a new one if you rotated it).

---

# Architecture

```
┌────────────────────┐     stdio       ┌──────────────┐   HTTPS + OAuth   ┌────────────┐
│ Claude Desktop     │ ───────────────▶│ mcp-remote   │ ─────────────────▶│ pg-mcp     │
│ (per employee)     │                 │ (npx, local) │                   │ (server)   │
└────────────────────┘                 └──────────────┘                   └─────┬──────┘
                                                                                │
                                                                                ▼
                                                                         ┌──────────────┐
                                                                         │ PostgreSQL   │
                                                                         │ (read-only)  │
                                                                         └──────────────┘
```

Alt path for CLI users: `Claude Code CLI ──(HTTPS + OAuth)──▶ pg-mcp`.

---

# Server endpoints

| Path                                                  | Purpose                                       |
|-------------------------------------------------------|-----------------------------------------------|
| `GET  /health`                                        | Liveness + DB pool check                      |
| `GET  /.well-known/oauth-authorization-server`        | OAuth 2.1 AS metadata (RFC 8414)              |
| `GET  /.well-known/oauth-protected-resource`          | Protected resource metadata (RFC 9728)        |
| `POST /oauth/register`                                | Dynamic Client Registration (RFC 7591)        |
| `GET  /oauth/authorize`                               | HTML form — enter admin token                 |
| `POST /oauth/authorize`                               | Verify token, issue auth code                 |
| `POST /oauth/token`                                   | Exchange code/refresh token for access token  |
| `GET/POST/DELETE /mcp`                                | StreamableHTTP MCP transport (preferred)      |
| `GET  /sse` + `POST /messages/`                       | Legacy SSE transport                          |

All MCP paths require `Authorization: Bearer <jwt>`.

---

# Security notes

- **Read-only DB**: server enforces `SELECT` / `WITH` only via SQL parser; other statements are rejected before execution.
- **Row limit**: `MAX_ROWS` in `.env` caps result size (default 1000).
- **OAuth 2.1 + PKCE S256**: single admin-token authentication per employee; issued JWTs expire in 1 hour; refresh tokens in 30 days.
- **TLS**: served behind nginx with Let's Encrypt Mozilla-Modern profile (TLS 1.2 + 1.3, ECDHE-only, no renegotiation).
- **No external IdP**: everything runs on your hardware; no tokens leave the perimeter beyond the JWTs issued to Claude Code / mcp-remote bridges.

---

# Troubleshooting

**`mcp-remote` can't open browser / authorize keeps failing**
Delete `~/.mcp-auth/` and restart Claude Desktop — cached broken tokens are wiped.

**Claude Desktop doesn't see `pg-mcp` in MCP list**
- Verify JSON is valid (`python -m json.tool claude_desktop_config.json`)
- Fully quit Claude Desktop (check Task Manager) before relaunch
- Check npx can run: `npx -y mcp-remote@latest --help`

**`curl https://your.domain:19000/mcp` returns 502**
Container restart changed Docker IP; `docker compose restart nginx`.

**`/mcp` returns 501 "StreamableHTTP not available"**
Rebuild with `mcp>=1.9.0` pinned in `requirements.txt`: `docker compose up -d --build pg-mcp`.

**Want to add `pg-mcp` to Custom Connectors UI anyway**
Don't — it's broken at Anthropic's end until they fix their OAuth proxy. Use `mcp-remote` instead.

---

# References

- [MCP 2025-03-26 Authorization spec](https://modelcontextprotocol.io/specification/draft/basic/authorization)
- [RFC 9728 — OAuth 2.0 Protected Resource Metadata](https://datatracker.ietf.org/doc/html/rfc9728)
- [mcp-remote on npm](https://www.npmjs.com/package/mcp-remote)
- [Claude Code MCP docs](https://code.claude.com/docs/en/mcp)
- [Claude Desktop OAuth broken — issue #5](https://github.com/anthropics/claude-ai-mcp/issues/5)
