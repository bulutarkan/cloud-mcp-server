from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


def managed_relpaths(repo: Path) -> List[Path]:
    files = [p.relative_to(repo) for p in sorted((repo / "mcp_server").glob("*.py"))]
    req = Path("mcp_server/requirements.txt")
    if (repo / req).exists():
        files.append(req)
    return files


def copy_managed(repo: Path, runtime: Path, backup: Path) -> Dict[str, List[str]]:
    copied: List[str] = []
    existed: List[str] = []
    for rel in managed_relpaths(repo):
        src = repo / rel
        dst = runtime / rel
        bak = backup / rel
        if dst.exists():
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bak)
            existed.append(rel.as_posix())
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel.as_posix())
    return {"copied": copied, "existed": existed}


def rollback(runtime: Path, backup: Path, manifest: Dict[str, List[str]]) -> None:
    existed = set(manifest.get("existed", []))
    for raw in manifest.get("copied", []):
        rel = Path(raw)
        dst = runtime / rel
        bak = backup / rel
        if raw in existed and bak.exists():
            shutil.copy2(bak, dst)
        elif dst.exists():
            dst.unlink()


def run(cmd: List[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


def health_ok(url: str, attempts: int = 30) -> bool:
    import urllib.request
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def write_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--service", default="cloud-mcp.service")
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    runtime = Path(args.runtime).resolve()
    result_path = Path(args.result).resolve()
    backup = Path.home() / ".cloud-mcp" / "deploy-backups" / args.deployment_id
    backup.mkdir(parents=True, exist_ok=True)
    payload = {
        "deployment_id": args.deployment_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "runtime": str(runtime),
        "service": args.service,
        "status": "running",
    }
    write_result(result_path, payload)

    manifest: Dict[str, List[str]] = {"copied": [], "existed": []}
    try:
        manifest = copy_managed(repo, runtime, backup)
        payload["manifest"] = manifest
        pip = runtime / ".venv" / "bin" / "pip"
        requirements = runtime / "mcp_server" / "requirements.txt"
        if pip.exists() and requirements.exists():
            install = run([str(pip), "install", "-r", str(requirements)], cwd=runtime, timeout=600)
            payload["pip_exit_code"] = install.returncode
            payload["pip_tail"] = (install.stdout + "\n" + install.stderr)[-4000:]
            if install.returncode != 0:
                raise RuntimeError("dependency installation failed")

        restart = run(["sudo", "systemctl", "restart", args.service], timeout=60)
        payload["restart_exit_code"] = restart.returncode
        if restart.returncode != 0:
            raise RuntimeError(f"service restart failed: {restart.stderr[-500:]}")
        if not health_ok(args.health_url):
            raise RuntimeError("health check failed after deployment")

        payload["status"] = "success"
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_result(result_path, payload)
        return 0
    except Exception as exc:
        payload["error"] = str(exc)
        payload["status"] = "rolling_back"
        write_result(result_path, payload)
        try:
            rollback(runtime, backup, manifest)
            run(["sudo", "systemctl", "restart", args.service], timeout=60)
            payload["rollback_health_ok"] = health_ok(args.health_url)
            payload["status"] = "rolled_back" if payload["rollback_health_ok"] else "rollback_failed"
        except Exception as rollback_exc:
            payload["rollback_error"] = str(rollback_exc)
            payload["status"] = "rollback_failed"
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_result(result_path, payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
