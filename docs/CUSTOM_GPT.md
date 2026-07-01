# Custom GPT Actions Setup

This guide connects a Custom GPT to your Cloud MCP Server through `/api/*` REST endpoints.

## 1. Confirm the server works

```bash
curl https://mcp.yourdomain.com/health
```

## 2. Configure authentication

In the GPT editor, create an Action and choose API Key authentication.

Recommended options:

- Auth type: `Custom header`
- Header name: `x-api-key`
- Header value: your `MCP_API_KEY` from `mcp_server/.env`

Bearer auth also works if you use the same `MCP_API_KEY`.

## 3. Add the OpenAPI schema

Open `examples/openapi.yaml`, replace:

```text
https://mcp.example.com
```

with your server URL:

```text
https://mcp.yourdomain.com
```

Then paste the schema into the Action schema editor.

## 4. Test safely

Test these first:

```json
{}
```

with `health_check`, then:

```json
{"command":"pwd && whoami && hostname && date"}
```

with `run_command`.

## 5. Suggested GPT instructions

```text
You are connected to my Linux server through Cloud MCP. Use server tools carefully. Prefer read-only checks before changing files. Ask for confirmation before destructive commands such as rm, kill, disk formatting, firewall changes, package removals, database writes, or production restarts. Use background jobs for long-running installs, builds, tests, Docker commands, and dev servers.
```
