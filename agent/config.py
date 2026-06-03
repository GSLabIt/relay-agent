"""Agent configuration from environment variables."""

from __future__ import annotations

import os


class Config:
    gateway_url: str  # wss://api.platform.com/agent/ws
    token: str  # plain agent token
    data_root_path: (
        str  # host path where tenant data lives (e.g. /data/tenants)
    )
    reconnect_max_backoff: int
    command_timeout: float
    log_level: str

    def __init__(self) -> None:
        self.gateway_url = _require("GATEWAY_URL")
        self.token = _require("TOKEN")
        self.data_root_path = os.environ.get(
            "DATA_ROOT_PATH", "/data/tenants"
        ).rstrip("/")
        self.reconnect_max_backoff = int(
            os.environ.get("RECONNECT_MAX_BACKOFF", "60")
        )
        self.command_timeout = float(os.environ.get("COMMAND_TIMEOUT", "120"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        # Set SSL_VERIFY=false in dev when using self-signed certificates
        self.ssl_verify = (
            os.environ.get("SSL_VERIFY", "true").lower() != "false"
        )


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return val
