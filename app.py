#!/usr/bin/env python3
"""LAN web and WebSocket controller for a USB-attached NeoKey Trinkey."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import os
import pathlib
import threading
import time
from typing import Any

import serial
from aiohttp import WSMsgType, web
from serial.tools import list_ports


ROOT = pathlib.Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
STATIC_DIR = ROOT / "static"
BIND = os.environ.get("NEOKEY_BIND", "0.0.0.0")
PORT = int(os.environ.get("NEOKEY_WEB_PORT", "8888"))

DEFAULT_STATE: dict[str, Any] = {
    "r": 255,
    "g": 255,
    "b": 255,
    "brightness": 25,
    "flash": False,
    "flashRate": 2.0,
}
STATE_FIELDS = {"r", "g", "b", "brightness", "flash", "flashRate"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("neokey-web")


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("state must be an object")
    result = {
        "r": int(value.get("r")),
        "g": int(value.get("g")),
        "b": int(value.get("b")),
        "brightness": int(value.get("brightness")),
        "flash": value.get("flash"),
        "flashRate": float(value.get("flashRate")),
    }
    if not all(0 <= result[name] <= 255 for name in ("r", "g", "b")):
        raise ValueError("RGB values must be from 0 to 255")
    if not 0 <= result["brightness"] <= 100:
        raise ValueError("brightness must be from 0 to 100")
    if not isinstance(result["flash"], bool):
        raise ValueError("flash must be true or false")
    if not 0.25 <= result["flashRate"] <= 10.0:
        raise ValueError("flashRate must be from 0.25 to 10 Hz")
    return result


def load_state() -> dict[str, Any]:
    try:
        return validate_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_STATE.copy()


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = load_state()
        self._dirty = False

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._state.copy()

    def update(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("state update must be an object")
        if set(patch) - STATE_FIELDS:
            raise ValueError("unknown state field")
        if not patch:
            raise ValueError("state update is empty")
        with self._lock:
            candidate = self._state.copy()
            candidate.update(patch)
            state = validate_state(candidate)
            self._state = state
            self._dirty = True
            return state.copy()

    def persist(self) -> None:
        """Coalesce rapid WebSocket changes into occasional SD-card writes."""
        with self._lock:
            if not self._dirty:
                return
            encoded = json.dumps(self._state, indent=2) + "\n"
            self._dirty = False
        try:
            temp = STATE_FILE.with_suffix(".json.tmp")
            temp.write_text(encoded, encoding="utf-8")
            temp.replace(STATE_FILE)
        except OSError:
            with self._lock:
                self._dirty = True
            log.exception("Could not persist NeoKey state")


class TrinkeySerial(threading.Thread):
    VID = 0x239A
    PID = 0x8100

    def __init__(self, store: StateStore) -> None:
        super().__init__(name="trinkey-serial", daemon=True)
        self.store = store
        self._changed = threading.Event()
        self._status_lock = threading.Lock()
        self._devices: list[str] = []
        self._last_error: str | None = None

    def notify(self) -> None:
        self._changed.set()

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return {
                "connected": bool(self._devices),
                "device": self._devices[0] if self._devices else None,
                "deviceCount": len(self._devices),
                "devices": self._devices.copy(),
                "lastError": self._last_error,
            }

    def _set_devices(self, devices: list[str], error: str | None = None) -> None:
        with self._status_lock:
            self._devices = sorted(devices)
            self._last_error = error

    @staticmethod
    def find_data_ports() -> list[str]:
        configured = os.environ.get("NEOKEY_DEVICE")
        if configured:
            devices = [item.strip() for item in configured.split(",") if item.strip()]
            return [item for item in devices if os.path.exists(item)]
        matches = [
            port
            for port in list_ports.comports()
            if port.vid == TrinkeySerial.VID and port.pid == TrinkeySerial.PID
        ]
        groups: dict[str, list[Any]] = {}
        for port in matches:
            identity = port.serial_number or (port.location or port.device).split(":")[0]
            groups.setdefault(identity, []).append(port)
        result: list[str] = []
        for ports in groups.values():
            # Each configured Trinkey exposes console first and data CDC second.
            if len(ports) >= 2:
                ports.sort(key=lambda item: (item.location or "", item.device))
                result.append(ports[-1].device)
        if result:
            return sorted(result)

        links = sorted(glob.glob("/dev/serial/by-id/*NeoKey*if*"))
        by_identity: dict[str, list[str]] = {}
        for link in links:
            identity = link.rsplit("-if", 1)[0]
            by_identity.setdefault(identity, []).append(link)
        return sorted(max(group) for group in by_identity.values() if len(group) >= 2)

    @staticmethod
    def command_for(state: dict[str, Any]) -> bytes:
        return (
            "SET {r} {g} {b} {brightness} {flash} {rate:.2f}\n".format(
                r=state["r"],
                g=state["g"],
                b=state["b"],
                brightness=state["brightness"],
                flash=1 if state["flash"] else 0,
                rate=state["flashRate"],
            )
        ).encode("ascii")

    def _send(self, link: serial.Serial) -> None:
        link.write(self.command_for(self.store.get()))
        link.flush()

    def run(self) -> None:
        links: dict[str, serial.Serial] = {}
        last_scan = 0.0
        while True:
            now = time.monotonic()
            if now - last_scan >= 1.0:
                last_scan = now
                desired = set(self.find_data_ports())
                for device in set(links) - desired:
                    log.info("NeoKey removed from %s", device)
                    links.pop(device).close()
                for device in desired - set(links):
                    try:
                        # Non-blocking reads keep state-to-light latency below
                        # the 10 Hz browser update interval.
                        link = serial.Serial(device, 115200, timeout=0, write_timeout=1)
                        links[device] = link
                        log.info("Connected to NeoKey data interface at %s", device)
                        self._send(link)
                    except (OSError, serial.SerialException) as exc:
                        log.warning("Could not open NeoKey at %s: %s", device, exc)
                self._set_devices(
                    list(links),
                    None if links else "No NeoKey data interfaces found",
                )

            if self._changed.wait(0.01):
                self._changed.clear()
                for device, link in tuple(links.items()):
                    try:
                        self._send(link)
                    except (OSError, serial.SerialException) as exc:
                        log.warning("NeoKey serial connection lost at %s: %s", device, exc)
                        link.close()
                        links.pop(device, None)
                        self._set_devices(list(links), str(exc))

            for device, link in tuple(links.items()):
                try:
                    response = link.readline()
                    if response:
                        log.debug(
                            "NeoKey %s: %s",
                            device,
                            response.decode("ascii", "replace").strip(),
                        )
                except (OSError, serial.SerialException) as exc:
                    log.warning("NeoKey serial connection lost at %s: %s", device, exc)
                    link.close()
                    links.pop(device, None)
                    self._set_devices(list(links), str(exc))


store = StateStore()
trinkey = TrinkeySerial(store)
websockets: set[web.WebSocketResponse] = set()


def public_state() -> dict[str, Any]:
    return {**store.get(), **trinkey.status()}


async def broadcast_state() -> None:
    if not websockets:
        return
    sockets = tuple(websockets)
    message = json.dumps({"type": "state", "state": public_state()})
    results = await asyncio.gather(
        *(socket.send_str(message) for socket in sockets),
        return_exceptions=True,
    )
    for socket, result in zip(sockets, results):
        if isinstance(result, BaseException) or socket.closed:
            websockets.discard(socket)


def apply_update(patch: Any) -> dict[str, Any]:
    state = store.update(patch)
    trinkey.notify()
    return {**state, **trinkey.status()}


@web.middleware
async def response_headers(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(public_state())


async def get_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **trinkey.status()})


async def post_state(request: web.Request) -> web.Response:
    try:
        if request.content_length is not None and request.content_length > 4096:
            raise ValueError("invalid request size")
        state = apply_update(await request.json())
        await broadcast_state()
        return web.json_response(state)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await socket.prepare(request)
    websockets.add(socket)
    await socket.send_json({"type": "state", "state": public_state()})
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict):
                        raise ValueError("WebSocket message must be an object")
                    patch = payload.get("patch") if payload.get("type") == "set" else payload
                    apply_update(patch)
                    await broadcast_state()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    await socket.send_json({"type": "error", "error": str(exc)})
            elif message.type == WSMsgType.ERROR:
                log.warning("WebSocket error: %s", socket.exception())
    finally:
        websockets.discard(socket)
    return socket


async def status_publisher(app: web.Application) -> None:
    previous: tuple[Any, ...] | None = None
    ticks = 0
    while True:
        status = trinkey.status()
        current = (
            status["connected"],
            tuple(status.get("devices", [])),
            status["lastError"],
        )
        if current != previous:
            previous = current
            await broadcast_state()
        await asyncio.sleep(0.25)
        ticks += 1
        if ticks % 2 == 0:
            store.persist()


async def background_tasks(app: web.Application):
    trinkey.start()
    task = asyncio.create_task(status_publisher(app))
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    store.persist()


def create_app() -> web.Application:
    app = web.Application(middlewares=[response_headers], client_max_size=4096)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/state", post_state)
    app.router.add_get("/api/health", get_health)
    app.router.add_get("/ws", websocket_handler)
    app.cleanup_ctx.append(background_tasks)
    return app


if __name__ == "__main__":
    log.info("NeoKey controller listening on http://%s:%d", BIND, PORT)
    web.run_app(create_app(), host=BIND, port=PORT, print=None)
