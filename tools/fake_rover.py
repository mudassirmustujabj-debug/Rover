"""Fake rover for developing the dashboard with no hardware.

    pip install -r requirements-dev.txt
    python tools/fake_rover.py            # then open http://localhost:8080/

- Serves the ../dashboard folder as static files.
- Speaks the protocol in docs/protocol.md on ws://localhost:8080/ws
- Sensor values are simulated and fed through the REAL rover/anomaly.py,
  so the dashboard sees the same states/alerts the real rover will produce.
- Trigger problems from a second browser tab or curl:
      /inject?what=gas      gas spike for 15 s
      /inject?what=temp     temperature jump for 15 s
      /inject?what=hum      humidity jump for 15 s
      /inject?what=room     move to a "different room" permanently (tests relearn)
- Implements the 500 ms drive failsafe so you can see the motors "stop".
"""

import asyncio
import json
import pathlib
import random
import sys
import time

from aiohttp import WSMsgType, web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rover"))
from anomaly import default_detector  # noqa: E402

FAILSAFE_S = 0.5
HISTORY_MAX = 50


def clamp(v, lo=-1.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


class FakeRover:
    def __init__(self):
        self.det = default_detector()
        self.rng = random.Random()
        self.clients = set()
        self.history = []
        self.left = 0.0
        self.right = 0.0
        self.last_drive = 0.0
        self.t0 = time.time()
        # simulated environment: level + noise per sensor
        self.base = {"gas": 400.0, "temp": 25.0, "hum": 50.0}
        self.noise = {"gas": 12.0, "temp": 0.15, "hum": 0.6}
        self.offset = {"gas": 0.0, "temp": 0.0, "hum": 0.0}
        self.inject_until = {}
        self.dist = 120.0

    # -- simulation -------------------------------------------------------
    def read_sensors(self):
        now = time.time()
        out = {}
        for k in self.base:
            bump = 0.0
            if self.inject_until.get(k, 0) > now:
                bump = {"gas": 1200.0, "temp": 9.0, "hum": 25.0}[k]
            out[k] = self.base[k] + self.offset[k] + bump + self.rng.gauss(0, self.noise[k])
        out["gas"] = int(out["gas"])
        out["temp"] = round(out["temp"], 1)
        out["hum"] = round(out["hum"], 1)
        # distance: wanders, shrinks when driving forward
        self.dist = max(5.0, min(300.0, self.dist + self.rng.gauss(0, 2) - 3 * (self.left + self.right) / 2))
        out["dist"] = int(self.dist)
        return out

    def inject(self, what):
        now = time.time()
        if what == "room":
            self.offset = {"gas": 150.0, "temp": 7.0, "hum": 15.0}
            return "moved to a new room (offsets applied permanently)"
        if what in ("gas", "temp", "hum"):
            self.inject_until[what] = now + 15
            return what + " spike for 15 s"
        return "unknown: use gas | temp | hum | room"

    # -- protocol ---------------------------------------------------------
    def on_drive(self, x, y):
        x, y = clamp(float(x)), clamp(float(y))
        left, right = y + x, y - x
        m = max(1.0, abs(left), abs(right))
        self.left, self.right = left / m, right / m
        self.last_drive = time.time()

    async def broadcast(self, msg):
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except ConnectionResetError:
                self.clients.discard(ws)

    async def loop(self):
        while True:
            await asyncio.sleep(1.0)
            if time.time() - self.last_drive > FAILSAFE_S:
                self.left = self.right = 0.0  # failsafe
            ts = int(time.time() - self.t0)
            s = self.read_sensors()
            events = self.det.update(s)
            tele = {"t": "telemetry", "ts": ts, "gas": s["gas"], "temp": s["temp"],
                    "hum": s["hum"], "dist": s["dist"], "state": self.det.states(),
                    "drive": {"l": round(self.left, 2), "r": round(self.right, 2)}}
            await self.broadcast(tele)
            for name, kind, value, z in events:
                alert = {"t": "alert", "ts": ts, "sensor": name, "kind": kind,
                         "value": value, "z": round(z, 1)}
                self.history.append(alert)
                self.history = self.history[-HISTORY_MAX:]
                await self.broadcast(alert)


rover = FakeRover()


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    rover.clients.add(ws)
    await ws.send_str(json.dumps({"t": "history", "alerts": rover.history}))
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            m = json.loads(msg.data)
            if m.get("t") == "drive":
                rover.on_drive(m.get("x", 0), m.get("y", 0))
            elif m.get("t") == "reset_baseline":
                rover.det.reset()
        except (ValueError, TypeError):
            pass  # the real rover must also survive garbage
    rover.clients.discard(ws)
    return ws


async def inject_handler(request):
    return web.Response(text=rover.inject(request.query.get("what", "")) + "\n")


async def index_handler(request):
    p = ROOT / "dashboard" / "index.html"
    if p.exists():
        return web.FileResponse(p)
    return web.Response(text="dashboard/index.html not written yet. WebSocket is live at /ws\n")


async def start_loop(app):
    app["loop_task"] = asyncio.create_task(rover.loop())


async def stop_loop(app):
    app["loop_task"].cancel()


def make_app():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/inject", inject_handler)
    app.router.add_get("/", index_handler)
    (ROOT / "dashboard").mkdir(exist_ok=True)
    app.router.add_static("/", ROOT / "dashboard")
    app.on_startup.append(start_loop)
    app.on_cleanup.append(stop_loop)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=8080)
