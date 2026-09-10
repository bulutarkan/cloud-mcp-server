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
- Control a real Linux X11/VNC desktop with screenshots, mouse, keyboard, scrolling, window focus, drag, and headed Chromium
- Run long commands as background jobs with logs
- Delegate long tasks to OpenCode or Codex agents in the background
- Run parallel commands
- Persistent human-readable memory with hybrid SQLite FTS5 + multilingual semantic search
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
git clone https://github.com/bulutarkan/cloud-mcp-server.git
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


## Real Linux desktop + headed Chromium

Cloud MCP can control a **real X11 desktop** such as an XFCE session running inside TigerVNC. This is not a headless Playwright browser: the assistant receives an actual desktop screenshot, reasons about what is visible, and sends real X11 mouse/keyboard events to the same GUI you can watch over VNC.

Typical uses include operating Chromium from your phone through ChatGPT, handling websites that require a normal persistent browser session, and interacting with other allow-listed desktop apps.

### Install the optional desktop dependencies

```bash
cd /home/ubuntu/cloud-mcp-server
./scripts/install-desktop.sh
```

If the server does not yet have XFCE/TigerVNC packages, install them too without automatically exposing a VNC port:

```bash
./scripts/install-desktop.sh --with-vnc-packages
```

The helper installs `xdotool`, `wmctrl`, `xclip`, ImageMagick, DBus X11 support, and Chromium when needed. It deliberately does **not** publish or configure a public VNC listener. Keep VNC on localhost/SSH or a private VPN such as Tailscale.

Configure the display in `mcp_server/.env`:

```env
CLOUD_MCP_DESKTOP_DISPLAY=:1
CLOUD_MCP_DESKTOP_XAUTHORITY=/home/ubuntu/.Xauthority
# Optional overrides:
# CLOUD_MCP_CHROMIUM_BIN=/snap/bin/chromium
# CLOUD_MCP_CHROMIUM_PROFILE=/home/ubuntu/snap/chromium/common/cloud-mcp-profile
```

For Chromium Snap, the default Cloud MCP profile is persistent under `~/snap/chromium/common/cloud-mcp-profile`. Closing Chromium or restarting the MCP does not delete that profile, so cookies, site storage, browsing state, and authenticated sessions can survive restarts.

### Semantic Chromium DOM control

In addition to raw desktop screenshots and coordinate input, headed Chromium exposes a localhost-only Chrome DevTools Protocol endpoint. Cloud MCP uses it directly (no Playwright and no headless browser) to inspect the DOM of the same visible Chromium window you see over VNC.

`browser_observe` returns stable `e1`, `e2`, ... element IDs with tag, ARIA role, text, placeholder, current value, checked state, select options, and viewport/document rectangles. `browser_act` can then target those IDs (or semantic query/role/text matches) for click, double-click, type/paste, select, check/uncheck, scroll, and focus actions. `observation_id` protects against stale page observations after navigation/reload.

Browser tools:

- `browser_list_tabs` — list inspectable headed Chromium tabs and stable CDP tab IDs.
- `browser_activate_tab` / `browser_close_tab` — activate or close a tab by tab ID.
- `browser_observe` — semantic DOM observation with optional viewport/element/full-page JPEG.
- `browser_find` — exact-first semantic element lookup.
- `browser_act` — batch up to 20 element-targeted actions.
- `browser_open_url` — navigate a selected tab or create a new visible tab.

The DevTools listener is bound to `127.0.0.1` only. Override its local port if needed:

```env
CLOUD_MCP_CHROMIUM_DEBUG_PORT=9222
```

Keep this port firewalled/private; Cloud MCP never needs to expose it through Nginx or the public Internet. For browser tasks, prefer `browser_observe`/`browser_act`; use `desktop_observe`/`desktop_act` as the visual/native fallback for browser chrome, OS dialogs, captchas, extensions, or non-browser applications.

The desktop tools are:

- `desktop_capabilities` — verify the X11 display, input/screenshot dependencies, Chromium path/profile, and password-store safety status.
- `desktop_observe` — return window metadata plus a connector-safe JPEG of the real desktop. It also reports the screenshot-to-display coordinate scale for accurate clicks.
- `desktop_windows` / `desktop_focus` — list and focus real X11 windows.
- `desktop_move_mouse`, `desktop_click`, `desktop_drag`, `desktop_type`, `desktop_key`, `desktop_scroll` — real mouse/keyboard input.
- `desktop_act` — batch up to 30 GUI actions in one call and optionally return a fresh screenshot/state, reducing remote round trips.
- `desktop_launch` — launch an allow-listed GUI app (`chromium`, `firefox`, or `terminal`).
- `chromium_launch` / `chromium_open_url` / `chromium_close` — manage visible Chromium while preserving its persistent profile. URL navigation uses the real address bar (`Ctrl+L`, paste, Enter).

A normal visual workflow is `desktop_observe` → decide where to interact → `desktop_act` → inspect the returned screenshot. The browser remains visible in VNC the entire time.

### Chromium profile and password security

A persistent browser profile is effectively a credential because it can contain authenticated cookies. Protect the server account, the MCP endpoint, and the VNC session accordingly.

On Ubuntu Chromium Snap, `desktop_capabilities` checks whether the Snap `password-manager-service` interface is backed by a desktop secret service. If it reports `mode: basic`, Chromium's launcher is using its **non-encrypted basic saved-password store**. Cookies/site sessions still persist, but do not use Chromium's "save password" feature until you configure a desktop keyring/secret service. Cloud MCP does not weaken or bypass this protection automatically.

If an aggressive `/tmp` cleanup script removes `/tmp/snap-private-tmp`, Chromium Snap may fail before opening. Exclude that directory from custom cleanup jobs or restore the required root-owned directory:

```bash
sudo install -d -m 0700 -o root -g root /tmp/snap-private-tmp
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

OpenAI Actions need two things: authentication details and an OpenAPI schema. This repository includes the schema at `examples/openapi.yaml`.

### 1. Configure `.env` on your server

Open your server env file:

```bash
nano /home/ubuntu/cloud-mcp-server/mcp_server/.env
```

Set at least these values:

```env
BASE_URL=https://mcp.yourdomain.com
MCP_API_KEY=replace_with_a_long_random_api_key
OAUTH_CLIENT_SECRET=replace_with_oauth_client_secret
OAUTH_MASTER_PASSWORD=replace_with_master_password
SERVER_HOME=/home/ubuntu
```

Generate strong values with:

```bash
openssl rand -hex 32
```

`MCP_API_KEY` is the key your Custom GPT will send with every `/api/*` request. Keep it private.

### 2. Configure authentication in the Custom GPT Action

In ChatGPT:

1. Create or edit your GPT.
2. Go to **Actions**.
3. Create a new action.
4. Under **Authentication**, choose **API Key**.
5. Use this setup:
   - **Auth type:** `Custom`
   - **Custom Header Name:** `x-api-key`
   - **API Key:** paste the exact value of `MCP_API_KEY` from `mcp_server/.env`

Alternative setup if the UI shows Bearer authentication instead:

- **Auth type:** `Bearer`
- **API Key:** paste the same `MCP_API_KEY`

The server accepts both `x-api-key: YOUR_KEY` and `Authorization: Bearer YOUR_KEY` for the REST API.

### 3. Add the OpenAPI schema

Open:

```bash
examples/openapi.yaml
```

In the schema, replace only this server URL:

```yaml
servers:
  - url: https://mcp.example.com
```

with your real public URL:

```yaml
servers:
  - url: https://mcp.yourdomain.com
```

Then paste the whole schema into the Custom GPT Action schema editor.

### 4. What users usually need to change

For a basic Linux server assistant, users usually change only:

1. `BASE_URL` in `mcp_server/.env`
2. `MCP_API_KEY` in `mcp_server/.env`
3. The `servers[0].url` value in `examples/openapi.yaml`
4. The Custom GPT Action authentication key, using the same `MCP_API_KEY`

Optional integrations such as WordPress, Zoho, Exa, Tavily, and n8n require their own `.env` variables. Leave them empty if you do not use them.

### 5. Test the Action

Test `/api/health_check` first. Then test `/api/run` with:

```json
{"command":"pwd && whoami && hostname && date"}
```

A good GPT instruction starter:

```text
You are connected to my Linux server through Cloud MCP. Before running destructive commands, explain the risk and ask for confirmation. Prefer reading files and checking status before changing anything. Use background jobs for long-running installs, builds, and dev servers.
```

## Agent delegation

If OpenCode and/or Codex CLI is installed on the server, the MCP endpoint exposes seven compact delegation tools:

- `agent_catalog` — discover providers, models, and reasoning options
- `spawn_agent` — start one non-blocking background agent; supports idle timeout and same-model automatic retries
- `spawn_agents` — start up to 10 agents as one `team_id` in a single call with shared provider/model/reasoning/access settings
- `wait_agents` — bounded wait for `all`, `any`, or `majority` completion and collect concise results without repeated polling
- `list_agents` — list compact agent states/result previews and optionally filter by `team_id`
- `get_agent` — fetch status, progress/timing telemetry and the concise final handoff; logs are opt-in for debugging
- `agent_action` — cancel, retry, despawn or resume an individual agent; team cancel/despawn/retry cascades to children

Agent and team state is stored on disk, so completed results remain available across MCP restarts. Team children inherit one shared model configuration to prevent accidental mixed-model teams. Progress metadata includes provider/event timing, phase, idle time, steps and tool calls. The default final handoff is intentionally concise to keep the parent AI context small.


## Persistent memory

The MCP endpoint exposes five persistent memory tools:

- `memory_add` — append a timestamped memory
- `memory_search` — hybrid semantic/FTS search or date-range listing
- `memory_get` — fetch one exact stable `memory_id`
- `memory_update` — update one exact memory, or list candidates by date before choosing
- `memory_delete` — preview deletion and require `confirm=true` for the destructive step

Human-readable Markdown is the source of truth and is stored under `~/.cloud-mcp/memory/YYYY/MM/YYYY-MM-DD.md` using Europe/Istanbul timestamps. A rebuildable SQLite FTS5/vector index lives at `~/.cloud-mcp/memory/memory-index.sqlite3`. Manual edits to the Markdown journals are detected and re-indexed automatically.

Query-based `memory_search` uses FastEmbed with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions) for multilingual semantic retrieval, with a dependency-free feature-hash fallback. The embedding model is downloaded lazily to `~/.cloud-mcp/cache/fastembed`. Inference runs in a separate worker process: normal add/get/update/delete and queryless listing do not start it; semantic search starts it on demand, reuses it briefly, and the worker exits after 60 seconds of embedding inactivity by default so model RAM is reclaimed.

Optional environment overrides:

```env
CLOUD_MCP_MEMORY_DIR=/home/ubuntu/.cloud-mcp/memory
CLOUD_MCP_MEMORY_MODEL_CACHE=/home/ubuntu/.cloud-mcp/cache/fastembed
CLOUD_MCP_MEMORY_MODEL_IDLE_SECONDS=60
CLOUD_MCP_MEMORY_EMBEDDING=auto
```

The memory and Agent Skills indexes share **one** embedding manager, model cache, and on-demand FastEmbed worker. `memory_search` and `skill_search` therefore never keep separate model processes in RAM. The shared worker exits after 60 seconds of embedding inactivity by default. New shared environment names are `CLOUD_MCP_EMBEDDING`, `CLOUD_MCP_EMBEDDING_MODEL_CACHE`, and `CLOUD_MCP_EMBEDDING_IDLE_SECONDS`; the older `CLOUD_MCP_MEMORY_*` embedding variables remain supported for backward compatibility.

## Agent Skills

Cloud MCP supports the open Agent Skills directory format. Managed skills live under `~/.cloud-mcp/skills/<skill-name>/SKILL.md`. A skill may also bundle `scripts/`, `references/`, `assets/`, or other files. Only name/description/path are returned during discovery; `skill_get` loads the full `SKILL.md`, and bundled resources are listed without eagerly loading their contents.

The MCP endpoint exposes five skill tools:

- `skill_list` — list skill catalog metadata and SKILL.md locations
- `skill_search` — hybrid SQLite FTS5 + shared multilingual semantic search
- `skill_get` — activate a skill by loading SKILL.md plus resource paths
- `skill_register` — register an external skill directory/SKILL.md path
- `skill_update_index` — rescan changed skill files without starting the embedding model

The skill index is rebuildable and stored at `~/.cloud-mcp/skills/skills-index.sqlite3`. Managed SKILL.md files are discovered automatically; registered external files remain source-of-truth at their original path.

Typical skill layout:

```text
~/.cloud-mcp/skills/wordpress-performance/
├── SKILL.md
├── scripts/
├── references/
└── assets/
```

`SKILL.md` must start with YAML frontmatter containing at least a lowercase/hyphenated `name` and a non-empty `description`. Relative paths in SKILL.md are resolved from the skill directory.

## Safe self-deploy

Two MCP tools support Cloud MCP self-maintenance without depending on the Mac:

- `cloud_mcp_self_deploy(check_only=true)` — fetches Git metadata and compares `/home/ubuntu/Projects/cloud-mcp-server` with the runtime.
- `cloud_mcp_self_deploy(check_only=false)` — only from a clean repository; runs compile/tests/pip checks, then starts a detached deploy helper. The helper backs up managed runtime code, installs declared dependencies, restarts `cloud-mcp.service`, checks `/health`, and restores the previous code automatically if health fails.
- `cloud_mcp_deploy_status(deployment_id)` — reads the persisted deployment result after the MCP process has restarted.

Deployment records live under `~/.cloud-mcp/deployments/` and code backups under `~/.cloud-mcp/deploy-backups/`. The helper runs outside the MCP request process so a normal service restart does not kill the deployment controller.

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
