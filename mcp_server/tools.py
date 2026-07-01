from __future__ import annotations
import os, subprocess, time, shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import HTTPException, status
from .security import Settings, resolve_path, truncate, validate_url
import httpx


HOME_DIR = Path(os.getenv("SERVER_HOME", str(Path.home())))


# ── Terminal ──────────────────────────────────────────────────────────────────
def run_command(settings: Settings, command: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
    timeout = min(max(1, timeout_s or settings.default_command_timeout_s), settings.max_command_timeout_s)
    env = os.environ.copy()
    env.update({"HOME": str(HOME_DIR), "USER": os.getenv("USER", "ubuntu"), "LANG": "en_US.UTF-8"})
    start = time.perf_counter()
    try:
        proc = subprocess.run(["/bin/bash", "-lc", command], capture_output=True,
                              text=True, timeout=timeout, env=env, cwd=str(HOME_DIR))
        ms = int((time.perf_counter() - start) * 1000)
        stdout, _ = truncate(proc.stdout or "", settings.max_output_chars)
        stderr, _ = truncate(proc.stderr or "", settings.max_output_chars)
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": stdout, "stderr": stderr, "duration_ms": ms}
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, f"Timed out after {timeout}s") from e


def process_list(settings: Settings, filter: Optional[str] = None) -> Dict[str, Any]:
    result = run_command(settings, "ps aux")
    if filter and result["ok"]:
        lines = result["stdout"].splitlines()
        matched = [l for l in lines[1:] if filter.lower() in l.lower()]
        result["stdout"] = "\n".join([lines[0]] + matched)
    return result


def kill_process(settings: Settings, pid: int, signal: str = "TERM") -> Dict[str, Any]:
    if signal.upper() not in {"TERM", "KILL", "HUP", "INT"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signal.")
    return run_command(settings, f"kill -{signal.upper()} {pid}")


def get_system_info(settings: Settings) -> Dict[str, Any]:
    return run_command(settings, """
echo '=== HOSTNAME ===' && hostname
echo '=== UPTIME ===' && uptime
echo '=== CPU ===' && nproc && cat /proc/cpuinfo | grep 'model name' | head -1
echo '=== MEMORY ===' && free -h
echo '=== DISK ===' && df -h /
echo '=== NETWORK ===' && ip addr show | grep 'inet ' | grep -v 127
""")


# ── Files ─────────────────────────────────────────────────────────────────────
MAX_READ = 200_000


def write_file(settings: Settings, path: str, content: str) -> Dict[str, Any]:
    t = resolve_path(path)
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(t), "bytes": len(content.encode())}


def write_files_batch(settings: Settings, files: List[Dict[str, str]]) -> Dict[str, Any]:
    written = []
    for item in files:
        p = resolve_path(item["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(item.get("content", ""), encoding="utf-8")
        written.append(str(p))
    return {"ok": True, "written": written}


def read_file(settings: Settings, path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists() or not t.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {path}")
    content = t.read_text(encoding="utf-8", errors="replace")
    if offset or length is not None:
        lines = content.splitlines(keepends=True)
        content = "".join(lines[offset: (offset + length) if length else None])
    bounded, truncated = truncate(content, MAX_READ)
    return {"ok": True, "path": str(t), "content": bounded, "truncated": truncated}


def read_multiple_files(settings: Settings, paths: List[str]) -> Dict[str, Any]:
    results = []
    for path in paths:
        try:
            t = resolve_path(path)
            c, _ = truncate(t.read_text(encoding="utf-8", errors="replace"), 50_000)
            results.append({"path": path, "content": c, "status": "ok"})
        except Exception as e:
            results.append({"path": path, "error": str(e), "status": "error"})
    return {"ok": True, "files": results}


def edit_file(settings: Settings, path: str, old_string: str, new_string: str,
              expected_replacements: int = 1) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {path}")
    content = t.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "old_string not found.")
    if count != expected_replacements:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Found {count} occurrences, expected {expected_replacements}.")
    t.write_text(content.replace(old_string, new_string), encoding="utf-8")
    return {"ok": True, "path": str(t), "replacements": count}


def list_directory(settings: Settings, path: str) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists() or not t.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not a directory: {path}")
    entries = [{"name": e.name, "type": "directory" if e.is_dir() else "file",
                "size": e.stat().st_size if e.is_file() else None} for e in sorted(t.iterdir())]
    return {"ok": True, "path": str(t), "count": len(entries), "entries": entries}


def directory_tree(settings: Settings, path: str, depth: int = 3) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {path}")
    def build(p: Path, d: int) -> Dict:
        node = {"name": p.name, "type": "directory", "children": []}
        if d <= 0:
            return node
        try:
            for e in sorted(p.iterdir()):
                node["children"].append(build(e, d-1) if e.is_dir() else
                                        {"name": e.name, "type": "file", "size": e.stat().st_size})
        except PermissionError:
            pass
        return node
    return {"ok": True, "path": str(t), "tree": build(t, depth)}


def create_directory(settings: Settings, path: str) -> Dict[str, Any]:
    t = resolve_path(path)
    t.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(t)}


def move_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    s, d = resolve_path(source), resolve_path(destination)
    if not s.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {source}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return {"ok": True, "source": str(s), "destination": str(d)}


def copy_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    s, d = resolve_path(source), resolve_path(destination)
    if not s.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {source}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(s), str(d)) if s.is_dir() else shutil.copy2(str(s), str(d))
    return {"ok": True, "source": str(s), "destination": str(d)}


def delete_path(settings: Settings, path: str, recursive: bool = False) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {path}")
    if t.is_dir():
        if not recursive:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set recursive=true for directories.")
        shutil.rmtree(str(t))
    else:
        t.unlink()
    return {"ok": True, "deleted": str(t)}


def get_file_info(settings: Settings, path: str) -> Dict[str, Any]:
    t = resolve_path(path)
    if not t.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not found: {path}")
    s = t.stat()
    return {"ok": True, "path": str(t), "type": "directory" if t.is_dir() else "file",
            "size": s.st_size, "modified": time.ctime(s.st_mtime)}


def find_files(settings: Settings, pattern: str, path: str = "/home/ubuntu",
               file_type: str = "any") -> Dict[str, Any]:
    root = resolve_path(path)
    results = []
    for m in sorted(root.rglob(pattern)):
        if file_type == "file" and not m.is_file(): continue
        if file_type == "dir" and not m.is_dir(): continue
        results.append({"path": str(m), "type": "directory" if m.is_dir() else "file"})
        if len(results) >= 500: break
    return {"ok": True, "count": len(results), "results": results}


def search_files(settings: Settings, pattern: str, path: str = "/home/ubuntu",
                 include_extensions: Optional[List[str]] = None) -> Dict[str, Any]:
    root = resolve_path(path)
    cmd = ["grep", "-rnI", "-i", pattern, str(root)]
    if include_extensions:
        for ext in include_extensions:
            cmd.extend(["--include", f"*.{ext.lstrip('.')}"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        stdout, truncated = truncate(proc.stdout, 50_000)
        return {"ok": True, "results": stdout, "truncated": truncated}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Search timed out."}


# ── HTTP ──────────────────────────────────────────────────────────────────────
def http_request(settings: Settings, url: str, method: str = "GET",
                 headers: Optional[Dict[str, str]] = None,
                 body: Optional[str] = None) -> Dict[str, Any]:
    validate_url(settings, url)
    method = method.upper()
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=settings.http_timeout_s, follow_redirects=True) as client:
            resp = client.request(method, url, headers=headers or {},
                                  content=body.encode() if body else None)
        text, _ = truncate(resp.text, settings.max_output_chars)
        return {"ok": 200 <= resp.status_code < 400, "status": resp.status_code,
                "text": text, "duration_ms": int((time.perf_counter() - start) * 1000)}
    except httpx.RequestError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
