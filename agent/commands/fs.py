"""Filesystem commands executed locally by the agent."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class FsCommands:
    def __init__(self, data_root_path: str) -> None:
        self._data_root = Path(data_root_path).resolve()

    def _safe_path(self, raw: str) -> Path:
        """Resolve *raw* and verify it stays within DATA_ROOT_PATH.

        Raises ValueError for any path that would escape the data root,
        preventing path-traversal writes to arbitrary host locations.
        """
        p = Path(raw).resolve()
        root = self._data_root
        if p != root and not str(p).startswith(str(root) + "/"):
            raise ValueError(f"Path {raw!r} is outside DATA_ROOT_PATH {root!r}")
        return p

    def write_text(self, params: dict) -> dict:
        """Write a UTF-8 text file, creating parent directories as needed."""
        path = self._safe_path(params["path"])
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
        path = self._safe_path(params["path"])
        data = base64.b64decode(params["data_b64"])
        append: bool = params.get("append", False)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if append else "wb"
        with open(path, mode) as f:
            f.write(data)
        logger.info(
            "fs.write_bytes: %d bytes → %s (append=%s)",
            len(data),
            path,
            append,
        )
        return {}

    def mkdir(self, params: dict) -> dict:
        """Create a directory and all parents."""
        path = self._safe_path(params["path"])
        mode = params.get("mode", 0o755)
        os.makedirs(path, mode=mode, exist_ok=True)
        return {}

    def list_dir(self, params: dict) -> dict:
        """Recursively list files under *path* (relative posix paths + sizes).

        Returns an empty list (not an error) when *path* does not exist —
        callers use this to back up tenant directories that may legitimately
        be empty or not yet created (e.g. an instance with no addons repos).
        """
        path = self._safe_path(params["path"])
        if not path.exists():
            return {"files": []}
        files = [
            {
                "path": entry.relative_to(path).as_posix(),
                "size": entry.stat().st_size,
            }
            for entry in path.rglob("*")
            if entry.is_file()
        ]
        return {"files": files}

    def read_bytes(self, params: dict) -> dict:
        """Read a chunk of a file as base64, for chunked download.

        params:
          path   — absolute path on the agent host
          offset — byte offset to start reading from (default 0)
          length — max bytes to read (default 4 MiB)

        Returns data_b64 plus eof=True once the read reaches end-of-file, so
        the caller (AgentHttpProxy.download_file_from_agent) knows when to
        stop without a separate stat() round-trip.
        """
        path = self._safe_path(params["path"])
        offset: int = params.get("offset", 0)
        length: int = params.get("length", 4 * 1024 * 1024)
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(length)
        size = path.stat().st_size
        eof = (offset + len(chunk)) >= size
        return {"data_b64": base64.b64encode(chunk).decode(), "eof": eof}
