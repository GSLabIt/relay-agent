"""Filesystem commands executed locally by the agent."""

from __future__ import annotations

import base64
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Cap a single fs.read_bytes chunk and fs.list_dir response so one request
# can't make the agent allocate (and base64-encode) an unbounded blob.
_MAX_READ_CHUNK = 8 * 1024 * 1024
_MAX_LIST_ENTRIES = 200_000


class FsCommands:
    def __init__(self, data_root_path: str) -> None:
        self._data_root_input = Path(os.path.abspath(data_root_path))
        self._data_root = Path(data_root_path).resolve()

    def _safe_path(self, raw: str) -> Path:
        """Verify *raw* stays within DATA_ROOT_PATH and return it resolved.

        Raises ValueError for any path that would escape the data root
        (via ``..``, an absolute value, or a symlink target) or that
        traverses a symlink *at or below* the data root.

        Symlinks *above* the data root (e.g. macOS ``/var`` → ``/private/var``,
        or ``DATA_ROOT_PATH=/data`` where ``/data`` is a mount symlink) are
        followed transparently — only components inside the root are
        forbidden, so a planted ``link -> ../other-tenant`` under the root
        can't redirect a write even though its target stays in-root.
        """
        root = str(self._data_root)
        raw_path = Path(raw)
        if not raw_path.is_absolute():
            raw_path = self._data_root / raw_path

        parts = Path(os.path.normpath(raw_path)).parts
        cur = parts[0]  # filesystem anchor ("/")
        inside = False
        for part in parts[1:]:
            cur = os.path.join(cur, part)
            # Check strictly-below-root components only: the root itself may
            # legitimately be a mount symlink; components above it are
            # followed transparently.
            if inside and os.path.islink(cur):
                raise ValueError(f"Path {raw!r} traverses a symlink")
            if not inside:
                real_cur = os.path.realpath(cur)
                if real_cur == root or real_cur.startswith(root + os.sep):
                    inside = True

        final = os.path.realpath(raw_path)
        if final != root and not final.startswith(root + os.sep):
            raise ValueError(f"Path {raw!r} is outside DATA_ROOT_PATH {root!r}")
        return Path(final)

    def _relative_parts(self, raw: str) -> tuple[str, ...]:
        """Return a lexical path below the canonical data root.

        Actual operations traverse these components from an already-open
        root fd with O_NOFOLLOW; this helper intentionally does not resolve
        them first, because doing so would reintroduce a check/use race.
        """
        root = str(self._data_root)
        candidate = os.path.abspath(
            raw if os.path.isabs(raw) else os.path.join(root, raw)
        )
        input_root = str(self._data_root_input)
        try:
            under_input_root = (
                os.path.commonpath((input_root, candidate)) == input_root
            )
        except ValueError:
            under_input_root = False
        if under_input_root:
            candidate = os.path.join(
                root, os.path.relpath(candidate, input_root)
            )
        try:
            inside = os.path.commonpath((root, candidate)) == root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError(f"Path {raw!r} is outside DATA_ROOT_PATH {root!r}")
        relative = os.path.relpath(candidate, root)
        if relative == ".":
            return ()
        parts = tuple(Path(relative).parts)
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Path {raw!r} is outside DATA_ROOT_PATH {root!r}")
        return parts

    @staticmethod
    def _dir_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @contextmanager
    def _parent_fd(
        self, raw: str, *, create_parents: bool = False
    ) -> Iterator[tuple[int, str]]:
        parts = self._relative_parts(raw)
        if not parts:
            raise ValueError("Operation requires a path below DATA_ROOT_PATH")
        fd = os.open(self._data_root, self._dir_flags())
        try:
            for part in parts[:-1]:
                if create_parents:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        except OSError as exc:
            if exc.errno in (40,):  # ELOOP: O_NOFOLLOW rejected a symlink
                raise ValueError(f"Path {raw!r} traverses a symlink") from exc
            raise
        finally:
            os.close(fd)

    def _open_dir(self, raw: str) -> int:
        parts = self._relative_parts(raw)
        fd = os.open(self._data_root, self._dir_flags())
        try:
            for part in parts:
                next_fd = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except Exception:
            os.close(fd)
            raise

    def write_text(self, params: dict) -> dict:
        """Write a UTF-8 text file, creating parent directories as needed."""
        raw_path = params["path"]
        content: str = params["content"]
        with self._parent_fd(raw_path, create_parents=True) as (parent, name):
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o666,
                dir_fd=parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
                file_obj.write(content)
        logger.info("fs.write_text: %d chars → %s", len(content), raw_path)
        return {}

    def write_bytes(self, params: dict) -> dict:
        """Write or append base64-encoded binary data to a file.

        params:
          path     — absolute path on the agent host
          data_b64 — base64-encoded bytes to write/append
          append   — if true, append to existing file (default: false)
        """
        raw_path = params["path"]
        data = base64.b64decode(params["data_b64"])
        append: bool = params.get("append", False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
        flags |= os.O_APPEND if append else os.O_TRUNC
        with self._parent_fd(raw_path, create_parents=True) as (parent, name):
            fd = os.open(name, flags, 0o666, dir_fd=parent)
            with os.fdopen(fd, "wb") as file_obj:
                file_obj.write(data)
        logger.info(
            "fs.write_bytes: %d bytes → %s (append=%s)",
            len(data),
            raw_path,
            append,
        )
        return {}

    def mkdir(self, params: dict) -> dict:
        """Create a directory and all parents."""
        raw_path = params["path"]
        mode = params.get("mode", 0o755)
        parts = self._relative_parts(raw_path)
        fd = os.open(self._data_root, self._dir_flags())
        try:
            for part in parts:
                try:
                    os.mkdir(part, mode=mode, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)
        return {}

    def list_dir(self, params: dict) -> dict:
        """Recursively list files under *path* (relative posix paths + sizes).

        Returns an empty list (not an error) when *path* does not exist —
        callers use this to back up tenant directories that may legitimately
        be empty or not yet created (e.g. an instance with no addons repos).
        """
        raw_path = params["path"]
        try:
            root_fd = self._open_dir(raw_path)
        except FileNotFoundError:
            return {"files": []}
        files: list[dict] = []
        truncated = False
        visited = 0
        stack: list[tuple[int, str]] = [(root_fd, "")]
        while stack and not truncated:
            current_fd, prefix = stack.pop()
            try:
                for name in os.listdir(current_fd):
                    visited += 1
                    if visited > _MAX_LIST_ENTRIES:
                        truncated = True
                        break
                    relative = f"{prefix}/{name}" if prefix else name
                    try:
                        info = os.stat(
                            name, dir_fd=current_fd, follow_symlinks=False
                        )
                        if stat.S_ISREG(info.st_mode):
                            files.append(
                                {"path": relative, "size": info.st_size}
                            )
                        elif stat.S_ISDIR(info.st_mode):
                            child_fd = os.open(
                                name, self._dir_flags(), dir_fd=current_fd
                            )
                            stack.append((child_fd, relative))
                    except OSError:
                        continue
            finally:
                os.close(current_fd)
        for pending_fd, _ in stack:
            os.close(pending_fd)
        return {"files": files, "truncated": truncated}

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
        raw_path = params["path"]
        offset = max(0, int(params.get("offset", 0)))
        # A negative/zero length means "read to EOF" for file.read() — treat
        # it as the default chunk size instead, and cap any positive value:
        # one request must not pull an arbitrarily large file into memory
        # (then base64 it, doubling the allocation).
        length = int(params.get("length", 4 * 1024 * 1024))
        if length <= 0:
            length = 4 * 1024 * 1024
        length = min(length, _MAX_READ_CHUNK)
        with self._parent_fd(raw_path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                os.lseek(fd, offset, os.SEEK_SET)
                chunk = os.read(fd, length)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
        eof = (offset + len(chunk)) >= size
        return {"data_b64": base64.b64encode(chunk).decode(), "eof": eof}
