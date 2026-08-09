"""WebSocket connection to the control plane's gateway.

Protocol:
  Agent connects outbound: wss://<platform>/agent/ws
  Token sent as HTTP header on the upgrade request:
    Authorization: Bearer <TOKEN>

  Gateway → Agent (standard):
    {"type": "hello", "server_id": N, "version": "1.0"}
    {"type": "ping"}
    {"type": "request", "id": "uuid", "method": "...", "params": {...}}

  Agent → Gateway (standard):
    {"type": "ready", "docker_version": "27.x"}
    {"type": "pong"}
    {"type": "response", "id": "uuid", "result": {...}}
    {"type": "response", "id": "uuid", "error": {"code": -1, "message": "..."}}

  PTY streaming extension:
    Gateway → Agent:
      {"type": "request", ..., "method": "docker.container.exec_pty",
       "params": {"name_or_id": "...", "stream_id": "...",
                  "cols": N, "rows": N}}
      {"type": "stream_stdin",  "stream_id": "...", "data": "<b64>"}
      {"type": "stream_resize", "stream_id": "...", "cols": N, "rows": N}
      {"type": "stream_close",  "stream_id": "..."}

    Agent → Gateway:
      {"type": "response", "id": "uuid", "result": {"ok": true}}  ← ack
      {"type": "stream_data",   "stream_id": "...", "data": "<base64>"}
      {"type": "stream_closed", "stream_id": "...", "exit_code": N}

  Host shell PTY extension (berth-platform's server-level terminal for
  agent-connected servers — routers/terminal.py::server_terminal): same
  stream_id-keyed queue/dispatch machinery and message shapes as the
  container PTY above (stream_stdin/stream_resize/stream_close/stream_data/
  stream_closed, verbatim), only the start method differs — no
  name_or_id, since it isn't scoped to any container:
      {"type": "request", ..., "method": "saas.host.exec_pty",
       "params": {"stream_id": "...", "cols": N, "rows": N}}

  TCP tunnel extension (DB tunnel — berth-platform's db_tunnel_manager.py):
    same stream_id-keyed queue/dispatch machinery as PTY above, reusing
    stream_stdin/stream_close/stream_data/stream_closed verbatim — no PTY
    semantics (no resize), plus two new control messages for flow control
    (a dropped chunk desyncs a wire protocol permanently, unlike PTY output
    where it's just a cosmetic glitch — see agent_registry.py's
    _STREAM_HIGH_WATERMARK on the control-plane side for why):
    Gateway → Agent:
      {"type": "request", ..., "method": "tcp.tunnel.open",
       "params": {"target_host": "...", "target_port": N, "stream_id": "..."}}
      {"type": "stream_pause",  "stream_id": "..."}  ← stop reading target_host
      {"type": "stream_resume", "stream_id": "..."}  ← resume reading
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import queue
from contextlib import suppress

import websockets
import websockets.exceptions

from agent.executor import Executor

logger = logging.getLogger(__name__)

_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="agent"
)


async def run(
    gateway_url: str,
    token: str,
    executor: Executor,
    max_backoff: int = 60,
    command_timeout: float = 120.0,
    ssl_verify: bool = True,
) -> None:
    """Connect to the gateway and handle messages, reconnecting on failure."""
    import ssl as _ssl

    backoff = 2

    ssl_ctx: _ssl.SSLContext | bool | None = None
    if gateway_url.startswith("wss://"):
        if ssl_verify:
            ssl_ctx = _ssl.create_default_context()
        else:
            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE

    while True:
        try:
            logger.info("Connecting to gateway: %s", gateway_url)
            async with websockets.connect(
                gateway_url,
                ssl=ssl_ctx,
                max_size=16 * 1024 * 1024,
                ping_interval=30,
                ping_timeout=10,
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                backoff = 2
                await _handle_session(ws, executor, command_timeout)

        except websockets.exceptions.InvalidStatus as exc:
            code = exc.response.status_code
            if code in (4401, 4409):
                logger.error(
                    "Gateway rejected connection (code %s) — check your token",
                    code,
                )
                await asyncio.sleep(backoff)
            else:
                logger.warning(
                    "Gateway returned HTTP %s, retrying in %ds", code, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        except (
            websockets.exceptions.ConnectionClosed,
            ConnectionRefusedError,
            OSError,
        ) as exc:
            logger.warning(
                "Disconnected (%s), reconnecting in %ds...", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

        except Exception as exc:
            logger.exception(
                "Unexpected error: %s — retrying in %ds", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def _handle_session(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    command_timeout: float,
) -> None:
    """Handle a single connected session until the WebSocket closes."""
    # stream_id → asyncio.Queue for stream control messages (stdin/resize/
    # pause/resume/close) — shared by PTY sessions and TCP tunnel sessions,
    # the queue itself is payload-agnostic (see gateway.py module docstring).
    active_streams: dict[str, asyncio.Queue] = {}

    async for raw in ws:
        try:
            import json

            message = json.loads(raw)
        except Exception:
            logger.warning("Received non-JSON message, ignoring")
            continue

        msg_type = message.get("type")

        if msg_type == "hello":
            server_id = message.get("server_id")
            logger.info("Connected to gateway (server_id=%s)", server_id)
            await ws.send(
                _json(
                    {
                        "type": "ready",
                        "docker_version": executor.docker_version(),
                    }
                )
            )

        elif msg_type == "ping":
            await ws.send(_json({"type": "pong"}))

        elif msg_type == "stream_stdin":
            stream_id = message.get("stream_id", "")
            stream_q = active_streams.get(stream_id)
            if stream_q:
                with suppress(asyncio.QueueFull):
                    stream_q.put_nowait(
                        ("data", base64.b64decode(message.get("data", "")))
                    )

        elif msg_type == "stream_resize":
            stream_id = message.get("stream_id", "")
            stream_q = active_streams.get(stream_id)
            if stream_q:
                with suppress(asyncio.QueueFull):
                    stream_q.put_nowait(
                        (
                            "resize",
                            message.get("cols", 80),
                            message.get("rows", 24),
                        )
                    )

        elif msg_type == "stream_pause":
            stream_id = message.get("stream_id", "")
            stream_q = active_streams.get(stream_id)
            if stream_q:
                with suppress(asyncio.QueueFull):
                    stream_q.put_nowait(("pause",))

        elif msg_type == "stream_resume":
            stream_id = message.get("stream_id", "")
            stream_q = active_streams.get(stream_id)
            if stream_q:
                with suppress(asyncio.QueueFull):
                    stream_q.put_nowait(("resume",))

        elif msg_type == "stream_close":
            stream_id = message.get("stream_id", "")
            stream_q = active_streams.get(stream_id)
            if stream_q:
                with suppress(asyncio.QueueFull):
                    stream_q.put_nowait(("close",))

        elif msg_type == "request":
            request_id = message.get("id", "")
            method = message.get("method", "")
            params = message.get("params") or {}

            if method == "docker.container.exec_pty":
                asyncio.create_task(
                    _handle_pty_session(
                        ws, executor, request_id, params, active_streams
                    )
                )
            elif method == "saas.host.exec_pty":
                asyncio.create_task(
                    _handle_host_pty_session(
                        ws, executor, request_id, params, active_streams
                    )
                )
            elif method == "tcp.tunnel.open":
                asyncio.create_task(
                    _handle_tcp_tunnel_session(
                        ws, executor, request_id, params, active_streams
                    )
                )
            else:
                asyncio.create_task(
                    _execute_and_reply(
                        ws,
                        executor,
                        request_id,
                        method,
                        params,
                        command_timeout,
                    )
                )

        else:
            logger.debug("Unknown message type %r, ignoring", msg_type)


async def _handle_pty_session(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    request_id: str,
    params: dict,
    active_streams: dict[str, asyncio.Queue],
) -> None:
    """Start an interactive PTY session and stream I/O over the gateway WS."""
    stream_id: str = params.get("stream_id", "")
    name_or_id: str = params.get("name_or_id", "")
    cols: int = int(params.get("cols", 220))
    rows: int = int(params.get("rows", 50))

    # asyncio queue: ("data", bytes) | ("resize", cols, rows) | ("close",)
    ctrl_q: asyncio.Queue = asyncio.Queue(maxsize=256)
    active_streams[stream_id] = ctrl_q

    # threading queue bridged from ctrl_q for use in the blocking PTY thread
    thread_q: queue.Queue = queue.Queue(maxsize=256)

    loop = asyncio.get_event_loop()

    async def _bridge() -> None:
        """Forward asyncio ctrl_q items to the thread queue."""
        try:
            while True:
                item = await ctrl_q.get()
                thread_q.put(item)
                if item[0] == "close":
                    break
        except asyncio.CancelledError:
            thread_q.put(("close",))

    async def _send_data(chunk: bytes) -> None:
        with suppress(Exception):
            await ws.send(
                _json(
                    {
                        "type": "stream_data",
                        "stream_id": stream_id,
                        "data": base64.b64encode(chunk).decode(),
                    }
                )
            )

    # Acknowledge immediately so the control plane knows the PTY is starting.
    await ws.send(
        _json({"type": "response", "id": request_id, "result": {"ok": True}})
    )

    bridge_task = asyncio.create_task(_bridge())
    exit_code = 0

    try:
        exit_code = await loop.run_in_executor(
            _THREAD_POOL,
            executor.run_pty_blocking,
            name_or_id,
            cols,
            rows,
            thread_q,
            loop,
            _send_data,
        )
    except Exception:
        logger.exception("PTY session error for container %s", name_or_id)
    finally:
        bridge_task.cancel()
        active_streams.pop(stream_id, None)
        with suppress(Exception):
            await ws.send(
                _json(
                    {
                        "type": "stream_closed",
                        "stream_id": stream_id,
                        "exit_code": exit_code,
                    }
                )
            )


async def _handle_host_pty_session(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    request_id: str,
    params: dict,
    active_streams: dict[str, asyncio.Queue],
) -> None:
    """Start a host-level shell session and stream I/O over the gateway WS.

    Mirrors _handle_pty_session almost exactly — same ctrl_q/thread_q bridge,
    same ack-then-stream shape — only the blocking call differs (no
    name_or_id: this isn't scoped to a container, see
    agent/commands/host_shell.py).
    """
    stream_id: str = params.get("stream_id", "")
    cols: int = int(params.get("cols", 220))
    rows: int = int(params.get("rows", 50))

    ctrl_q: asyncio.Queue = asyncio.Queue(maxsize=256)
    active_streams[stream_id] = ctrl_q

    thread_q: queue.Queue = queue.Queue(maxsize=256)

    loop = asyncio.get_event_loop()

    async def _bridge() -> None:
        try:
            while True:
                item = await ctrl_q.get()
                thread_q.put(item)
                if item[0] == "close":
                    break
        except asyncio.CancelledError:
            thread_q.put(("close",))

    async def _send_data(chunk: bytes) -> None:
        with suppress(Exception):
            await ws.send(
                _json(
                    {
                        "type": "stream_data",
                        "stream_id": stream_id,
                        "data": base64.b64encode(chunk).decode(),
                    }
                )
            )

    await ws.send(
        _json({"type": "response", "id": request_id, "result": {"ok": True}})
    )

    bridge_task = asyncio.create_task(_bridge())
    exit_code = 0

    try:
        exit_code = await loop.run_in_executor(
            _THREAD_POOL,
            executor.run_host_pty_blocking,
            cols,
            rows,
            thread_q,
            loop,
            _send_data,
        )
    except Exception:
        logger.exception("Host PTY session error")
    finally:
        bridge_task.cancel()
        active_streams.pop(stream_id, None)
        with suppress(Exception):
            await ws.send(
                _json(
                    {
                        "type": "stream_closed",
                        "stream_id": stream_id,
                        "exit_code": exit_code,
                    }
                )
            )


async def _handle_tcp_tunnel_session(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    request_id: str,
    params: dict,
    active_streams: dict[str, asyncio.Queue],
) -> None:
    """Open a raw TCP connection to target_host:target_port (reachable from
    this agent's own Docker host — for the DB tunnel feature this is always
    the local berth_postgres container) and relay bytes over the gateway WS.

    Unlike _handle_pty_session, this runs entirely on the event loop — no
    thread pool, no threading.Queue bridge — since asyncio.open_connection
    is natively async. Honors stream_pause/stream_resume (queued as
    ("pause",) / ("resume",) tuples, same ctrl_q as PTY's stdin/close) by
    gating the target->WS read loop on an asyncio.Event, so a slow WS
    consumer can throttle how fast we drain the target socket instead of
    us buffering unboundedly or silently dropping chunks.

    ctrl_q itself (the WS->target direction: client writes/COPY data) is
    deliberately unbounded, unlike PTY's maxsize=256 stdin queue — PTY input
    is keystrokes, where an occasional drop under a stalled terminal is a
    known-acceptable cosmetic tradeoff, but there is no pause/resume signal
    in this direction (only target->WS has one, see above), so a bounded
    queue would silently drop chunks of a stateful wire protocol under a
    large write burst (e.g. \\copy of a big file) outrunning the target's
    drain rate — exactly the desync risk this module's docstring warns
    about, just in the direction it doesn't cover. Unbounded trades that for
    a memory-growth risk that in practice stays small: growth is capped by
    how fast the control-plane can push stream_stdin frames, which is itself
    bounded by ordinary WS/TCP backpressure between the control plane and
    this agent.
    """
    stream_id: str = params.get("stream_id", "")
    target_host: str = params.get("target_host", "")

    try:
        target_port = int(params.get("target_port", 0))
    except (TypeError, ValueError) as exc:
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": f"Invalid target_port: {exc}",
                    },
                }
            )
        )
        return

    ctrl_q: asyncio.Queue = asyncio.Queue()
    active_streams[stream_id] = ctrl_q

    try:
        reader, writer = await executor.open_tcp_tunnel_connection(
            target_host, target_port
        )
    except Exception as exc:
        active_streams.pop(stream_id, None)
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {"code": -32002, "message": str(exc)},
                }
            )
        )
        return

    await ws.send(
        _json({"type": "response", "id": request_id, "result": {"ok": True}})
    )

    not_paused = asyncio.Event()
    not_paused.set()

    async def _read_target_to_ws() -> None:
        try:
            while True:
                await not_paused.wait()
                chunk = await reader.read(65536)
                if not chunk:
                    return
                # Gate again right before sending, not just before starting
                # the read: reader.read() can't be interrupted mid-flight,
                # so a read already in progress when "pause" arrives still
                # completes — without this second check that chunk would
                # ship anyway, defeating the pause. This bounds the worst
                # case to "one chunk (<=64KB) held in our own memory while
                # paused, never transmitted until resumed" instead of an
                # unbounded number of chunks leaking through.
                await not_paused.wait()
                await ws.send(
                    _json(
                        {
                            "type": "stream_data",
                            "stream_id": stream_id,
                            "data": base64.b64encode(chunk).decode(),
                        }
                    )
                )
        except Exception:
            return

    async def _consume_ctrl() -> None:
        while True:
            item = await ctrl_q.get()
            tag = item[0]
            if tag == "data":
                try:
                    writer.write(item[1])
                    await writer.drain()
                except Exception:
                    return
            elif tag == "pause":
                not_paused.clear()
            elif tag == "resume":
                not_paused.set()
            elif tag == "close":
                return

    read_task = asyncio.create_task(_read_target_to_ws())
    ctrl_task = asyncio.create_task(_consume_ctrl())
    try:
        await asyncio.wait(
            {read_task, ctrl_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        read_task.cancel()
        ctrl_task.cancel()
        with suppress(asyncio.CancelledError):
            await read_task
        with suppress(asyncio.CancelledError):
            await ctrl_task
        with suppress(Exception):
            writer.close()
            await writer.wait_closed()
        active_streams.pop(stream_id, None)
        with suppress(Exception):
            await ws.send(
                _json(
                    {
                        "type": "stream_closed",
                        "stream_id": stream_id,
                        "exit_code": 0,
                    }
                )
            )


async def _execute_and_reply(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    request_id: str,
    method: str,
    params: dict,
    timeout: float,
) -> None:
    """Run a command in the thread pool and send the response."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                _THREAD_POOL, executor.dispatch, method, params
            ),
            timeout=timeout,
        )
        await ws.send(
            _json({"type": "response", "id": request_id, "result": result})
        )
    except asyncio.TimeoutError:
        logger.error("Command %r timed out after %ss", method, timeout)
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"Command timed out after {timeout}s",
                    },
                }
            )
        )
    except Exception as exc:
        logger.exception("Command %r failed: %s", method, exc)
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {"code": -32001, "message": str(exc)},
                }
            )
        )


def _json(obj: dict) -> str:
    import json

    return json.dumps(obj)
