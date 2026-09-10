from __future__ import annotations

import asyncio, json, time, uuid, os
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .security import RateLimiter, Settings, load_settings, setup_audit_logger, truncate
from .oauth import (
    authorize_get, authorize_post_handler, token_handler, verify_token,
    register_client_handler,
)
from .tools import (
    run_command, process_list, kill_process, get_system_info,
    write_file, write_files_batch, read_file, read_multiple_files,
    edit_file, apply_patch, move_file, copy_file, delete_path,
    list_directory, directory_tree, create_directory, get_file_info, find_files,
    search_files, http_request,
)
from .tools_jobs import (
    start_background_job, get_job_status, get_job_output,
    stop_job, list_jobs, wait_jobs, run_commands_parallel,
)
from .tools_agents import (
    agent_catalog, spawn_agent, spawn_agents, wait_agents,
    list_agents, get_agent, agent_action,
)
from .tools_memory import (
    memory_add, memory_search, memory_get, memory_update, memory_delete,
)
from .tools_skills import (
    skill_list, skill_search, skill_get, skill_register, skill_update_index,
)
from .tools_update import cloud_mcp_self_deploy, cloud_mcp_deploy_status
from .tools_desktop import (
    desktop_capabilities, desktop_observe, desktop_windows, desktop_focus,
    desktop_move_mouse, desktop_click, desktop_type, desktop_key, desktop_scroll, desktop_drag, desktop_act,
    desktop_launch, chromium_launch, chromium_open_url, chromium_close,
)
from .tools_browser_semantic import (
    browser_list_tabs, browser_activate_tab, browser_close_tab, browser_observe, browser_find, browser_act, browser_open_url,
)


def _log(audit_logger, tool: str, fn):
    start = time.perf_counter()
    outcome = "ok"
    try:
        return fn()
    except HTTPException as exc:
        outcome = f"error:{exc.status_code}"
        raise
    except Exception as exc:
        outcome = f"error:500:{exc}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        audit_logger.info(json.dumps({"tool": tool, "outcome": outcome, "duration_ms": ms}))


async def _log_async(audit_logger, tool: str, fn):
    """Run blocking tool implementations without blocking Uvicorn's event loop."""
    return await asyncio.to_thread(_log, audit_logger, tool, fn)


def create_app():
    settings = load_settings()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    audit_logger = setup_audit_logger()
    base_url = settings.base_url

    mcp = FastMCP(
        name="tarkan-cloud-mcp",
        instructions=(
            "Tarkan'in Oracle Cloud sunucusunun AI asistanisin. "
            "Ana dizin: /home/ubuntu. Shared klasor: /home/ubuntu/Shared (Mac ile senkron). "
            "run_command ile her Linux komutunu calistirabilirsin. "
            "Tam dosya sistemi erisimin var."
        ),
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    _api_key = os.getenv("MCP_API_KEY", "")

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path

            # OAuth endpoints — no auth needed
            if path in {"/oauth/authorize", "/oauth/token", "/health"}:
                return await call_next(request)

            # HEAD /mcp — needed by ChatGPT connector probe
            if request.method == "HEAD" and path == "/mcp":
                return Response(status_code=200, headers={
                    "content-type": "text/event-stream; charset=utf-8",
                    "mcp-session-id": uuid.uuid4().hex,
                })

            # MCP endpoints — Nginx-validated key OR API key OR Bearer token
            if path.startswith("/mcp"):
                # Nginx-validated: /claude-mcp requests rewritten by Nginx after key check
                if request.headers.get("x-claude-key-validated") == "true":
                    return await call_next(request)

                # Direct API key check (query param or header) — skips OAuth entirely
                req_key = (
                    request.query_params.get("apiKey")
                    or request.headers.get("x-api-key")
                )
                if req_key and _api_key and req_key == _api_key:
                    return await call_next(request)

                try:
                    verify_token(request.headers.get("authorization"))
                    ip = request.headers.get("x-forwarded-for", "unknown").split(",")[0]
                    limiter.check(ip)
                except HTTPException as exc:
                    resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                    # Claude connector expects WWW-Authenticate on 401 to infer Bearer auth.
                    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                        detail = str(exc.detail)
                        err = "invalid_request" if "Missing Authorization" in detail else "invalid_token"
                        resp.headers["WWW-Authenticate"] = (
                            f'Bearer realm="mcp", error="{err}", error_description="{detail}"'
                        )
                    return resp

            return await call_next(request)

    # ── Linux desktop / real-GUI browser tools ─────────────────────────────
    @mcp.tool(name="desktop_capabilities",
              description="Check whether the real X11/VNC desktop, screenshot, mouse/keyboard and Chromium controls are available.")
    async def _desktop_capabilities() -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_capabilities", desktop_capabilities)

    @mcp.tool(name="desktop_observe",
              description="Observe the real Linux VNC/X11 desktop. Returns window metadata and, by default, a screenshot image the model can see.",
              structured_output=False)
    async def _desktop_observe(include_screenshot: bool = True,
                               window_id: Optional[str] = None) -> Any:
        return await _log_async(audit_logger, "desktop_observe",
                                lambda: desktop_observe(include_screenshot=include_screenshot, window_id=window_id))

    @mcp.tool(name="desktop_windows",
              description="List visible X11 windows with IDs, titles, classes, PIDs and screen geometry. Optional title/class filters.")
    async def _desktop_windows(title: Optional[str] = None,
                               wm_class: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_windows",
                                lambda: desktop_windows(title=title, wm_class=wm_class))

    @mcp.tool(name="desktop_focus",
              description="Focus a real desktop window by window_id or by matching title/class.")
    async def _desktop_focus(window_id: Optional[str] = None, title: Optional[str] = None,
                             wm_class: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_focus",
                                lambda: desktop_focus(window_id=window_id, title=title, wm_class=wm_class))

    @mcp.tool(name="desktop_move_mouse",
              description="Move the real X11 mouse pointer to absolute screen coordinates.")
    async def _desktop_move_mouse(x: int, y: int) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_move_mouse",
                                lambda: desktop_move_mouse(x=x, y=y))

    @mcp.tool(name="desktop_click",
              description="Click the real Linux desktop at absolute screen coordinates. button: left, middle, right.")
    async def _desktop_click(x: int, y: int, button: str = "left",
                             click_count: int = 1) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_click",
                                lambda: desktop_click(x=x, y=y, button=button, click_count=click_count))

    @mcp.tool(name="desktop_type",
              description="Type text into the focused real GUI control using the X11 clipboard; supports Unicode/Turkish text.")
    async def _desktop_type(text: str, clear: bool = False,
                            restore_clipboard: bool = True) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_type",
                                lambda: desktop_type(text=text, clear=clear, restore_clipboard=restore_clipboard))

    @mcp.tool(name="desktop_key",
              description="Press a real keyboard key or xdotool key combo such as Return, Escape, ctrl+l, ctrl+a or alt+Tab.")
    async def _desktop_key(keys: str, repeat: int = 1) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_key",
                                lambda: desktop_key(keys=keys, repeat=repeat))

    @mcp.tool(name="desktop_scroll",
              description="Scroll the real desktop. Positive amount scrolls down/right; negative scrolls up/left. Optional x/y moves pointer first.")
    async def _desktop_scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None,
                              horizontal: bool = False) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_scroll",
                                lambda: desktop_scroll(amount=amount, x=x, y=y, horizontal=horizontal))

    @mcp.tool(name="desktop_drag",
              description="Drag with the real X11 mouse from one absolute display coordinate to another.")
    async def _desktop_drag(start_x: int, start_y: int, end_x: int, end_y: int,
                            duration_ms: int = 500) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_drag",
                                lambda: desktop_drag(start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y, duration_ms=duration_ms))

    @mcp.tool(name="desktop_act",
              description="Perform up to 30 real GUI actions as one bounded batch (move, click, double_click, type, key/shortcut, scroll, focus, drag, sleep), then optionally return a fresh screenshot/state.",
              structured_output=False)
    async def _desktop_act(actions: List[Dict[str, Any]], return_state: bool = True,
                           include_screenshot: bool = True, stop_on_error: bool = True) -> Any:
        return await _log_async(audit_logger, "desktop_act",
                                lambda: desktop_act(actions=actions, return_state=return_state, include_screenshot=include_screenshot, stop_on_error=stop_on_error))

    @mcp.tool(name="desktop_launch",
              description="Launch an allow-listed GUI app on the real VNC desktop: chromium, firefox, or terminal.")
    async def _desktop_launch(app: str, url_or_arg: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "desktop_launch",
                                lambda: desktop_launch(app=app, url_or_arg=url_or_arg))

    @mcp.tool(name="chromium_launch",
              description="Launch visible Chromium on the real X11/VNC desktop with a persistent profile so sessions survive window/process restarts.")
    async def _chromium_launch(url: str = "about:blank", new_window: bool = True) -> Dict[str, Any]:
        return await _log_async(audit_logger, "chromium_launch",
                                lambda: chromium_launch(url=url, new_window=new_window))

    @mcp.tool(name="chromium_open_url",
              description="Open a URL in visible Chromium through the real GUI address bar (Ctrl+L, paste, Enter), not Playwright/headless automation.")
    async def _chromium_open_url(url: str, new_window: bool = False) -> Dict[str, Any]:
        return await _log_async(audit_logger, "chromium_open_url",
                                lambda: chromium_open_url(url=url, new_window=new_window))

    @mcp.tool(name="chromium_close",
              description="Gracefully close the active/selected Chromium window while preserving its persistent profile. Set all_windows=true to close all Chromium windows.")
    async def _chromium_close(window_id: Optional[str] = None, all_windows: bool = False) -> Dict[str, Any]:
        return await _log_async(audit_logger, "chromium_close",
                                lambda: chromium_close(window_id=window_id, all_windows=all_windows))

    # ── Semantic headed-Chromium tools (CDP + DOM, still visible in VNC) ──
    @mcp.tool(name="browser_list_tabs",
              description="List inspectable tabs in the visible Cloud MCP Chromium using localhost Chrome DevTools Protocol.")
    async def _browser_list_tabs() -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_list_tabs", browser_list_tabs)

    @mcp.tool(name="browser_activate_tab",
              description="Activate a visible Cloud MCP Chromium tab by stable CDP tab_id.")
    async def _browser_activate_tab(tab_id: str) -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_activate_tab", lambda: browser_activate_tab(tab_id))

    @mcp.tool(name="browser_close_tab",
              description="Close a visible Cloud MCP Chromium tab by stable CDP tab_id.")
    async def _browser_close_tab(tab_id: str) -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_close_tab", lambda: browser_close_tab(tab_id))

    @mcp.tool(name="browser_observe", structured_output=False,
              description="Observe the visible Chromium tab semantically. Returns stable e1/e2 element IDs, tag/role/text/value/checked/options and DOM bounding boxes; visual can be none, viewport, element, or full_page.")
    async def _browser_observe(scope: str = "interactive", max_elements: int = 40,
                               visual: str = "none", element_id: Optional[str] = None,
                               tab_id: Optional[str] = None) -> Any:
        return await _log_async(audit_logger, "browser_observe",
                                lambda: browser_observe(scope=scope, max_elements=max_elements, visual=visual, element_id=element_id, tab_id=tab_id))

    @mcp.tool(name="browser_find",
              description="Find a rendered Chromium DOM element by semantic text/role and return the best stable element_id for browser_act.")
    async def _browser_find(query: str, role: Optional[str] = None, text: Optional[str] = None,
                            tab_id: Optional[str] = None, max_results: int = 5,
                            actionable_only: bool = False) -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_find",
                                lambda: browser_find(query=query, role=role, text=text, tab_id=tab_id, max_results=max_results, actionable_only=actionable_only))

    @mcp.tool(name="browser_act",
              description="Perform up to 20 semantic actions in the visible Chromium tab using element_id or query/role/text targeting. Supports click, double_click, type/paste, select, check/uncheck, scroll and focus with observation_id stale protection.")
    async def _browser_act(actions: List[Dict[str, Any]], observation_id: Optional[str] = None,
                           tab_id: Optional[str] = None, return_state: str = "compact") -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_act",
                                lambda: browser_act(actions=actions, observation_id=observation_id, tab_id=tab_id, return_state=return_state))

    @mcp.tool(name="browser_open_url",
              description="Navigate the selected visible Chromium tab over localhost CDP; Chromium remains headed and visible in VNC.")
    async def _browser_open_url(url: str, tab_id: Optional[str] = None, new_tab: bool = False,
                                activate: bool = True) -> Dict[str, Any]:
        return await _log_async(audit_logger, "browser_open_url",
                                lambda: browser_open_url(url=url, tab_id=tab_id, new_tab=new_tab, activate=activate))

    # ── Terminal tools ──────────────────────────────────────────────────────
    @mcp.tool(name="run_command",
              description="Run any shell command on the Oracle Cloud Linux server (bash).")
    async def _run_command(command: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "run_command",
                                lambda: run_command(settings, command=command, timeout_s=timeout_s))

    @mcp.tool(name="process_list", description="List running processes. Optional name filter.")
    async def _process_list(filter: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "process_list",
                                lambda: process_list(settings, filter=filter))

    @mcp.tool(name="kill_process", description="Kill a process by PID. signal: TERM or KILL.")
    async def _kill_process(pid: int, signal: str = "TERM") -> Dict[str, Any]:
        return await _log_async(audit_logger, "kill_process",
                                lambda: kill_process(settings, pid=pid, signal=signal))

    @mcp.tool(name="get_system_info",
              description="Get server info: CPU, memory, disk, network, uptime.")
    async def _get_system_info() -> Dict[str, Any]:
        return await _log_async(audit_logger, "get_system_info",
                                lambda: get_system_info(settings))

    @mcp.tool(name="health_check",
              description="Run a quick Linux server health check: CPU, memory, disk, network, uptime.")
    async def _health_check() -> Dict[str, Any]:
        return await _log_async(audit_logger, "health_check",
                                lambda: get_system_info(settings))

    @mcp.tool(name="start_background_job",
              description="Start a long-running shell command on the Oracle Cloud Linux server and return immediately with job_id. Use for installs, builds, tests, downloads, dev servers and docker commands.")
    def _start_background_job(command: str, cwd: Optional[str] = None,
                              env: Optional[Dict[str, str]] = None,
                              timeout_s: Optional[int] = None,
                              no_output_timeout_s: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "start_background_job",
                    lambda: start_background_job(settings, command=command, cwd=cwd, env=env,
                                                 timeout_s=timeout_s, no_output_timeout_s=no_output_timeout_s))

    @mcp.tool(name="get_job_status", description="Get status for a background job by job_id.")
    def _get_job_status(job_id: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_job_status",
                    lambda: get_job_status(settings, job_id=job_id))

    @mcp.tool(name="get_job_output",
              description="Read stdout/stderr for a background job. Use since_offset for incremental output or tail_lines for recent logs.")
    def _get_job_output(job_id: str, tail_lines: Optional[int] = None,
                        since_offset: Optional[int] = None, stream: str = "both") -> Dict[str, Any]:
        return _log(audit_logger, "get_job_output",
                    lambda: get_job_output(settings, job_id=job_id, tail_lines=tail_lines,
                                           since_offset=since_offset, stream=stream))

    @mcp.tool(name="stop_job", description="Stop a background job by job_id. signal: TERM, KILL, INT, HUP.")
    def _stop_job(job_id: str, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "stop_job",
                    lambda: stop_job(settings, job_id=job_id, signal_name=signal))

    @mcp.tool(name="list_jobs",
              description="List background jobs. status_filter can be running, stalled, completed, failed, timeout, killed.")
    def _list_jobs(status_filter: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_jobs",
                    lambda: list_jobs(settings, status_filter=status_filter))

    @mcp.tool(name="wait_jobs", description="Wait for background jobs to finish, optionally returning output.")
    async def _wait_jobs(job_ids: List[str], timeout_s: Optional[int] = None,
                         return_output: bool = False) -> Dict[str, Any]:
        return await _log_async(audit_logger, "wait_jobs",
                                lambda: wait_jobs(settings, job_ids=job_ids, timeout_s=timeout_s,
                                                  return_output=return_output))

    @mcp.tool(name="run_commands_parallel",
              description="Run multiple shell commands in parallel on the Oracle Cloud Linux server and collect results.")
    async def _run_commands_parallel(commands: List[str], cwd: Optional[str] = None,
                                     timeout_s: Optional[int] = None,
                                     return_output: bool = True) -> Dict[str, Any]:
        return await _log_async(audit_logger, "run_commands_parallel",
                                lambda: run_commands_parallel(settings, commands=commands, cwd=cwd,
                                                              timeout_s=timeout_s,
                                                              return_output=return_output))

    # ── Agent delegation tools ──────────────────────────────────────────────
    @mcp.tool(
        name="agent_catalog",
        description="List available agent providers/models and supported reasoning options.",
    )
    async def _agent_catalog(provider: Optional[str] = None, model_filter: Optional[str] = None,
                             free_only: bool = False, limit: int = 80) -> Dict[str, Any]:
        return await _log_async(audit_logger, "agent_catalog",
                                lambda: agent_catalog(settings, provider=provider,
                                                      model_filter=model_filter, free_only=free_only,
                                                      limit=limit))

    @mcp.tool(
        name="spawn_agent",
        description=(
            "Delegate a task to an OpenCode or Codex agent running in the background. "
            "Returns immediately with agent_id; final handoff is concise by default."
        ),
    )
    def _spawn_agent(provider: str, prompt: str, model: Optional[str] = None,
                     reasoning: Optional[str] = None, cwd: Optional[str] = None,
                     timeout_s: Optional[int] = None, title: Optional[str] = None,
                     result_style: str = "concise", access_mode: str = "workspace_write",
                     idle_timeout_s: Optional[int] = None, retries: int = 0) -> Dict[str, Any]:
        return _log(audit_logger, "spawn_agent",
                    lambda: spawn_agent(settings, provider=provider, prompt=prompt, model=model,
                                        reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                        title=title, result_style=result_style, access_mode=access_mode,
                                        idle_timeout_s=idle_timeout_s, retries=retries))

    @mcp.tool(
        name="spawn_agents",
        description=(
            "Spawn 1-10 background agents as one team in a single call. All children inherit the same "
            "provider, model, reasoning and access_mode. Returns immediately with team_id and agent_ids."
        ),
    )
    def _spawn_agents(tasks: List[Dict[str, Any]], provider: str, model: Optional[str] = None,
                      reasoning: Optional[str] = None, cwd: Optional[str] = None,
                      timeout_s: Optional[int] = None, idle_timeout_s: Optional[int] = None,
                      retries: int = 0, result_style: str = "concise",
                      access_mode: str = "read_only", title: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "spawn_agents",
                    lambda: spawn_agents(settings, tasks=tasks, provider=provider, model=model,
                                         reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                         idle_timeout_s=idle_timeout_s, retries=retries,
                                         result_style=result_style, access_mode=access_mode, title=title))

    @mcp.tool(
        name="wait_agents",
        description=(
            "Bounded wait for a team or explicit agent_ids. mode: all, any, majority. "
            "Returns concise results for agents that finished, avoiding repeated polling."
        ),
    )
    async def _wait_agents(team_id: Optional[str] = None, agent_ids: Optional[List[str]] = None,
                           mode: str = "all", timeout_s: int = 30,
                           include_results: bool = True) -> Dict[str, Any]:
        return await _log_async(audit_logger, "wait_agents",
                                lambda: wait_agents(settings, team_id=team_id, agent_ids=agent_ids,
                                                    mode=mode, timeout_s=timeout_s,
                                                    include_results=include_results))

    @mcp.tool(
        name="list_agents",
        description="List delegated agents with compact status/result previews.",
    )
    def _list_agents(status_filter: Optional[str] = None, limit: int = 20,
                     team_id: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_agents",
                    lambda: list_agents(settings, status_filter=status_filter, limit=limit, team_id=team_id))

    @mcp.tool(
        name="get_agent",
        description="Get one delegated agent status and concise final result. Set include_logs=true only for debugging.",
    )
    def _get_agent(agent_id: str, include_logs: bool = False,
                   tail_lines: int = 40) -> Dict[str, Any]:
        return _log(audit_logger, "get_agent",
                    lambda: get_agent(settings, agent_id=agent_id, include_logs=include_logs,
                                      tail_lines=tail_lines))

    @mcp.tool(
        name="agent_action",
        description=(
            "Control one agent or a whole team. action: cancel, retry, despawn; message is available "
            "for individual resumable agent sessions. Team cancel cascades to all children."
        ),
    )
    def _agent_action(action: str, agent_id: Optional[str] = None, team_id: Optional[str] = None,
                      message: Optional[str] = None, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "agent_action",
                    lambda: agent_action(settings, action=action, agent_id=agent_id, team_id=team_id,
                                         message=message, signal=signal))

    # ── Memory tools ────────────────────────────────────────────────────────
    @mcp.tool(
        name="memory_add",
        description=(
            "Append a timestamped memory to today's Europe/Istanbul Markdown journal. "
            "The server creates ~/.cloud-mcp/memory/YYYY/MM/YYYY-MM-DD.md automatically and updates the SQLite search index."
        ),
    )
    async def _memory_add(content: str, tags: Optional[List[str]] = None,
                          importance: str = "normal", source: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "memory_add",
            lambda: memory_add(content=content, tags=tags, importance=importance, source=source),
        )

    @mcp.tool(
        name="memory_search",
        description=(
            "Search or list Cloud MCP memories. query enables hybrid SQLite FTS5 + local vector search; "
            "date/date_from/date_to filter time ranges. Query can be omitted to list memories chronologically."
        ),
    )
    async def _memory_search(query: Optional[str] = None, date: Optional[str] = None,
                             date_from: Optional[str] = None, date_to: Optional[str] = None,
                             tags: Optional[List[str]] = None, importance: Optional[str] = None,
                             sort: str = "relevance", limit: int = 20) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "memory_search",
            lambda: memory_search(query=query, date=date, date_from=date_from, date_to=date_to,
                                  tags=tags, importance=importance, sort=sort, limit=limit),
        )

    @mcp.tool(
        name="memory_get",
        description="Get one exact memory by stable memory_id without running semantic search.",
    )
    async def _memory_get(memory_id: str) -> Dict[str, Any]:
        return await _log_async(audit_logger, "memory_get", lambda: memory_get(memory_id))

    @mcp.tool(
        name="memory_update",
        description=(
            "Update an exact memory by memory_id. Without memory_id, use date/date_from/date_to to list timestamped "
            "candidate memories for selection without changing anything."
        ),
    )
    async def _memory_update(memory_id: Optional[str] = None, content: Optional[str] = None,
                             tags: Optional[List[str]] = None, importance: Optional[str] = None,
                             source: Optional[str] = None, date: Optional[str] = None,
                             date_from: Optional[str] = None, date_to: Optional[str] = None,
                             limit: int = 50) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "memory_update",
            lambda: memory_update(memory_id=memory_id, content=content, tags=tags, importance=importance,
                                  source=source, date=date, date_from=date_from, date_to=date_to, limit=limit),
        )

    @mcp.tool(
        name="memory_delete",
        description=(
            "Delete a memory by memory_id only when confirm=true. Without memory_id, date/date_from/date_to lists "
            "timestamped candidates for selection and does not delete anything."
        ),
    )
    async def _memory_delete(memory_id: Optional[str] = None, confirm: bool = False,
                             date: Optional[str] = None, date_from: Optional[str] = None,
                             date_to: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "memory_delete",
            lambda: memory_delete(memory_id=memory_id, confirm=confirm, date=date,
                                  date_from=date_from, date_to=date_to, limit=limit),
        )

    # ── Skill tools ─────────────────────────────────────────────────────────
    @mcp.tool(
        name="skill_list",
        description=(
            "List indexed Agent Skills without loading full SKILL.md bodies. Returns name, description, and location "
            "for progressive disclosure."
        ),
    )
    async def _skill_list(limit: int = 100) -> Dict[str, Any]:
        return await _log_async(audit_logger, "skill_list", lambda: skill_list(limit=limit))

    @mcp.tool(
        name="skill_search",
        description=(
            "Search Agent Skills with hybrid SQLite FTS5 + the same shared multilingual embedding worker used by memory_search. "
            "Returns skill metadata and SKILL.md paths; call skill_get to activate one."
        ),
    )
    async def _skill_search(query: str, limit: int = 10) -> Dict[str, Any]:
        return await _log_async(audit_logger, "skill_search", lambda: skill_search(query=query, limit=limit))

    @mcp.tool(
        name="skill_get",
        description=(
            "Load one Agent Skill by name or SKILL.md path. Returns full SKILL.md content, skill directory, and a list of "
            "bundled scripts/references/assets without eagerly loading resource contents."
        ),
    )
    async def _skill_get(name: Optional[str] = None, path: Optional[str] = None,
                         resource_limit: int = 200) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "skill_get",
            lambda: skill_get(name=name, path=path, resource_limit=resource_limit),
        )

    @mcp.tool(
        name="skill_register",
        description=(
            "Validate and register an existing Agent Skill directory or SKILL.md path. Managed skills under "
            "~/.cloud-mcp/skills are discovered automatically; external skill paths can be registered explicitly."
        ),
    )
    async def _skill_register(path: str) -> Dict[str, Any]:
        return await _log_async(audit_logger, "skill_register", lambda: skill_register(path=path))

    @mcp.tool(
        name="skill_update_index",
        description="Rescan managed and registered SKILL.md files and rebuild changed skill index entries without starting the embedding worker.",
    )
    async def _skill_update_index() -> Dict[str, Any]:
        return await _log_async(audit_logger, "skill_update_index", skill_update_index)

    # ── Safe self-deploy tools ───────────────────────────────────────────────
    @mcp.tool(
        name="cloud_mcp_self_deploy",
        description=(
            "Check or start a safe self-deploy from /home/ubuntu/Projects/cloud-mcp-server to the running Cloud MCP. "
            "check_only=true compares Git/runtime state. check_only=false requires a clean repo, runs preflight tests, "
            "then starts a detached deploy helper with restart, health check, and automatic code rollback on failure."
        ),
    )
    async def _cloud_mcp_self_deploy(check_only: bool = True, branch: str = "main") -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "cloud_mcp_self_deploy",
            lambda: cloud_mcp_self_deploy(check_only=check_only, branch=branch),
        )

    @mcp.tool(
        name="cloud_mcp_deploy_status",
        description="Read the persisted status/result of a Cloud MCP self-deployment by deployment_id.",
    )
    async def _cloud_mcp_deploy_status(deployment_id: str) -> Dict[str, Any]:
        return await _log_async(
            audit_logger, "cloud_mcp_deploy_status",
            lambda: cloud_mcp_deploy_status(deployment_id),
        )

    # ── File tools ──────────────────────────────────────────────────────────
    @mcp.tool(name="write_file", description="Write content to a file on the server.")
    def _write_file(path: str, content: str) -> Dict[str, Any]:
        return _log(audit_logger, "write_file",
                    lambda: write_file(settings, path=path, content=content))

    @mcp.tool(name="write_files_batch",
              description="Write multiple files in one call. Pass a list of objects with 'path' and 'content'.")
    def _write_files_batch(files: List[Dict[str, str]]) -> Dict[str, Any]:
        return _log(audit_logger, "write_files_batch",
                    lambda: write_files_batch(settings, files=files))

    @mcp.tool(name="read_file", description="Read a file. offset/length for pagination.")
    def _read_file(path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "read_file",
                    lambda: read_file(settings, path=path, offset=offset, length=length))

    @mcp.tool(name="read_multiple_files", description="Read multiple files in one call.")
    def _read_multiple_files(paths: List[str]) -> Dict[str, Any]:
        return _log(audit_logger, "read_multiple_files",
                    lambda: read_multiple_files(settings, paths=paths))

    @mcp.tool(name="edit_file",
              description="Find-and-replace in a file. Fails if count != expected_replacements.")
    def _edit_file(path: str, old_string: str, new_string: str,
                   expected_replacements: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "edit_file",
                    lambda: edit_file(settings, path=path, old_string=old_string,
                                      new_string=new_string, expected_replacements=expected_replacements))

    @mcp.tool(
        name="apply_patch",
        description=(
            "Apply a Codex-style *** Begin Patch diff. Prefer this for code edits and multi-file "
            "changes; it can add, update, move, or delete files in one call."
        ),
    )
    async def _apply_patch(patch: str, cwd: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "apply_patch",
                                lambda: apply_patch(settings, patch=patch, cwd=cwd))

    @mcp.tool(name="move_file", description="Move or rename a file or directory.")
    def _move_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "move_file",
                    lambda: move_file(settings, source=source, destination=destination))

    @mcp.tool(name="copy_file", description="Copy a file or directory.")
    def _copy_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "copy_file",
                    lambda: copy_file(settings, source=source, destination=destination))

    @mcp.tool(name="delete_path",
              description="Delete a file or directory. Set recursive=true for directories.")
    def _delete_path(path: str, recursive: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "delete_path",
                    lambda: delete_path(settings, path=path, recursive=recursive))

    @mcp.tool(name="list_directory", description="List files and directories at a path.")
    def _list_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "list_directory",
                    lambda: list_directory(settings, path=path))

    @mcp.tool(name="directory_tree", description="Show directory structure as a tree.")
    def _directory_tree(path: str, depth: int = 3) -> Dict[str, Any]:
        return _log(audit_logger, "directory_tree",
                    lambda: directory_tree(settings, path=path, depth=depth))

    @mcp.tool(name="create_directory", description="Create a directory.")
    def _create_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "create_directory",
                    lambda: create_directory(settings, path=path))

    @mcp.tool(name="get_file_info", description="Get file/directory metadata.")
    def _get_file_info(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_file_info",
                    lambda: get_file_info(settings, path=path))

    @mcp.tool(name="find_files",
              description="Find files by glob pattern. file_type: file|dir|any.")
    def _find_files(pattern: str, path: str = "/home/ubuntu",
                    file_type: str = "any") -> Dict[str, Any]:
        return _log(audit_logger, "find_files",
                    lambda: find_files(settings, pattern=pattern, path=path, file_type=file_type))

    @mcp.tool(name="search_files",
              description="Search file contents with grep. include_extensions e.g. ['py','js'].")
    async def _search_files(pattern: str, path: str = "/home/ubuntu",
                            include_extensions: Optional[List[str]] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "search_files",
                                lambda: search_files(settings, pattern=pattern, path=path,
                                                     include_extensions=include_extensions))

    @mcp.tool(name="http_request",
              description="Make HTTP GET/POST/PUT/DELETE requests to external URLs.")
    async def _http_request(url: str, method: str = "GET",
                            headers: Optional[Dict[str, str]] = None,
                            body: Optional[str] = None) -> Dict[str, Any]:
        return await _log_async(audit_logger, "http_request",
                                lambda: http_request(settings, url=url, method=method,
                                                     headers=headers, body=body))

    # ── App assembly ────────────────────────────────────────────────────────
    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware)

    # OAuth2 routes
    async def oauth_authorize(request: Request):
        if request.method == "GET":
            return authorize_get(request)
        return await authorize_post_handler(request)

    async def oauth_token(request: Request):
        return await token_handler(request)

    async def health(_: Request):
        return JSONResponse({
            "ok": True, "server": "tarkan-cloud-mcp",
            "base_url": base_url,
            "oauth_authorize": f"{base_url}/oauth/authorize",
            "oauth_token": f"{base_url}/oauth/token",
        })

    # OpenID Connect discovery (ChatGPT connector needs this)
    async def openid_config(_: Request):
        return JSONResponse({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "jwks_uri": f"{base_url}/.well-known/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "scopes_supported": ["openid", "claudeai"],
        })

    async def jwks(_: Request):
        return JSONResponse({"keys": []})

    async def oauth_server_meta(_: Request):
        return JSONResponse({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
            "resource_indicators_supported": True,
            "scopes_supported": ["claudeai"],
            "registration_endpoint": f"{base_url}/register",
        })

    # OAuth 2.0 Protected Resource Metadata (RFC 9728)
    # Claude connector probes this to learn how to send the access token to /mcp.
    async def protected_resource_meta(_: Request):
        return JSONResponse({
            "resource": f"{base_url}/mcp",
            "authorization_servers": [base_url],
            "bearer_methods_supported": ["header"],
            "jwks_uri": f"{base_url}/.well-known/jwks.json",
        })

    # Some clients probe /.well-known/oauth-protected-resource/mcp
    async def protected_resource_meta_mcp(_: Request):
        return await protected_resource_meta(_)


    app.router.routes += [
        Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/token", oauth_token, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/.well-known/openid-configuration", openid_config, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_server_meta, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource_meta, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource_meta_mcp, methods=["GET"]),
        Route("/.well-known/jwks.json", jwks, methods=["GET"]),
        Route("/register", register_client_handler, methods=["POST"]),
    ]

    # ── Simple REST API for CustomGPT Actions ──────────────────────────────
    _rest_api_key = os.getenv("MCP_API_KEY", "")

    async def api_auth(request: Request):
        auth_header = request.headers.get("authorization", "")
        bearer_key = ""
        if auth_header.lower().startswith("bearer "):
            bearer_key = auth_header.split(" ", 1)[1].strip()
        elif auth_header.lower().startswith("apikey "):
            bearer_key = auth_header.split(" ", 1)[1].strip()
        elif auth_header and " " not in auth_header:
            bearer_key = auth_header.strip()

        key = (
            request.headers.get("x-api-key")
            or request.headers.get("api-key")
            or request.query_params.get("apiKey")
            or bearer_key
        )
        if not key or key != _rest_api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key.")

    async def _body(request: Request):
        try:
            return await request.json()
        except Exception:
            return {}

    async def api_run(request: Request):
        await api_auth(request)
        b = await _body(request)
        command = b.get("command", "").strip()
        if not command:
            return JSONResponse({"error": "command is required"}, status_code=400)
        result = await asyncio.to_thread(
            run_command, settings, command=command, timeout_s=b.get("timeout_s")
        )
        return JSONResponse(result)

    async def api_process_list(request: Request):
        await api_auth(request)
        b = await _body(request)
        return JSONResponse(await asyncio.to_thread(process_list, settings, filter=b.get("filter")))

    async def api_kill_process(request: Request):
        await api_auth(request)
        b = await _body(request)
        pid = b.get("pid")
        if not pid:
            return JSONResponse({"error": "pid is required"}, status_code=400)
        return JSONResponse(kill_process(settings, pid=int(pid), signal=b.get("signal", "TERM")))

    async def api_system_info(request: Request):
        await api_auth(request)
        return JSONResponse(await asyncio.to_thread(get_system_info, settings))

    async def api_health_check(request: Request):
        await api_auth(request)
        return JSONResponse(await asyncio.to_thread(get_system_info, settings))

    async def api_jobs_start(request: Request):
        await api_auth(request)
        b = await _body(request)
        command = b.get("command", "").strip()
        if not command:
            return JSONResponse({"error": "command is required"}, status_code=400)
        return JSONResponse(start_background_job(settings, command=command, cwd=b.get("cwd"), env=b.get("env"),
                                                timeout_s=b.get("timeout_s"), no_output_timeout_s=b.get("no_output_timeout_s")))

    async def api_jobs_status(request: Request):
        await api_auth(request)
        b = await _body(request)
        job_id = b.get("job_id", "").strip()
        if not job_id:
            return JSONResponse({"error": "job_id is required"}, status_code=400)
        return JSONResponse(get_job_status(settings, job_id=job_id))

    async def api_jobs_output(request: Request):
        await api_auth(request)
        b = await _body(request)
        job_id = b.get("job_id", "").strip()
        if not job_id:
            return JSONResponse({"error": "job_id is required"}, status_code=400)
        return JSONResponse(get_job_output(settings, job_id=job_id, tail_lines=b.get("tail_lines"),
                                          since_offset=b.get("since_offset"), stream=b.get("stream", "both")))

    async def api_jobs_stop(request: Request):
        await api_auth(request)
        b = await _body(request)
        job_id = b.get("job_id", "").strip()
        if not job_id:
            return JSONResponse({"error": "job_id is required"}, status_code=400)
        return JSONResponse(stop_job(settings, job_id=job_id, signal_name=b.get("signal", "TERM")))

    async def api_jobs_list(request: Request):
        await api_auth(request)
        b = await _body(request)
        return JSONResponse(list_jobs(settings, status_filter=b.get("status_filter")))

    async def api_jobs_wait(request: Request):
        await api_auth(request)
        b = await _body(request)
        job_ids = b.get("job_ids", [])
        if not job_ids:
            return JSONResponse({"error": "job_ids is required"}, status_code=400)
        result = await asyncio.to_thread(
            wait_jobs, settings, job_ids=job_ids, timeout_s=b.get("timeout_s"),
            return_output=b.get("return_output", False)
        )
        return JSONResponse(result)

    async def api_run_parallel(request: Request):
        await api_auth(request)
        b = await _body(request)
        commands = b.get("commands", [])
        if not commands:
            return JSONResponse({"error": "commands is required"}, status_code=400)
        result = await asyncio.to_thread(
            run_commands_parallel, settings, commands=commands, cwd=b.get("cwd"),
            timeout_s=b.get("timeout_s"), return_output=b.get("return_output", True)
        )
        return JSONResponse(result)

    async def api_read_file(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(read_file(settings, path=path, offset=b.get("offset", 0), length=b.get("length")))

    async def api_read_multiple_files(request: Request):
        await api_auth(request)
        b = await _body(request)
        paths = b.get("paths", [])
        if not paths:
            return JSONResponse({"error": "paths is required"}, status_code=400)
        return JSONResponse(read_multiple_files(settings, paths=paths))

    async def api_write_file(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(write_file(settings, path=path, content=b.get("content", "")))

    async def api_write_files_batch(request: Request):
        await api_auth(request)
        b = await _body(request)
        files = b.get("files", [])
        if not files:
            return JSONResponse({"error": "files is required"}, status_code=400)
        return JSONResponse(write_files_batch(settings, files=files))

    async def api_edit_file(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path or "old_string" not in b or "new_string" not in b:
            return JSONResponse({"error": "path, old_string, new_string required"}, status_code=400)
        return JSONResponse(edit_file(settings, path=path, old_string=b["old_string"], new_string=b["new_string"], expected_replacements=b.get("expected_replacements", 1)))

    async def api_move_file(request: Request):
        await api_auth(request)
        b = await _body(request)
        if not b.get("source") or not b.get("destination"):
            return JSONResponse({"error": "source and destination required"}, status_code=400)
        return JSONResponse(move_file(settings, source=b["source"], destination=b["destination"]))

    async def api_copy_file(request: Request):
        await api_auth(request)
        b = await _body(request)
        if not b.get("source") or not b.get("destination"):
            return JSONResponse({"error": "source and destination required"}, status_code=400)
        return JSONResponse(copy_file(settings, source=b["source"], destination=b["destination"]))

    async def api_delete_path(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(delete_path(settings, path=path, recursive=b.get("recursive", False)))

    async def api_list_dir(request: Request):
        await api_auth(request)
        b = await _body(request)
        return JSONResponse(list_directory(settings, path=b.get("path", "/home/ubuntu")))

    async def api_directory_tree(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(directory_tree(settings, path=path, depth=b.get("depth", 3)))

    async def api_create_directory(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(create_directory(settings, path=path))

    async def api_get_file_info(request: Request):
        await api_auth(request)
        b = await _body(request)
        path = b.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        return JSONResponse(get_file_info(settings, path=path))

    async def api_find_files(request: Request):
        await api_auth(request)
        b = await _body(request)
        pattern = b.get("pattern", "").strip()
        if not pattern:
            return JSONResponse({"error": "pattern is required"}, status_code=400)
        return JSONResponse(find_files(settings, pattern=pattern, path=b.get("path", "/home/ubuntu"), file_type=b.get("file_type", "any")))

    async def api_search_files(request: Request):
        await api_auth(request)
        b = await _body(request)
        pattern = b.get("pattern", "").strip()
        if not pattern:
            return JSONResponse({"error": "pattern is required"}, status_code=400)
        result = await asyncio.to_thread(
            search_files, settings, pattern=pattern, path=b.get("path", "/home/ubuntu"),
            include_extensions=b.get("include_extensions")
        )
        return JSONResponse(result)

    async def api_http_request(request: Request):
        await api_auth(request)
        b = await _body(request)
        url = b.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "url is required"}, status_code=400)
        result = await asyncio.to_thread(
            http_request, settings, url=url, method=b.get("method", "GET"),
            headers=b.get("headers"), body=b.get("body")
        )
        return JSONResponse(result)

    # ── WordPress & Zoho CRM proxy endpoints ───────────────────────────────
    import httpx as _httpx

    async def _sse_mcp_call(client, url, payload, extra_headers=None):
        """SSE tabanlı MCP sunucuya istek at, ilk data: yanıtını döndür."""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if extra_headers:
            headers.update(extra_headers)
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("data:"):
                    return __import__("json").loads(line[5:].strip())
        return {}

    _WP_URL = "https://tarkan.cloud/wp-json/stifli-flex-mcp/v1/messages"
    _WP_AUTH = ("bulutarkan", "no77Wf3lSUsYDh7t1pQmBf9b")
    _ZOHO_URL = "https://ckhturkey-912284844.zohomcp.com/mcp/message?key=da4ef101e2d68b5454b71eb1295adede"

    async def api_wp_call(request: Request):
        await api_auth(request)
        b = await _body(request)
        tool = b.get("tool", "").strip()
        if not tool:
            return JSONResponse({"error": "tool is required"}, status_code=400)
        payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                   "params": {"name": tool, "arguments": b.get("params", {})}}
        try:
            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(_WP_URL, auth=_WP_AUTH, json=payload)
            return JSONResponse(resp.json())
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_zoho_call(request: Request):
        await api_auth(request)
        b = await _body(request)
        tool = b.get("tool", "").strip()
        if not tool:
            return JSONResponse({"error": "tool is required"}, status_code=400)
        payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                   "params": {"name": tool, "arguments": b.get("params", {})}}
        try:
            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(_ZOHO_URL, json=payload)
            return JSONResponse(resp.json())
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    _EXA_URL = "https://mcp.exa.ai/mcp"
    _TAVILY_URL = "https://mcp.tavily.com/mcp/?tavilyApiKey=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"
    _N8N_URL = "https://n8n.tarkan.cloud/mcp/server"

    async def api_exa_call(request: Request):
        await api_auth(request)
        b = await _body(request)
        tool = b.get("tool", "").strip()
        if not tool:
            return JSONResponse({"error": "tool is required"}, status_code=400)
        args = b.get("params") or {k: v for k, v in b.items() if k != "tool"}
        payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                   "params": {"name": tool, "arguments": args}}
        try:
            async with _httpx.AsyncClient(timeout=30) as client:
                result = await _sse_mcp_call(client, _EXA_URL, payload)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_tavily_call(request: Request):
        await api_auth(request)
        b = await _body(request)
        tool = b.get("tool", "").strip()
        if not tool:
            return JSONResponse({"error": "tool is required"}, status_code=400)
        args = b.get("params") or {k: v for k, v in b.items() if k != "tool"}
        payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                   "params": {"name": tool, "arguments": args}}
        try:
            async with _httpx.AsyncClient(timeout=30) as client:
                result = await _sse_mcp_call(client, _TAVILY_URL, payload)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def _n8n_dispatch(tool_name: str, arguments: dict):
        sse_hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        async with _httpx.AsyncClient(timeout=60) as client:
            init_resp = await client.post(_N8N_URL, headers=sse_hdrs,
                json={"jsonrpc":"2.0","method":"initialize","id":1,"params":{
                    "protocolVersion":"2024-11-05","capabilities":{},
                    "clientInfo":{"name":"proxy","version":"1"}}})
            session_id = init_resp.headers.get("mcp-session-id","")
            return await _sse_mcp_call(client, _N8N_URL,
                {"jsonrpc":"2.0","method":"tools/call","id":2,
                 "params":{"name":tool_name,"arguments":arguments}},
                {"mcp-session-id": session_id})

    async def api_n8n_call(request: Request):
        """Generic /api/n8n — geriye dönük uyumluluk"""
        await api_auth(request)
        b = await _body(request)
        tool = b.get("tool", "").strip()
        if not tool:
            return JSONResponse({"error": "tool is required"}, status_code=400)
        try:
            result = await _n8n_dispatch(tool, b.get("params", {}))
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_check_mail(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("check_mail_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_send_mail(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("send_mail_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_get_events(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("get_events_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_create_event(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("create_event_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_update_event(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("update_event_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_facebook_report(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("facebook_report_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_check_spreadsheet(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("check_spreadsheet_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_google_analytics(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("google_analytics_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_google_search_console(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("google_search_console_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_news(request: Request):
        await api_auth(request)
        b = await _body(request)
        try:
            result = await _n8n_dispatch("news_api_tool", b)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    app.router.routes += [
        Route("/api/run", api_run, methods=["POST"]),
        Route("/api/process_list", api_process_list, methods=["POST"]),
        Route("/api/kill_process", api_kill_process, methods=["POST"]),
        Route("/api/system_info", api_system_info, methods=["POST"]),
        Route("/api/health_check", api_health_check, methods=["POST"]),
        Route("/api/jobs/start", api_jobs_start, methods=["POST"]),
        Route("/api/jobs/status", api_jobs_status, methods=["POST"]),
        Route("/api/jobs/output", api_jobs_output, methods=["POST"]),
        Route("/api/jobs/stop", api_jobs_stop, methods=["POST"]),
        Route("/api/jobs/list", api_jobs_list, methods=["POST"]),
        Route("/api/jobs/wait", api_jobs_wait, methods=["POST"]),
        Route("/api/run_parallel", api_run_parallel, methods=["POST"]),
        Route("/api/read_file", api_read_file, methods=["POST"]),
        Route("/api/read_multiple_files", api_read_multiple_files, methods=["POST"]),
        Route("/api/write_file", api_write_file, methods=["POST"]),
        Route("/api/write_files_batch", api_write_files_batch, methods=["POST"]),
        Route("/api/edit_file", api_edit_file, methods=["POST"]),
        Route("/api/move_file", api_move_file, methods=["POST"]),
        Route("/api/copy_file", api_copy_file, methods=["POST"]),
        Route("/api/delete_path", api_delete_path, methods=["POST"]),
        Route("/api/list_dir", api_list_dir, methods=["POST"]),
        Route("/api/directory_tree", api_directory_tree, methods=["POST"]),
        Route("/api/create_directory", api_create_directory, methods=["POST"]),
        Route("/api/get_file_info", api_get_file_info, methods=["POST"]),
        Route("/api/find_files", api_find_files, methods=["POST"]),
        Route("/api/search_files", api_search_files, methods=["POST"]),
        Route("/api/http_request", api_http_request, methods=["POST"]),
        Route("/api/wp", api_wp_call, methods=["POST"]),
        Route("/api/zoho", api_zoho_call, methods=["POST"]),
        Route("/api/exa", api_exa_call, methods=["POST"]),
        Route("/api/tavily", api_tavily_call, methods=["POST"]),
        Route("/api/n8n", api_n8n_call, methods=["POST"]),
        Route("/api/n8n/check_mail", api_check_mail, methods=["POST"]),
        Route("/api/n8n/send_mail", api_send_mail, methods=["POST"]),
        Route("/api/n8n/get_events", api_get_events, methods=["POST"]),
        Route("/api/n8n/create_event", api_create_event, methods=["POST"]),
        Route("/api/n8n/update_event", api_update_event, methods=["POST"]),
        Route("/api/n8n/facebook_report", api_facebook_report, methods=["POST"]),
        Route("/api/n8n/check_spreadsheet", api_check_spreadsheet, methods=["POST"]),
        Route("/api/n8n/google_analytics", api_google_analytics, methods=["POST"]),
        Route("/api/n8n/google_search_console", api_google_search_console, methods=["POST"]),
        Route("/api/n8n/news", api_news, methods=["POST"]),
    ]

    return app


app = create_app()
