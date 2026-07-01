# Cloud MCP Server

Cloud MCP Server is a self-hosted bridge that lets an AI assistant connect to your Linux server through two interfaces:

1. **MCP endpoint**: `/mcp` for MCP-compatible clients.
2. **REST API endpoint**: `/api/*` for Custom GPT Actions using an OpenAPI schema.

It is designed for Ubuntu/Linux servers such as Oracle Cloud, DigitalOcean, Hetzner, AWS EC2, or any VPS where you have SSH access.

> ⚠️ Security warning: this server can run shell commands and edit files on your machine. Put it behind HTTPS, use a long API key, never commit `.env`, and only share the endpoint with people you trust.

## Features

- Run shell commands
- Read, write, edit, move, copy, delete files
- List processes and kill processes
- Check CPU, RAM, disk, network, and uptime
- Run long commands as background jobs with logs
- Run parallel commands
- Make outbound HTTP requests
- Optional proxy endpoints for WordPress MCP, Zoho MCP, Exa, Tavily, and n8n
- systemd service file for 24/7 operation and automatic restart
- Nginx reverse proxy example
- Custom GPT OpenAPI schema example

## Requirements

- Ubuntu 22.04+ or another modern Linux server
- Python 3.10+
- A domain or subdomain pointing to your server, recommended for HTTPS
- Nginx and Certbot, recommended for production
- A Custom GPT with Actions enabled, if you want to use the REST API from ChatGPT

## Quick install on a new server

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

cd /home/ubuntu
git clone https://github.com/YOUR_GITHUB_USERNAME/cloud-mcp-server.git
cd cloud-mcp-server
cp .env.example mcp_server/.env
nano mcp_server/.env
./scripts/install.sh
sudo systemctl restart cloud-mcp
sudo systemctl status cloud-mcp --no-pager
```

Your app will run locally on:

```text
http://127.0.0.1:8000
```

## Configure your `.env`

Edit:

```bash
nano /home/ubuntu/cloud-mcp-server/mcp_server/.env
```

Minimum required values:

```env
BASE_URL=https://mcp.yourdomain.com
MCP_API_KEY=use_a_long_random_secret
OAUTH_CLIENT_SECRET=use_a_long_random_secret
OAUTH_MASTER_PASSWORD=use_a_long_random_password
SERVER_HOME=/home/ubuntu
```

Generate strong secrets:

```bash
openssl rand -hex 32
```

## Nginx and HTTPS

Copy the example config:

```bash
sudo cp nginx/cloud-mcp.conf /etc/nginx/sites-available/cloud-mcp
sudo nano /etc/nginx/sites-available/cloud-mcp
```

Change this line:

```nginx
server_name mcp.example.com;
```

to your real domain:

```nginx
server_name mcp.yourdomain.com;
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/cloud-mcp /etc/nginx/sites-enabled/cloud-mcp
sudo nginx -t
sudo systemctl reload nginx
```

Enable HTTPS:

```bash
sudo certbot --nginx -d mcp.yourdomain.com
```

Then set this in `mcp_server/.env`:

```env
BASE_URL=https://mcp.yourdomain.com
```

Restart:

```bash
sudo systemctl restart cloud-mcp
```

Test:

```bash
curl https://mcp.yourdomain.com/health
```

## Custom GPT setup

In ChatGPT:

1. Create or edit your GPT.
2. Go to **Actions**.
3. Create a new action.
4. Authentication: choose **API Key**.
5. Auth type: **Bearer** or **Custom header**.
   - Bearer: use your `MCP_API_KEY`.
   - Custom header: header name `x-api-key`, value your `MCP_API_KEY`.
6. Paste `examples/openapi.yaml` into the schema editor.
7. Replace every `https://mcp.example.com` with your real `BASE_URL`.
8. Test `/api/health_check` first.

A good GPT instruction starter:

```text
You are connected to my Linux server through Cloud MCP. Before running destructive commands, explain the risk and ask for confirmation. Prefer reading files and checking status before changing anything. Use background jobs for long-running installs, builds, and dev servers.
```

## Important endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Public health check |
| `POST /api/run` | Run a shell command |
| `POST /api/health_check` | Get system health |
| `POST /api/read_file` | Read a file |
| `POST /api/write_file` | Write a file |
| `POST /api/jobs/start` | Start a background job |
| `POST /api/jobs/status` | Check background job status |
| `POST /api/jobs/output` | Read background job logs |
| `POST /mcp` | MCP streamable HTTP endpoint |

## Service management

```bash
sudo systemctl status cloud-mcp --no-pager
sudo systemctl restart cloud-mcp
sudo journalctl -u cloud-mcp -f
```

The service uses `Restart=always`, so systemd will start it again after a crash.

## Updating

```bash
cd /home/ubuntu/cloud-mcp-server
git pull
. .venv/bin/activate
pip install -r mcp_server/requirements.txt
sudo systemctl restart cloud-mcp
```

## Optional integrations

These are disabled unless you set their env variables:

```env
WP_MCP_URL=
WP_MCP_USERNAME=
WP_MCP_PASSWORD=
ZOHO_MCP_URL=
EXA_MCP_URL=https://mcp.exa.ai/mcp
TAVILY_MCP_URL=
N8N_MCP_URL=
```

Leave them empty if you only need Linux server control.

## Safety checklist before publishing your own fork

Run this before pushing:

```bash
grep -RInE 'API_KEY|SECRET|TOKEN|PASSWORD|BEGIN PRIVATE|\.cloud|\.com' . \
  --exclude-dir=.git --exclude='.env.example' --exclude='README.md'
```

Make sure no real passwords, tokens, domains, private keys, logs, backups, or `.env` files are committed.

## License

MIT
