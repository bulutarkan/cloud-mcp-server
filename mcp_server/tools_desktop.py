from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp.utilities.types import Image

_HOME = Path.home()
_SCREENSHOT_MAX_BYTES = 600_000
_SCREENSHOT_MAX_DIMENSION = 1600
_DEFAULT_APPS = {
    "chromium": ["/snap/bin/chromium", "chromium", "chromium-browser"],
    "firefox": ["/snap/bin/firefox", "firefox"],
    "terminal": ["xfce4-terminal", "gnome-terminal", "xterm"],
}


def _display() -> str:
    return os.getenv("CLOUD_MCP_DESKTOP_DISPLAY", ":1")


def _xauthority() -> str:
    return os.getenv("CLOUD_MCP_DESKTOP_XAUTHORITY", str(_HOME / ".Xauthority"))


def _xenv() -> Dict[str, str]:
    env = os.environ.copy()
    env["DISPLAY"] = _display()
    env["XAUTHORITY"] = _xauthority()
    runtime = f"/run/user/{os.getuid()}"
    if "DBUS_SESSION_BUS_ADDRESS" not in env and Path(runtime, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    return env


def _which(name: str) -> Optional[str]:
    if name.startswith("/"):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


def _run(args: List[str], timeout_s: float = 15, input_bytes: Optional[bytes] = None,
         check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_bytes,
        capture_output=True,
        env=_xenv(),
        timeout=timeout_s,
        check=check,
    )


def desktop_capabilities() -> Dict[str, Any]:
    commands = {name: _which(name) for name in ("xdotool", "wmctrl", "xclip", "import", "convert", "identify")}
    missing = [name for name, path in commands.items() if not path]
    display_ok = False
    size = None
    error = None
    try:
        proc = _run([commands.get("xdotool") or "xdotool", "getdisplaygeometry"], timeout_s=5)
        if proc.returncode == 0:
            parts = proc.stdout.decode().strip().split()
            if len(parts) == 2:
                size = {"width": int(parts[0]), "height": int(parts[1])}
            display_ok = True
        else:
            error = proc.stderr.decode(errors="replace").strip()
    except Exception as exc:
        error = str(exc)
    chromium = _chromium_executable()
    password_store = _chromium_password_store_status(chromium)
    return {
        "ok": not missing and display_ok,
        "display": _display(),
        "xauthority": _xauthority(),
        "display_ok": display_ok,
        "display_size": size,
        "commands": commands,
        "missing_commands": missing,
        "chromium": chromium,
        "chromium_profile": str(_chromium_profile()),
        "chromium_password_store": password_store,
        "chromium_debug_port": _chromium_debug_port(),
        "chromium_debug_url": f"http://127.0.0.1:{_chromium_debug_port()}",
        "error": error,
    }


def _parse_windows(raw: str) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        # wmctrl -lpGx: id desktop pid x y w h wm_class title
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        try:
            wid, desktop, pid, x, y, width, height = parts[:7]
            wm_class = parts[7]
            title = parts[8] if len(parts) > 8 else ""
            windows.append({
                "window_id": wid.lower(),
                "desktop": int(desktop),
                "pid": int(pid),
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "wm_class": wm_class,
                "title": title,
            })
        except (TypeError, ValueError):
            continue
    return windows


def desktop_windows(title: Optional[str] = None, wm_class: Optional[str] = None) -> Dict[str, Any]:
    proc = _run(["wmctrl", "-lpGx"], timeout_s=8)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode(errors="replace").strip() or "wmctrl failed"}
    windows = _parse_windows(proc.stdout.decode(errors="replace"))
    if title:
        needle = title.casefold()
        windows = [w for w in windows if needle in w["title"].casefold()]
    if wm_class:
        needle = wm_class.casefold()
        windows = [w for w in windows if needle in w["wm_class"].casefold()]
    return {"ok": True, "count": len(windows), "windows": windows}


def _active_window() -> Dict[str, Any]:
    proc = _run(["xdotool", "getactivewindow"], timeout_s=5)
    if proc.returncode != 0:
        return {}
    try:
        dec = int(proc.stdout.decode().strip())
    except ValueError:
        return {}
    target = f"0x{dec:08x}"
    for window in desktop_windows().get("windows", []):
        if int(window["window_id"], 16) == dec:
            return window
    name = _run(["xdotool", "getwindowname", str(dec)], timeout_s=5)
    return {
        "window_id": target,
        "title": name.stdout.decode(errors="replace").strip() if name.returncode == 0 else "",
    }


def _capture_screen(window_id: Optional[str] = None) -> Tuple[Optional[bytes], Optional[str], Dict[str, Any]]:
    import_bin = _which("import")
    convert_bin = _which("convert")
    if not import_bin or not convert_bin:
        return None, "ImageMagick import/convert is required", {}
    fd, raw_path = tempfile.mkstemp(prefix="cloud-mcp-desktop-", suffix=".png")
    os.close(fd)
    out_path = raw_path[:-4] + ".jpg"
    try:
        target = window_id or "root"
        proc = _run([import_bin, "-window", target, raw_path], timeout_s=15)
        if proc.returncode != 0:
            return None, proc.stderr.decode(errors="replace").strip() or "screen capture failed", {}
        quality = 72
        while quality >= 38:
            proc = subprocess.run(
                [convert_bin, raw_path, "-resize", f"{_SCREENSHOT_MAX_DIMENSION}x{_SCREENSHOT_MAX_DIMENSION}>",
                 "-strip", "-quality", str(quality), out_path],
                capture_output=True, timeout=12,
            )
            if proc.returncode != 0:
                return None, proc.stderr.decode(errors="replace").strip() or "screenshot conversion failed", {}
            data = Path(out_path).read_bytes()
            if data and len(data) <= _SCREENSHOT_MAX_BYTES:
                meta: Dict[str, Any] = {}
                identify_bin = _which("identify")
                if identify_bin:
                    ident = subprocess.run([identify_bin, "-format", "%w %h", out_path], capture_output=True, timeout=5)
                    if ident.returncode == 0:
                        try:
                            width, height = (int(v) for v in ident.stdout.decode().strip().split())
                            meta = {"width": width, "height": height}
                        except (TypeError, ValueError):
                            pass
                return data, None, meta
            quality -= 10
        data = Path(out_path).read_bytes() if Path(out_path).exists() else b""
        if not data:
            return None, "screen capture returned an empty image", {}
        return None, f"screenshot exceeds connector safety limit ({len(data)} bytes)", {}
    except Exception as exc:
        return None, f"Could not capture desktop: {exc}", {}
    finally:
        for path in (raw_path, out_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def desktop_observe(include_screenshot: bool = True, window_id: Optional[str] = None) -> Any:
    active = _active_window()
    caps = desktop_capabilities()
    payload: Dict[str, Any] = {
        "ok": bool(caps.get("display_ok")),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "display": _display(),
        "display_size": caps.get("display_size"),
        "active_window": active,
        "windows": desktop_windows().get("windows", []),
        "screenshot": {"requested": bool(include_screenshot), "included_as_image_content": False},
    }
    image_data: Optional[bytes] = None
    if include_screenshot:
        image_data, error, image_meta = _capture_screen(window_id=window_id)
        payload["screenshot"].update(image_meta)
        payload["screenshot"]["included_as_image_content"] = bool(image_data)
        payload["screenshot"]["mime_type"] = "image/jpeg" if image_data else None
        if image_data:
            payload["screenshot"]["bytes"] = len(image_data)
            if not window_id and caps.get("display_size") and image_meta.get("width") and image_meta.get("height"):
                payload["screenshot"]["display_coordinate_scale"] = {
                    "x": round(caps["display_size"]["width"] / image_meta["width"], 6),
                    "y": round(caps["display_size"]["height"] / image_meta["height"], 6),
                }
                payload["screenshot"]["coordinate_note"] = (
                    "desktop_click uses native display pixels. Multiply screenshot-image coordinates "
                    "by display_coordinate_scale before clicking."
                )
        if error:
            payload["screenshot"]["error"] = error
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if image_data:
        return [text, Image(data=image_data, format="jpeg")]
    return text


def desktop_focus(window_id: Optional[str] = None, title: Optional[str] = None,
                  wm_class: Optional[str] = None) -> Dict[str, Any]:
    windows = desktop_windows(title=title, wm_class=wm_class).get("windows", [])
    target = window_id
    if not target:
        if not windows:
            return {"ok": False, "error": "No matching window found"}
        target = windows[-1]["window_id"]
    proc = _run(["wmctrl", "-ia", target], timeout_s=8)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode(errors="replace").strip() or "Could not focus window"}
    time.sleep(0.12)
    return {"ok": True, "window_id": target, "active_window": _active_window()}


def desktop_move_mouse(x: int, y: int) -> Dict[str, Any]:
    x, y = int(x), int(y)
    proc = _run(["xdotool", "mousemove", str(x), str(y)], timeout_s=8)
    return {"ok": proc.returncode == 0, "x": x, "y": y,
            "error": proc.stderr.decode(errors="replace").strip() or None}


def desktop_click(x: int, y: int, button: str = "left", click_count: int = 1) -> Dict[str, Any]:
    buttons = {"left": "1", "middle": "2", "right": "3"}
    if button not in buttons:
        return {"ok": False, "error": "button must be left, middle, or right"}
    click_count = max(1, min(int(click_count), 3))
    args = ["xdotool", "mousemove", str(int(x)), str(int(y)), "click", "--repeat", str(click_count), buttons[button]]
    proc = _run(args, timeout_s=8)
    return {"ok": proc.returncode == 0, "x": int(x), "y": int(y), "button": button,
            "click_count": click_count, "error": proc.stderr.decode(errors="replace").strip() or None}


def _clipboard_read() -> Tuple[bool, bytes]:
    try:
        proc = _run(["xclip", "-selection", "clipboard", "-o"], timeout_s=2)
        return proc.returncode == 0, proc.stdout
    except Exception:
        return False, b""


def _clipboard_write(data: bytes) -> bool:
    # xclip forks a background selection owner. Do not capture stdout/stderr here:
    # the child inherits those pipes and subprocess.run() would wait for EOF.
    try:
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-i"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_xenv(),
        )
        proc.communicate(input=data, timeout=3)
        return proc.returncode == 0
    except Exception:
        return False


def desktop_type(text: str, clear: bool = False, restore_clipboard: bool = True) -> Dict[str, Any]:
    if len(text) > 50_000:
        return {"ok": False, "error": "text is limited to 50,000 characters"}
    had_clipboard, previous = _clipboard_read()
    if not _clipboard_write(text.encode("utf-8")):
        return {"ok": False, "error": "Could not write X11 clipboard"}
    try:
        if clear:
            _run(["xdotool", "key", "--clearmodifiers", "ctrl+a"], timeout_s=5)
        proc = _run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout_s=8)
        time.sleep(0.12)
        return {"ok": proc.returncode == 0, "characters": len(text), "clear": bool(clear),
                "error": proc.stderr.decode(errors="replace").strip() or None}
    finally:
        if restore_clipboard and had_clipboard:
            _clipboard_write(previous)


def desktop_key(keys: str, repeat: int = 1) -> Dict[str, Any]:
    keys = keys.strip()
    if not keys or len(keys) > 120 or any(c in keys for c in "\n\r\x00"):
        return {"ok": False, "error": "Invalid key sequence"}
    repeat = max(1, min(int(repeat), 20))
    proc = _run(["xdotool", "key", "--clearmodifiers", "--repeat", str(repeat), keys], timeout_s=10)
    return {"ok": proc.returncode == 0, "keys": keys, "repeat": repeat,
            "error": proc.stderr.decode(errors="replace").strip() or None}


def desktop_scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None,
                   horizontal: bool = False) -> Dict[str, Any]:
    amount = max(-50, min(50, int(amount)))
    if amount == 0:
        return {"ok": True, "amount": 0}
    if x is not None and y is not None:
        moved = desktop_move_mouse(int(x), int(y))
        if not moved.get("ok"):
            return moved
    if horizontal:
        button = "7" if amount > 0 else "6"
    else:
        button = "5" if amount > 0 else "4"
    proc = _run(["xdotool", "click", "--repeat", str(abs(amount)), button], timeout_s=10)
    return {"ok": proc.returncode == 0, "amount": amount, "horizontal": horizontal,
            "error": proc.stderr.decode(errors="replace").strip() or None}



def desktop_drag(start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 500) -> Dict[str, Any]:
    duration_ms = max(50, min(int(duration_ms), 5000))
    sx, sy, ex, ey = map(int, (start_x, start_y, end_x, end_y))
    down = _run(["xdotool", "mousemove", str(sx), str(sy), "mousedown", "1"], timeout_s=8)
    if down.returncode != 0:
        return {"ok": False, "error": down.stderr.decode(errors="replace").strip() or "Could not start drag"}
    steps = max(2, min(30, duration_ms // 40))
    error: Optional[str] = None
    try:
        for i in range(1, steps + 1):
            x = round(sx + (ex - sx) * i / steps)
            y = round(sy + (ey - sy) * i / steps)
            proc = _run(["xdotool", "mousemove", str(x), str(y)], timeout_s=5)
            if proc.returncode != 0:
                error = proc.stderr.decode(errors="replace").strip() or "Mouse movement failed during drag"
                break
            time.sleep(duration_ms / steps / 1000.0)
    finally:
        up = _run(["xdotool", "mouseup", "1"], timeout_s=5)
        if up.returncode != 0 and not error:
            error = up.stderr.decode(errors="replace").strip() or "Could not release mouse button"
    return {
        "ok": error is None,
        "start": {"x": sx, "y": sy},
        "end": {"x": ex, "y": ey},
        "duration_ms": duration_ms,
        "error": error,
    }


def desktop_act(actions: List[Dict[str, Any]], return_state: bool = True,
                include_screenshot: bool = True, stop_on_error: bool = True) -> Any:
    if not isinstance(actions, list) or not actions:
        return {"ok": False, "error": "actions must be a non-empty list"}
    if len(actions) > 30:
        return {"ok": False, "error": "a maximum of 30 actions is allowed per batch"}
    started = time.monotonic()
    results: List[Dict[str, Any]] = []
    for index, action in enumerate(actions):
        if time.monotonic() - started > 60:
            results.append({"index": index, "ok": False, "error": "60-second desktop_act safety budget exceeded"})
            break
        if not isinstance(action, dict):
            result = {"ok": False, "error": "action must be an object"}
            results.append({"index": index, "type": None, **result})
            if stop_on_error:
                break
            continue
        kind = str(action.get("type", "")).strip().casefold()
        try:
            if kind == "move":
                result = desktop_move_mouse(action["x"], action["y"])
            elif kind in {"click", "double_click"}:
                result = desktop_click(
                    action["x"], action["y"],
                    button=str(action.get("button", "left")),
                    click_count=2 if kind == "double_click" else int(action.get("click_count", 1)),
                )
            elif kind == "type":
                result = desktop_type(
                    str(action.get("text", "")),
                    clear=bool(action.get("clear", False)),
                    restore_clipboard=bool(action.get("restore_clipboard", True)),
                )
            elif kind in {"key", "shortcut"}:
                result = desktop_key(str(action.get("keys", "")), repeat=int(action.get("repeat", 1)))
            elif kind == "scroll":
                result = desktop_scroll(
                    int(action.get("amount", 0)),
                    x=action.get("x"), y=action.get("y"),
                    horizontal=bool(action.get("horizontal", False)),
                )
            elif kind == "focus":
                result = desktop_focus(
                    window_id=action.get("window_id"), title=action.get("title"), wm_class=action.get("wm_class")
                )
            elif kind == "drag":
                result = desktop_drag(
                    action["start_x"], action["start_y"], action["end_x"], action["end_y"],
                    duration_ms=int(action.get("duration_ms", 500)),
                )
            elif kind == "sleep":
                seconds = max(0.0, min(float(action.get("seconds", 0.25)), 3.0))
                time.sleep(seconds)
                result = {"ok": True, "seconds": seconds}
            else:
                result = {"ok": False, "error": f"Unsupported action type: {kind or '<empty>'}"}
        except (KeyError, TypeError, ValueError) as exc:
            result = {"ok": False, "error": f"Invalid {kind or 'action'} arguments: {exc}"}
        results.append({"index": index, "type": kind, **result})
        if stop_on_error and not result.get("ok"):
            break
    report = {
        "ok": bool(results) and all(r.get("ok") for r in results),
        "actions_requested": len(actions),
        "actions_completed": len(results),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
    }
    if not return_state:
        return report
    observation = desktop_observe(include_screenshot=include_screenshot)
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    if isinstance(observation, list):
        return [report_text, *observation]
    return [report_text, observation]

def _chromium_executable() -> Optional[str]:
    configured = os.getenv("CLOUD_MCP_CHROMIUM_BIN", "").strip()
    candidates = [configured] if configured else []
    candidates += ["/snap/bin/chromium", "chromium", "chromium-browser"]
    for candidate in candidates:
        if candidate and (path := _which(candidate)):
            return path
    return None



def _chromium_password_store_status(executable: Optional[str] = None) -> Dict[str, Any]:
    executable = executable or _chromium_executable()
    if not executable:
        return {"mode": "unavailable", "encrypted": None}
    if not executable.startswith("/snap/"):
        return {
            "mode": "browser_default",
            "encrypted": None,
            "note": "Non-Snap Chromium password-store security depends on the desktop keyring configuration.",
        }
    snap_bin = _which("snap")
    if not snap_bin:
        return {"mode": "unknown", "encrypted": None}
    try:
        proc = subprocess.run([snap_bin, "connections", "chromium"], capture_output=True, timeout=5)
        text = proc.stdout.decode(errors="replace")
        connected = False
        for line in text.splitlines():
            if line.startswith("password-manager-service"):
                parts = line.split()
                # Interface, Plug, Slot, Notes. A '-' slot means disconnected.
                connected = len(parts) >= 3 and parts[2] != "-"
                break
        if connected:
            return {
                "mode": "desktop_keyring",
                "encrypted": True,
                "note": "Chromium Snap password-manager-service is connected; the desktop secret service can be used.",
            }
        return {
            "mode": "basic",
            "encrypted": False,
            "warning": (
                "Chromium Snap password-manager-service is not connected, so its launcher selects "
                "the basic non-encrypted password store. Cookies/site sessions still persist, but "
                "do not rely on Chromium's saved-password feature until a desktop keyring/secret service is configured."
            ),
        }
    except Exception as exc:
        return {"mode": "unknown", "encrypted": None, "error": str(exc)}

def _chromium_debug_port() -> int:
    try:
        return int(os.getenv("CLOUD_MCP_CHROMIUM_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def _chromium_profile() -> Path:
    configured = os.getenv("CLOUD_MCP_CHROMIUM_PROFILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/snap/bin/chromium").exists():
        return _HOME / "snap/chromium/common/cloud-mcp-profile"
    return _HOME / ".local/share/cloud-mcp/chromium-profile"


def _pid_command_line(pid: int) -> str:
    try:
        data = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return data.replace(b"\x00", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return ""


def _chromium_windows() -> List[Dict[str, Any]]:
    profile_arg = f"--user-data-dir={_chromium_profile()}"
    windows = desktop_windows().get("windows", [])
    owned: List[Dict[str, Any]] = []
    for window in windows:
        if "chromium" not in window.get("wm_class", "").casefold():
            continue
        if profile_arg in _pid_command_line(window.get("pid", 0)):
            owned.append(window)
    return owned


def chromium_launch(url: str = "about:blank", new_window: bool = True) -> Dict[str, Any]:
    executable = _chromium_executable()
    if not executable:
        return {"ok": False, "error": "Chromium executable not found"}
    if executable.startswith("/snap/") and not Path("/tmp/snap-private-tmp").is_dir():
        return {
            "ok": False,
            "error": "Snap runtime directory /tmp/snap-private-tmp is missing",
            "repair_hint": "sudo mkdir -p /tmp/snap-private-tmp && sudo chown root:root /tmp/snap-private-tmp && sudo chmod 0700 /tmp/snap-private-tmp",
        }
    profile = _chromium_profile()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        executable,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={_chromium_debug_port()}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if new_window:
        args.append("--new-window")
    args.append(url)
    before = {w["window_id"] for w in _chromium_windows()}
    try:
        proc = subprocess.Popen(args, env=_xenv(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception as exc:
        return {"ok": False, "error": f"Could not launch Chromium: {exc}"}
    deadline = time.monotonic() + 15
    windows: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        time.sleep(0.25)
        windows = _chromium_windows()
        created = [w for w in windows if w["window_id"] not in before]
        if created or (not new_window and windows) or (not before and windows):
            break
        # When another Chromium process already owns the profile, this launcher
        # may exit after forwarding the request. Keep waiting for the new X11 window.
        if proc.poll() is not None and not before:
            return {"ok": False, "error": f"Chromium exited early with code {proc.returncode}", "pid": proc.pid}
    if not windows:
        return {"ok": False, "error": "Chromium did not create a visible X11 window", "pid": proc.pid}
    if new_window and before and not created:
        return {"ok": False, "error": "Chromium did not create the requested new visible window", "pid": proc.pid}
    target = (created or windows)[-1]
    desktop_focus(window_id=target["window_id"])
    return {
        "ok": True,
        "pid": proc.pid,
        "url": url,
        "profile": str(profile),
        "persistent_profile": True,
        "created_new_window": bool(created),
        "debug_port": _chromium_debug_port(),
        "semantic_browser_ready": True,
        "window": target,
    }


def chromium_open_url(url: str, new_window: bool = False) -> Dict[str, Any]:
    windows = _chromium_windows()
    if not windows or new_window:
        return chromium_launch(url=url, new_window=True)
    target = windows[-1]
    focused = desktop_focus(window_id=target["window_id"])
    if not focused.get("ok"):
        return focused
    key = desktop_key("ctrl+l")
    if not key.get("ok"):
        return key
    typed = desktop_type(url, clear=False)
    if not typed.get("ok"):
        return typed
    entered = desktop_key("Return")
    if not entered.get("ok"):
        return entered
    return {"ok": True, "url": url, "window_id": target["window_id"], "via": "real_gui_address_bar"}


def chromium_close(window_id: Optional[str] = None, all_windows: bool = False) -> Dict[str, Any]:
    windows = _chromium_windows()
    if not windows:
        return {"ok": True, "closed": False, "reason": "No Chromium window is open"}
    if window_id:
        targets = [w for w in windows if w["window_id"].lower() == window_id.lower()]
    elif all_windows:
        targets = windows
    else:
        active = _active_window()
        active_id = active.get("window_id", "").lower()
        active_match = [w for w in windows if w["window_id"].lower() == active_id]
        targets = active_match or [windows[-1]]
    if not targets:
        return {"ok": False, "error": "Chromium window not found"}
    closed: List[str] = []
    for window in targets:
        proc = _run(["wmctrl", "-ic", window["window_id"]], timeout_s=5)
        if proc.returncode == 0:
            closed.append(window["window_id"])
    return {"ok": len(closed) == len(targets), "closed": closed, "profile_preserved": str(_chromium_profile())}


def desktop_launch(app: str, url_or_arg: Optional[str] = None) -> Dict[str, Any]:
    app = app.strip().casefold()
    if app == "chromium":
        return chromium_launch(url=url_or_arg or "about:blank")
    if app not in _DEFAULT_APPS:
        return {"ok": False, "error": f"Unsupported app. Allowed: {', '.join(sorted(_DEFAULT_APPS))}"}
    candidates = _DEFAULT_APPS[app]
    executable = next((_which(candidate) for candidate in candidates if _which(candidate)), None)
    if not executable:
        return {"ok": False, "error": f"{app} executable not found"}
    args = [executable]
    if url_or_arg:
        args.append(url_or_arg)
    try:
        proc = subprocess.Popen(args, env=_xenv(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        return {"ok": True, "app": app, "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
