"""WebSocket connection to the SaaS Platform gateway.

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
    # stream_id → asyncio.Queue for PTY control messages (stdin/resize/close)
    active_ptys: dict[str, asyncio.Queue] = {}

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
            pty_q = active_ptys.get(stream_id)
            if pty_q:
                with suppress(asyncio.QueueFull):
                    pty_q.put_nowait(
                        ("data", base64.b64decode(message.get("data", "")))
                    )

        elif msg_type == "stream_resize":
            stream_id = message.get("stream_id", "")
            pty_q = active_ptys.get(stream_id)
            if pty_q:
                with suppress(asyncio.QueueFull):
                    pty_q.put_nowait(
                        (
                            "resize",
                            message.get("cols", 80),
                            message.get("rows", 24),
                        )
                    )

        elif msg_type == "stream_close":
            stream_id = message.get("stream_id", "")
            pty_q = active_ptys.get(stream_id)
            if pty_q:
                with suppress(asyncio.QueueFull):
                    pty_q.put_nowait(("close",))

        elif msg_type == "request":
            request_id = message.get("id", "")
            method = message.get("method", "")
            params = message.get("params") or {}

            if method == "docker.container.exec_pty":
                asyncio.create_task(
                    _handle_pty_session(
                        ws, executor, request_id, params, active_ptys
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
    active_ptys: dict[str, asyncio.Queue],
) -> None:
    """Start an interactive PTY session and stream I/O over the gateway WS."""
    stream_id: str = params.get("stream_id", "")
    name_or_id: str = params.get("name_or_id", "")
    cols: int = int(params.get("cols", 220))
    rows: int = int(params.get("rows", 50))

    # asyncio queue: ("data", bytes) | ("resize", cols, rows) | ("close",)
    ctrl_q: asyncio.Queue = asyncio.Queue(maxsize=256)
    active_ptys[stream_id] = ctrl_q

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
        active_ptys.pop(stream_id, None)
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
