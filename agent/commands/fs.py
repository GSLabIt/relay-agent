"""Filesystem commands executed locally by the agent."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FsCommands:
    def write_text(self, params: dict) -> dict:
        """Write a UTF-8 text file, creating parent directories as needed."""
        path = Path(params["path"])
        content: str = params["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("fs.write_text: %d chars → %s", len(content), path)
        return {}

    def write_bytes(self, params: dict) -> dict:
        """Write or append base64-encoded binary data to a file.

        params:
          path     — absolute path on the agent host
          data_b64 — base64-encoded bytes to write/append
          append   — if true, append to existing file (default: false)
        """
        path = Path(params["path"])
        data = base64.b64decode(params["data_b64"])
        append: bool = params.get("append", False)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if append else "wb"
        with open(path, mode) as f:
            f.write(data)
        logger.info("fs.write_bytes: %d bytes → %s (append=%s)", len(data), path, append)
        return {}

    def mkdir(self, params: dict) -> dict:
        """Create a directory and all parents."""
        import os

        path = params["path"]
        mode = params.get("mode", 0o755)
        os.makedirs(path, mode=mode, exist_ok=True)
        return {}
