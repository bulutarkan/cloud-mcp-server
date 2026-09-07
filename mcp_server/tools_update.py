from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

DEFAULT_REPO = Path("/home/ubuntu/Projects/cloud-mcp-server")
DEFAULT_RUNTIME = Path("/home/ubuntu/cloud-mcp")


def _repo() -> Path:
    return Path(os.getenv("CLOUD_MCP_DEPLOY_REPO", str(DEFAULT_REPO))).expanduser().resolve()


def _runtime() -> Path:
    return Path(os.getenv("CLOUD_MCP_DEPLOY_RUNTIME", str(DEFAULT_RUNTIME))).expanduser().resolve()


def _run(command: List[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _sha(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() and path.is_file() else None


def _managed_relpaths(repo: Path) -> List[Path]:
    files = [p.relative_to(repo) for p in sorted((repo / "mcp_server").glob("*.py"))]
    req = Path("mcp_server/requirements.txt")
    if (repo / req).exists():
        files.append(req)
    return files


def _runtime_diff(repo: Path, runtime: Path) -> List[Dict[str, Any]]:
    diffs = []
    for rel in _managed_relpaths(repo):
        src_hash = _sha(repo / rel)
        runtime_hash = _sha(runtime / rel)
        if src_hash != runtime_hash:
            diffs.append({"path": rel.as_posix(), "repo_sha256": src_hash, "runtime_sha256": runtime_hash})
    return diffs


def cloud_mcp_self_deploy(check_only: bool = True, branch: str = "main") -> Dict[str, Any]:
    repo = _repo()
    runtime = _runtime()
    if not (repo / ".git").exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Deployment repo not found: {repo}")
    if not runtime.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runtime not found: {runtime}")

    fetch = _run(["git", "fetch", "origin", branch], repo, timeout=120)
    if fetch.returncode != 0:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"git fetch failed: {fetch.stderr[-500:]}")
    status_result = _run(["git", "status", "--porcelain"], repo)
    head_result = _run(["git", "rev-parse", "HEAD"], repo)
    origin_result = _run(["git", "rev-parse", f"origin/{branch}"], repo)
    repo_clean = not status_result.stdout.strip()
    info = {
        "ok": True,
        "check_only": bool(check_only),
        "repo": str(repo),
        "runtime": str(runtime),
        "branch": branch,
        "repo_clean": repo_clean,
        "head": head_result.stdout.strip(),
        "origin": origin_result.stdout.strip(),
        "head_matches_origin": head_result.stdout.strip() == origin_result.stdout.strip(),
        "runtime_differences": _runtime_diff(repo, runtime),
    }
    if check_only:
        return info
    if not repo_clean:
        raise HTTPException(status.HTTP_409_CONFLICT, "Refusing self-deploy because the distribution repository has uncommitted changes.")

    # Strong preflight: syntax, complete unit suite, dependency consistency.
    python = runtime / ".venv" / "bin" / "python"
    pip = runtime / ".venv" / "bin" / "pip"
    if not python.exists():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Runtime Python not found: {python}")
    compile_result = _run([str(python), "-m", "compileall", "-q", "mcp_server"], repo, timeout=120)
    if compile_result.returncode != 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"compileall failed: {compile_result.stderr[-1000:]}")
    tests = _run([str(python), "-m", "unittest", "discover", "-s", "tests", "-v"], repo, timeout=300)
    if tests.returncode != 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tests failed: {(tests.stdout + tests.stderr)[-3000:]}")
    if pip.exists():
        pip_check = _run([str(pip), "check"], runtime, timeout=120)
        if pip_check.returncode != 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"pip check failed: {pip_check.stdout[-1000:]} {pip_check.stderr[-1000:]}")

    deployment_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    deployment_dir = Path.home() / ".cloud-mcp" / "deployments"
    deployment_dir.mkdir(parents=True, exist_ok=True)
    result_path = deployment_dir / f"{deployment_id}.json"
    log_path = deployment_dir / f"{deployment_id}.log"
    helper = runtime / "mcp_server" / "self_deploy_worker.py"
    if not helper.exists():
        # Bootstrap case: execute the helper from the repo before it exists in runtime.
        helper = repo / "mcp_server" / "self_deploy_worker.py"
    log = log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        [
            str(python), str(helper),
            "--repo", str(repo),
            "--runtime", str(runtime),
            "--service", "cloud-mcp.service",
            "--deployment-id", deployment_id,
            "--result", str(result_path),
        ],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    return {
        **info,
        "action": "deploy_started",
        "deployment_id": deployment_id,
        "result_path": str(result_path),
        "log_path": str(log_path),
        "note": "Detached deploy helper performs copy, dependency install, restart, health check, and automatic rollback on failure.",
    }


def cloud_mcp_deploy_status(deployment_id: str) -> Dict[str, Any]:
    key = str(deployment_id or "").strip()
    if not re_safe_id(key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid deployment_id.")
    path = Path.home() / ".cloud-mcp" / "deployments" / f"{key}.json"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Deployment not found: {key}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Cannot read deployment status: {exc}") from exc
    return {"ok": True, **payload, "result_path": str(path)}


def re_safe_id(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(ch.isalnum() or ch in "-_" for ch in value)
