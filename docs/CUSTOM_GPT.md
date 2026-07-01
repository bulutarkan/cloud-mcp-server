# Custom GPT Actions Setup

This guide connects a Custom GPT to your Cloud MCP Server through `/api/*` REST endpoints.

OpenAI Actions require API details, authentication information, and an OpenAPI schema. This repository already includes the schema in `examples/openapi.yaml`.

## 1. Configure the server `.env`

On your Linux server:

```bash
cd /home/ubuntu/cloud-mcp-server
cp .env.example mcp_server/.env
nano mcp_server/.env
```

Minimum required values:

```env
BASE_URL=https://mcp.yourdomain.com
MCP_API_KEY=replace_with_a_long_random_api_key
OAUTH_CLIENT_SECRET=replace_with_oauth_client_secret
OAUTH_MASTER_PASSWORD=replace_with_master_password
SERVER_HOME=/home/ubuntu
```

Generate secure secrets:

```bash
openssl rand -hex 32
```

Important values:

| Env variable | What it does | Required? |
|---|---|---|
| `BASE_URL` | Your public HTTPS server URL, for example `https://mcp.yourdomain.com` | Yes |
| `MCP_API_KEY` | API key used by Custom GPT Actions for `/api/*` endpoints | Yes |
| `OAUTH_CLIENT_SECRET` | OAuth client secret for MCP clients that use `/mcp` directly | Recommended |
| `OAUTH_MASTER_PASSWORD` | Password shown on the OAuth authorization page | Recommended |
| `SERVER_HOME` | Default home directory where commands run | Yes |
| `SHARED_DIR` | Optional shared folder path | No |
| `WP_MCP_URL` | Optional WordPress MCP proxy URL | No |
| `ZOHO_MCP_URL` | Optional Zoho MCP proxy URL | No |
| `TAVILY_MCP_URL` | Optional Tavily MCP URL, usually includes your Tavily API key | No |
| `N8N_MCP_URL` | Optional n8n MCP endpoint URL | No |

Restart after editing `.env`:

```bash
sudo systemctl restart cloud-mcp
```

## 2. Confirm the server works

```bash
curl https://mcp.yourdomain.com/health
```

You should see a JSON response with `ok: true`.

## 3. Configure authentication in Custom GPT

In the GPT editor:

1. Create or edit your GPT.
2. Open **Actions**.
3. Create a new Action.
4. Under **Authentication**, choose **API Key**.
5. Recommended setup:
   - **Auth type:** `Custom`
   - **Custom Header Name:** `x-api-key`
   - **API Key:** paste the exact value of `MCP_API_KEY` from `mcp_server/.env`

Alternative setup if your UI shows Bearer authentication:

- **Auth type:** `Bearer`
- **API Key:** paste the exact same `MCP_API_KEY`

The server accepts both:

```http
x-api-key: YOUR_MCP_API_KEY
```

and:

```http
Authorization: Bearer YOUR_MCP_API_KEY
```

## 4. Add the OpenAPI schema

Open:

```bash
examples/openapi.yaml
```

Replace this:

```yaml
servers:
  - url: https://mcp.example.com
```

with your own public HTTPS URL:

```yaml
servers:
  - url: https://mcp.yourdomain.com
```

Then paste the full schema into the Custom GPT Action schema editor.

## 5. Do users only change the URL?

For a basic Linux server assistant, users usually change only these things:

1. `BASE_URL` in `mcp_server/.env`
2. `MCP_API_KEY` in `mcp_server/.env`
3. `servers[0].url` in `examples/openapi.yaml`
4. The Action authentication key in Custom GPT, using the same `MCP_API_KEY`

Optional integrations need more env variables:

- WordPress tools need `WP_MCP_URL`, and sometimes `WP_MCP_USERNAME` / `WP_MCP_PASSWORD`.
- Zoho tools need `ZOHO_MCP_URL`.
- Tavily tools need `TAVILY_MCP_URL`.
- n8n tools need `N8N_MCP_URL`.

If you do not configure those optional values, the core Linux tools still work.

## 6. Test safely

Test `health_check` first with an empty JSON object:

```json
{}
```

Then test `run_command`:

```json
{"command":"pwd && whoami && hostname && date"}
```

## 7. Suggested GPT instructions

```text
You are connected to my Linux server through Cloud MCP. Use server tools carefully. Prefer read-only checks before changing files. Ask for confirmation before destructive commands such as rm, kill, disk formatting, firewall changes, package removals, database writes, or production restarts. Use background jobs for long-running installs, builds, tests, Docker commands, and dev servers.
```
