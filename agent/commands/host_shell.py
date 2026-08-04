"""Interactive host-level shell PTY, executed locally by the agent.

Distinct from ContainerCommands.run_pty_blocking (docker exec into a named
container): this spawns a real shell process on the machine the agent
itself runs on, via a raw pty pair — no Docker API involved at all. Used
for the control plane's server-level terminal on agent-connected servers
(berth-platform routers/terminal.py::server_terminal), the agent-connection
counterpart of the SSH invoke_shell path already used for ssh-connected
servers.

Not a new trust boundary: the agent channel already lets the control plane
run arbitrary commands as this same process's user via docker.container.run
(e.g. a privileged container with `/` bind-mounted, then exec into it) —
this only offers a more direct path to a capability that's already
reachable. Runs as whatever user started the agent process (typically
root, same as every other docker.* command here).
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

# Allowlist, not a denylist: os.environ on this process includes TOKEN (the
# agent's own long-lived credential for the /agent/ws connection, set via
# `docker run -e TOKEN=...`, see agent/config.py) — dict(os.environ) would
# hand it straight to whoever opens this shell. Unlike the container-scoped
# terminal (docker exec uses the *container's* configured env, never the
# agent process's own), a host shell has no such separation for free, so it
# has to be built explicitly. An allowlist also survives a future secret
# being added to the agent's own env without anyone remembering to add it
# to a denylist here.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "SHELL",
)


def _safe_shell_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env.setdefault(
        "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    env["TERM"] = "xterm-256color"
    return env


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with suppress(OSError, struct.error):
        fcntl.ioctl(
            fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )


class HostShellCommands:
    def run_pty_blocking(
        self,
        cols: int,
        rows: int,
        stdin_q: Any,
        loop: Any,
        data_cb: Any,
    ) -> int:
        """Run an interactive shell on the host. Blocks until the shell exits.

        Same stdin_q/loop/data_cb contract as
        ContainerCommands.run_pty_blocking — see that docstring.

        Returns the shell's exit code.
        """
        import asyncio
        import queue
        import select as _select
        import threading

        shell = os.environ.get("SHELL") or "/bin/bash"
        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, rows, cols)

        env = _safe_shell_env()

        proc = subprocess.Popen(
            [shell, "-l"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
            env=env,
        )
        # The child inherited its own copy of the slave fd across fork —
        # the parent's copy would otherwise keep the slave end open even
        # after the child exits, and reads on master_fd would never see EOF.
        os.close(slave_fd)

        os.set_blocking(master_fd, False)
        stop_event = threading.Event()

        def _reader() -> None:
            try:
                while not stop_event.is_set():
                    r, _, _ = _select.select([master_fd], [], [], 0.05)
                    if r:
                        try:
                            chunk = os.read(master_fd, 4096)
                        except BlockingIOError:
                            continue
                        except OSError:
                            break
                        if not chunk:
                            break
                        asyncio.run_coroutine_threadsafe(data_cb(chunk), loop)
            except Exception:
                logger.debug("Host PTY reader exited")
            finally:
                stop_event.set()

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            while not stop_event.is_set():
                try:
                    item = stdin_q.get(timeout=0.1)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                kind = item[0]
                if kind == "data":
                    try:
                        os.write(master_fd, item[1])
                    except OSError:
                        break
                elif kind == "resize":
                    _, new_cols, new_rows = item
                    _set_winsize(master_fd, new_rows, new_cols)
                elif kind == "close":
                    break
        finally:
            stop_event.set()
            # SIGHUP the whole process group, not just the shell itself —
            # matches what a real terminal disconnect does, so anything the
            # user backgrounded (nohup-less) gets a chance to notice and
            # doesn't linger as an orphan attached to a dead pty.
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            with suppress(OSError):
                os.close(master_fd)
            reader_thread.join(timeout=2.0)
            with suppress(Exception):
                proc.wait(timeout=2.0)

        return proc.returncode or 0
