"""Self-checks for the v0.12 security/reliability hardening.

Pure-logic only — no Docker daemon, no WebSocket. Run with `make test` or
`python tests/test_hardening.py`.
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.commands.container import ContainerCommands
from agent.commands.fs import FsCommands
from agent.commands.instance import _safe_slug
from agent.commands.postgres import PostgresCommands
from agent.gateway import (
    _MAX_ACTIVE_STREAMS,
    _SESSION_MAX_BUFFERED_BYTES,
    _TUNNEL_MAX_BUFFERED_BYTES,
    _claim_stream,
    _dispatch_message,
    _offer,
    _redact,
    _release_stream,
    _Session,
    _stream_ctrl,
)


def test_slug_validation() -> None:
    assert _safe_slug("acme-prod") == "acme-prod"
    assert _safe_slug("a") == "a"
    for bad in ("../../etc", "/etc/passwd", "a/b", "A", "", "-x", "x" * 64):
        try:
            _safe_slug(bad)
        except ValueError:
            continue
        raise AssertionError(f"slug {bad!r} should have been rejected")


def test_volume_relative_escape_rejected() -> None:
    cc = ContainerCommands(docker_client=None, data_root_path="/data/tenants")
    ok = cc._resolve_volumes({"acme/filestore": {"bind": "/x"}})
    assert ok == {"/data/tenants/acme/filestore": {"bind": "/x"}}
    # absolute passes through (trusted control plane)
    assert cc._resolve_volumes({"/opt/x": {"bind": "/x"}}) == {
        "/opt/x": {"bind": "/x"}
    }
    for bad in ("../../etc", "acme/../../root", ".."):
        try:
            cc._resolve_volumes({bad: {"bind": "/x"}})
        except ValueError:
            continue
        raise AssertionError(f"relative volume {bad!r} should be rejected")
    with tempfile.TemporaryDirectory() as root:
        os.symlink("/etc", os.path.join(root, "escape"))
        cc = ContainerCommands(docker_client=None, data_root_path=root)
        try:
            cc._resolve_volumes({"escape": {"bind": "/x"}})
        except ValueError:
            pass
        else:
            raise AssertionError("symlinked relative volume should be rejected")


def test_fs_safe_path_and_clamps() -> None:
    with tempfile.TemporaryDirectory() as root:
        fs = FsCommands(root)
        # escape
        for bad in ("/etc/passwd", os.path.join(root, "..", "x")):
            try:
                fs._safe_path(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} should escape-check fail")
        # symlink component pointing outside root
        os.symlink("/etc", os.path.join(root, "link"))
        # in-root symlink redirect (link -> a sibling still under root):
        # resolve() would collapse it and pass the containment check
        os.mkdir(os.path.join(root, "tenant-b"))
        os.symlink(os.path.join(root, "tenant-b"), os.path.join(root, "inroot"))
        for bad in ("link/passwd", "inroot/secret"):
            try:
                fs._safe_path(os.path.join(root, bad))
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} symlink traversal not rejected")

        # read_bytes clamps negative / oversized length
        p = os.path.join(root, "f.bin")
        with open(p, "wb") as f:
            f.write(b"0123456789")
        out = fs.read_bytes({"path": p, "offset": 0, "length": -1})
        import base64

        assert base64.b64decode(out["data_b64"]) == b"0123456789"
        out = fs.read_bytes({"path": p, "offset": -5, "length": 3})
        assert base64.b64decode(out["data_b64"]) == b"012"

        import agent.commands.fs as fsmod

        original_cap = fsmod._MAX_READ_CHUNK
        fsmod._MAX_READ_CHUNK = 4
        try:
            for requested in (-1, 100):
                out = fs.read_bytes(
                    {"path": p, "offset": 0, "length": requested}
                )
                assert base64.b64decode(out["data_b64"]) == b"0123"
                assert out["eof"] is False
        finally:
            fsmod._MAX_READ_CHUNK = original_cap


def test_fs_operations_reject_symlink_targets() -> None:
    with tempfile.TemporaryDirectory() as root:
        fs = FsCommands(root)
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        try:
            os.symlink(outside.name, os.path.join(root, "victim"))
            for operation, params in (
                (fs.write_text, {"path": "victim", "content": "owned"}),
                (
                    fs.write_bytes,
                    {"path": "victim", "data_b64": "b3duZWQ="},
                ),
                (fs.read_bytes, {"path": "victim"}),
            ):
                try:
                    operation(params)
                except OSError:
                    continue
                raise AssertionError("descriptor operation followed a symlink")
            assert os.path.getsize(outside.name) == 0
        finally:
            os.unlink(outside.name)


def test_list_dir_entry_cap_counts_all_entries() -> None:
    import agent.commands.fs as fsmod

    with tempfile.TemporaryDirectory() as root:
        fs = FsCommands(root)
        # Many directories, zero files — the cap must still stop traversal.
        for i in range(50):
            os.makedirs(os.path.join(root, f"d{i}", "sub"))
        orig = fsmod._MAX_LIST_ENTRIES
        fsmod._MAX_LIST_ENTRIES = 10
        try:
            out = fs.list_dir({"path": root})
        finally:
            fsmod._MAX_LIST_ENTRIES = orig
        assert out["truncated"] is True
        assert out["files"] == []


def test_redact() -> None:
    assert _redact("Bearer sk_live_abc123 failed") == "Bearer *** failed"
    assert _redact('password: "hunter2"') == 'password: "***"'
    assert _redact("token=abc.def end") == "token=*** end"
    assert _redact("no secrets here") == "no secrets here"


def test_stream_claim_and_cap() -> None:
    sess = _Session()
    q = __import__("asyncio").Queue()
    assert _claim_stream(sess, "", q) is not None  # empty rejected
    assert _claim_stream(sess, "s1", q) is None
    assert _claim_stream(sess, "s1", q) is not None  # duplicate rejected

    # byte cap: an oversized data chunk enqueues one ("close",), marks the
    # stream closing, and drops every later frame (queue must not grow).
    sess.stream_bytes["s1"] = _TUNNEL_MAX_BUFFERED_BYTES
    sess.total_stream_bytes = _TUNNEL_MAX_BUFFERED_BYTES
    _stream_ctrl(sess, {"stream_id": "s1"}, ("data", b"x" * 1024))
    _stream_ctrl(sess, {"stream_id": "s1"}, ("data", b"y" * 1024))
    _stream_ctrl(sess, {"stream_id": "s1"}, ("data", b"z" * 1024))
    assert q.qsize() == 1
    assert q.get_nowait() == ("close",)
    assert "s1" in sess.closing_streams
    assert sess.total_stream_bytes == _TUNNEL_MAX_BUFFERED_BYTES

    _release_stream(sess, "s1", q)
    assert "s1" not in sess.streams
    assert "s1" not in sess.closing_streams
    assert sess.total_stream_bytes == 0

    # release only removes its own mapping
    q2 = __import__("asyncio").Queue()
    _claim_stream(sess, "s2", q2)
    _release_stream(sess, "s2", q)  # wrong queue — must not pop
    assert "s2" in sess.streams

    # Aggregate cap closes a stream even when its own per-stream cap has
    # not been reached.
    sess.total_stream_bytes = _SESSION_MAX_BUFFERED_BYTES
    _stream_ctrl(sess, {"stream_id": "s2"}, ("data", b"x"))
    assert q2.get_nowait() == ("close",)


def test_dispatch_rejects_stream_before_spawning() -> None:
    class Ws:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, message) -> None:
            self.messages.append(__import__("json").loads(message))

    async def scenario() -> None:
        sess = _Session()
        for index in range(_MAX_ACTIVE_STREAMS):
            assert (
                _claim_stream(
                    sess, f"existing-{index}", __import__("asyncio").Queue()
                )
                is None
            )
        ws = Ws()
        raw = __import__("json").dumps(
            {
                "type": "request",
                "id": "request-1",
                "method": "tcp.tunnel.open",
                "params": {"stream_id": "overflow"},
            }
        )
        await _dispatch_message(ws, object(), 0.1, sess, raw)
        assert not sess.tasks
        assert ws.messages[0]["error"]["message"] == "too many active streams"

    __import__("asyncio").run(scenario())


def test_dispatch_rejects_non_object_params() -> None:
    class Ws:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, message) -> None:
            self.messages.append(__import__("json").loads(message))

    async def scenario() -> None:
        sess = _Session()
        ws = Ws()
        raw = __import__("json").dumps(
            {
                "type": "request",
                "id": "bad-params",
                "method": "fake.run",
                "params": [],
            }
        )
        await _dispatch_message(ws, object(), 0.1, sess, raw)
        assert ws.messages[0]["error"]["code"] == -32602
        assert sess.pending_commands == 0
        assert not sess.tasks

    __import__("asyncio").run(scenario())


def test_pty_ack_waits_for_worker_start() -> None:
    class Ws:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, message) -> None:
            self.messages.append(__import__("json").loads(message))

    allow_start = threading.Event()

    class FakeExecutor:
        def run_host_pty_blocking(
            self,
            cols,
            rows,
            stdin_q,
            loop,
            data_cb,
            stop_event,
            started_cb,
            data_drained_cb,
        ):
            allow_start.wait(2)
            started_cb()
            while not stop_event.is_set():
                try:
                    item = stdin_q.get(timeout=0.01)
                except queue.Empty:
                    continue
                if item[0] == "data":
                    data_drained_cb(len(item[1]))
                elif item[0] == "close":
                    break
            return 0

    async def scenario() -> None:
        asyncio = __import__("asyncio")
        sess = _Session()
        ws = Ws()
        raw = __import__("json").dumps(
            {
                "type": "request",
                "id": "pty",
                "method": "saas.host.exec_pty",
                "params": {"stream_id": "pty-1"},
            }
        )
        await _dispatch_message(ws, FakeExecutor(), 0.1, sess, raw)
        await asyncio.sleep(0.02)
        assert ws.messages == []
        allow_start.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if ws.messages:
                break
        assert ws.messages[0]["result"] == {"ok": True}
        _stream_ctrl(sess, {"stream_id": "pty-1"}, ("close",))
        await sess.close()

    __import__("asyncio").run(scenario())


def test_command_timeout_keeps_underlying_slot() -> None:
    class Ws:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, message) -> None:
            self.messages.append(__import__("json").loads(message))

    release = threading.Event()

    class FakeExecutor:
        def dispatch(self, method, params):
            release.wait(2)
            return {}

    async def scenario() -> None:
        sess = _Session()
        ws = Ws()
        raw = __import__("json").dumps(
            {"type": "request", "id": "slow", "method": "fake.run"}
        )
        await _dispatch_message(ws, FakeExecutor(), 0.01, sess, raw)
        await __import__("asyncio").sleep(0.05)
        assert sess.pending_commands == 1
        assert ws.messages[0]["error"]["message"].startswith(
            "Command timed out"
        )
        release.set()
        for _ in range(20):
            await __import__("asyncio").sleep(0.01)
            if sess.pending_commands == 0:
                break
        assert sess.pending_commands == 0
        await sess.close()

    __import__("asyncio").run(scenario())


def test_offer_never_blocks() -> None:
    q: queue.Queue = queue.Queue(maxsize=2)
    _offer(q, ("data", b"a"))
    _offer(q, ("data", b"b"))
    _offer(q, ("data", b"c"))  # full — dropped, no block
    assert q.qsize() == 2
    _offer(q, ("close",))  # forces room
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert ("close",) in items


def test_postgres_recreate_rolls_back_on_startup_failure() -> None:
    calls = []

    class Container:
        status = "running"
        short_id = "abc123"

        def stop(self):
            calls.append("old.stop")

        def start(self):
            calls.append("old.start")

        def rename(self, name):
            calls.append(f"old.rename:{name}")

        def remove(self, **kwargs):
            calls.append(f"old.remove:{kwargs}")

    class Partial:
        def remove(self, **kwargs):
            calls.append(f"partial.remove:{kwargs}")

    command = PostgresCommands(docker_client=None, data_root_path="/tmp")
    command._run_container = lambda *args, **kwargs: Partial()
    command._get_existing = lambda: Partial()

    def fail_ready(container, pg_user):
        raise RuntimeError("replacement failed")

    command._wait_ready = fail_ready
    try:
        command._recreate_with_rollback(
            Container(),
            "postgres",
            "secret",
            "network",
            image="postgres:16",
            wal_archiving=False,
            pg_extra_args=[],
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed replacement should be propagated")

    assert calls == [
        "old.stop",
        "old.rename:berth_postgres_rollback_abc123",
        "partial.remove:{'force': True}",
        "old.rename:berth_postgres",
        "old.start",
    ]


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run()
