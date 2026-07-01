from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import ipaddress
import socket
import logging
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import HTTPException, Request, status

BASE_DIR = Path(__file__).resolve().parent
HOME_DIR = Path(os.getenv("SERVER_HOME", str(Path.home())))

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (ValueError, TypeError):
        return default


def _strlist(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    rate_limit_per_minute: int
    default_command_timeout_s: int
    max_command_timeout_s: int
    max_output_chars: int
    shared_dir: Path
    http_allowlist: List[str]
    http_https_only: bool
    http_max_response_bytes: int
    http_timeout_s: int
    base_url: str


def load_settings() -> Settings:
    shared = Path(os.getenv("SHARED_DIR", str(HOME_DIR / "Shared"))).expanduser()
    shared.mkdir(parents=True, exist_ok=True)
    return Settings(
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 1000),
        default_command_timeout_s=_int("DEFAULT_COMMAND_TIMEOUT_S", 120),
        max_command_timeout_s=_int("MAX_COMMAND_TIMEOUT_S", 600),
        max_output_chars=_int("MAX_OUTPUT_CHARS", 100000),
        shared_dir=shared,
        http_allowlist=_strlist("HTTP_ALLOWLIST", ["*"]),
        http_https_only=_bool("HTTP_HTTPS_ONLY", False),
        http_max_response_bytes=_int("HTTP_MAX_RESPONSE_BYTES", 5_000_000),
        http_timeout_s=_int("HTTP_TIMEOUT_S", 60),
        base_url=os.getenv("BASE_URL", "http://localhost:8000"),
    )


def resolve_path(user_path: str) -> Path:
    p = Path(user_path).expanduser()
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        candidate = (HOME_DIR / Path(str(p).lstrip("/"))).resolve()
        if candidate.exists():
            return candidate
        return p.resolve()
    return (HOME_DIR / p).resolve()


class RateLimiter:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._hits: Dict[str, deque] = {}

    def check(self, key: str):
        now = time.time()
        q = self._hits.setdefault(key, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded.")
        q.append(now)


def setup_audit_logger() -> logging.Logger:
    logger = logging.getLogger("cloud_mcp_audit")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    h = logging.FileHandler(BASE_DIR / "audit.log")
    h.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(h)
    logger.propagate = False
    return logger


def truncate(text: str, limit: int) -> Tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    suffix = "\n... [truncated]"
    return text[:max(0, limit - len(suffix))] + suffix, True


_PRIVATE = [ipaddress.ip_network(n) for n in [
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7",
]]


def validate_url(settings: Settings, url: str):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid URL.")
    if settings.http_https_only and parsed.scheme != "https":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only HTTPS allowed.")
    hostname = (parsed.hostname or "").lower()
    if "*" not in settings.http_allowlist:
        if not any(hostname.endswith(a) for a in settings.http_allowlist):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hostname not allowed.")
