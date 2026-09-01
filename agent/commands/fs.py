"""Filesystem commands executed locally by the agent."""

from __future__ import annotations

import base64
import errno
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Cap a single fs.read_bytes chunk and each fs.list_dir page so one request
# can't make the agent allocate (and base64-encode) an unbounded blob.
_MAX_READ_CHUNK = 8 * 1024 * 1024
_MAX_LIST_ENTRIES = 20_000
_MAX_LIST_FILES = 10_000
_MAX_LIST_CURSOR_DEPTH = 128


class FsCommands:
    def __init__(self, data_root_path: str) -> None:
        self._data_root_input = Path(os.path.abspath(data_root_path))
        self._data_root = Path(data_root_path).resolve()

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
            if exc.errno == errno.ELOOP:  # O_NOFOLLOW rejected a symlink
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
        except OSError as exc:
            os.close(fd)
            if exc.errno == errno.ELOOP:
                raise ValueError(f"Path {raw!r} traverses a symlink") from exc
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
                try:
                    next_fd = os.open(part, self._dir_flags(), dir_fd=fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise ValueError(
                            f"Path {raw_path!r} traverses a symlink"
                        ) from exc
                    raise
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

        The result is paginated by both visited entries and returned files.
        Pass ``next_cursor`` back as ``cursor`` until it is null. The cursor
        is a validated DFS stack, so pagination resumes without rescanning
        the whole tree and without retaining server-side state.
        """
        raw_path = params["path"]
        pagination_supported = params.get("pagination") is True
        cursor = params.get("cursor")
        if cursor is None:
            stack: list[dict[str, str]] = [{"path": "", "after": ""}]
        else:
            stack = self._validate_list_cursor(cursor)

        try:
            root_fd = self._open_dir(raw_path)
        except FileNotFoundError:
            return {"files": [], "truncated": False, "next_cursor": None}
        else:
            os.close(root_fd)

        files: list[dict] = []
        visited = 0
        page_full = False
        while stack and not page_full:
            frame = stack[-1]
            relative_dir = frame["path"]
            page_path = (
                f"{raw_path.rstrip('/')}/{relative_dir}"
                if relative_dir
                else raw_path
            )
            try:
                current_fd = self._open_dir(page_path)
            except OSError:
                stack.pop()
                continue

            descended = False
            try:
                names = sorted(
                    name
                    for name in os.listdir(current_fd)
                    if name > frame["after"]
                )
                for name in names:
                    frame["after"] = name
                    visited += 1
                    relative = (
                        f"{relative_dir}/{name}" if relative_dir else name
                    )
                    try:
                        info = os.stat(
                            name, dir_fd=current_fd, follow_symlinks=False
                        )
                        if stat.S_ISREG(info.st_mode):
                            files.append(
                                {"path": relative, "size": info.st_size}
                            )
                        elif stat.S_ISDIR(info.st_mode):
                            if len(stack) >= _MAX_LIST_CURSOR_DEPTH:
                                raise ValueError(
                                    "Directory nesting exceeds fs.list_dir "
                                    "cursor limit"
                                )
                            stack.append({"path": relative, "after": ""})
                            descended = True
                            if visited >= _MAX_LIST_ENTRIES:
                                page_full = True
                            break
                    except OSError:
                        pass

                    if (
                        visited >= _MAX_LIST_ENTRIES
                        or len(files) >= _MAX_LIST_FILES
                    ):
                        page_full = True
                        break
            finally:
                os.close(current_fd)

            if page_full:
                break
            if descended:
                continue
            stack.pop()

        next_cursor = {"version": 1, "stack": stack} if stack else None
        if next_cursor is not None and not pagination_supported:
            raise ValueError(
                "fs.list_dir exceeds one page; client must support pagination"
            )
        return {
            "files": files,
            "truncated": next_cursor is not None,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _validate_list_cursor(cursor: object) -> list[dict[str, str]]:
        if not isinstance(cursor, dict) or cursor.get("version") != 1:
            raise ValueError("Invalid fs.list_dir cursor")
        raw_stack = cursor.get("stack")
        if (
            not isinstance(raw_stack, list)
            or not 1 <= len(raw_stack) <= _MAX_LIST_CURSOR_DEPTH
        ):
            raise ValueError("Invalid fs.list_dir cursor stack")

        stack: list[dict[str, str]] = []
        for index, raw_frame in enumerate(raw_stack):
            if not isinstance(raw_frame, dict):
                raise ValueError("Invalid fs.list_dir cursor frame")
            relative = raw_frame.get("path")
            after = raw_frame.get("after")
            if not isinstance(relative, str) or not isinstance(after, str):
                raise ValueError("Invalid fs.list_dir cursor frame")
            if relative:
                path = Path(relative)
                if path.is_absolute() or any(
                    part in ("", ".", "..") for part in path.parts
                ):
                    raise ValueError("Invalid fs.list_dir cursor path")
            if "/" in after or "\x00" in after:
                raise ValueError("Invalid fs.list_dir cursor position")
            if index == 0 and relative != "":
                raise ValueError("Invalid fs.list_dir cursor root")
            if index:
                parent = stack[index - 1]
                expected = (
                    f"{parent['path']}/{parent['after']}"
                    if parent["path"]
                    else parent["after"]
                )
                if relative != expected:
                    raise ValueError("Invalid fs.list_dir cursor ancestry")
            stack.append({"path": relative, "after": after})
        return stack

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
