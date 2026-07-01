# Security Policy

Cloud MCP Server gives remote AI tools shell and file access to your Linux server. Treat it like SSH access.

Recommended minimum security:

- Use HTTPS only.
- Use a long random `MCP_API_KEY`.
- Never commit `.env`.
- Do not expose the local Uvicorn port directly to the internet.
- Run behind Nginx or another reverse proxy.
- Prefer a dedicated low-privilege Linux user for production.
- Keep system packages and Python dependencies updated.
- Review logs regularly.
- Disable optional proxy integrations you do not use.

For public deployments, consider adding IP allowlisting, a WAF, or VPN-only access.
