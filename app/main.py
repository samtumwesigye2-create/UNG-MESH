"""UNG-MESH server: fusion engine + live map + APIs.

Environment variables
  OPERATOR_PASSWORD  password for the map and operator actions (required in production)
  INGEST_KEY         key sensors send in the X-API-Key header when posting detections
  SESSION_SECRET     signs login cookies (any long random string)
  ADSB_SOURCE        adsb.lol (default) | airplanes.live | opensky | off
  SIM                on (default) | off   simulated boats, drones, vehicles, radars, cameras
  DATA_DIR           where the SQLite file lives (default ./data; use a Railway volume at /data)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .adsb import AdsbFeed, _from_readsb
from .engine import Engine
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mesh")

PASSWORD = os.environ.get("OPERATOR_PASSWORD") or ""
if not PASSWORD:
    PASSWORD = secrets.token_urlsafe(9)
    log.warning("OPERATOR_PASSWORD not set. Temporary password for this run: %s", PASSWORD)
INGEST_KEY = os.environ.get("INGEST_KEY") or ""
SECRET = (os.environ.get("SESSION_SECRET") or PASSWORD + "-mesh").encode()
ADSB = os.environ.get("ADSB_SOURCE", "adsb.lol").strip().lower()
SIM = os.environ.get("SIM", "on").strip().lower() not in ("off", "0", "false", "no")
DATA = Path(os.environ.get("DATA_DIR", "data"))
STATIC = Path(__file__).resolve().parent.parent / "static"

store = Store(str(DATA / "mesh.db"))
engine = Engine(store, live_adsb_name=None if ADSB == "off" else f"Live ADS-B network ({ADSB})", sim=SIM)
feed = None if ADSB == "off" else AdsbFeed(engine, preferred=ADSB)
clients = {}


async def tick_loop():
    while True:
        try:
            engine.tick()
            if clients:
                base = engine.snapshot(include_raw=False)
                raw = None
                for ws, want_raw in list(clients.items()):
                    msg = base
                    if want_raw:
                        if raw is None:
                            raw = dict(base, raw=engine.snapshot(include_raw=True)["raw"])
                        msg = raw
                    try:
                        await ws.send_text(json.dumps(msg, separators=(",", ":")))
                    except Exception:  # noqa: BLE001
                        clients.pop(ws, None)
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(tick_loop())]
    if feed:
        tasks.append(asyncio.create_task(feed.run()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="UNG-MESH", lifespan=lifespan)


# ---------- auth ----------
def _token():
    return hmac.new(SECRET, b"operator:" + PASSWORD.encode(), hashlib.sha256).hexdigest()


def _authed(cookie):
    return bool(cookie) and hmac.compare_digest(cookie, _token())


def require(request: Request):
    if not _authed(request.cookies.get("mesh")):
        raise HTTPException(401, "Sign in first")


def require_ingest(key):
    if not INGEST_KEY:
        raise HTTPException(503, "INGEST_KEY is not set on the server")
    if not key or not hmac.compare_digest(key, INGEST_KEY):
        raise HTTPException(401, "Wrong or missing X-API-Key")


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), PASSWORD):
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie("mesh", _token(), httponly=True, samesite="strict", secure=secure, max_age=14 * 86400)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("mesh")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    require(request)
    return {"ok": True}


# ---------- pages ----------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"ok": True, "tracks": len(engine.tracks), "adsb": feed.active if feed else "off",
            "adsbAircraft": feed.count if feed else 0, "sim": SIM}


# ---------- state + live updates ----------
@app.get("/api/state")
async def state(request: Request, raw: bool = False):
    require(request)
    return JSONResponse(engine.snapshot(include_raw=raw))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if not _authed(websocket.cookies.get("mesh")):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    clients[websocket] = False
    try:
        await websocket.send_text(json.dumps(engine.snapshot()))
        while True:
            msg = json.loads(await websocket.receive_text())
            if "raw" in msg:
                clients[websocket] = bool(msg["raw"])
    except (WebSocketDisconnect, Exception):  # noqa: BLE001
        clients.pop(websocket, None)


# ---------- operator actions ----------
def _ok(err):
    if err:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/assets/{aid}/task")
async def task(aid: str, request: Request):
    require(request)
    return _ok(engine.task_asset(aid, await request.json()))


@app.post("/api/tracks/{tid}/classify")
async def classify(tid: str, request: Request):
    require(request)
    return _ok(engine.classify(tid, (await request.json()).get("aff")))


@app.post("/api/sensors/{sid}/toggle")
async def toggle(sid: str, request: Request):
    require(request)
    return _ok(engine.toggle_sensor(sid))


@app.post("/api/events/ack")
async def ack(request: Request):
    require(request)
    engine.ack_all()
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request, limit: int = 500, before: int | None = None):
    require(request)
    return store.search_events(min(limit, 2000), before)


@app.post("/api/zones")
async def add_zone(request: Request):
    require(request)
    b = await request.json()
    try:
        name = str(b["name"]).strip()[:60] or "New zone"
        lat, lon, r = float(b["lat"]), float(b["lon"]), max(0.1, min(50.0, float(b["r"])))
        dom = [d for d in b.get("dom", ["air", "surface", "ground"]) if d in ("air", "surface", "ground")] or ["air"]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Zone needs name, lat, lon and r (radius in km)")
    return engine.add_zone(name, lat, lon, r, dom)


@app.delete("/api/zones/{zid}")
async def del_zone(zid: str, request: Request):
    require(request)
    return _ok(None if engine.delete_zone(zid) else "No such zone")


# ---------- sensor ingest ----------
@app.post("/api/ingest")
async def ingest(request: Request, x_api_key: str | None = Header(default=None)):
    """Generic detections from any sensor.
    Body: {"sensor": {"id","name","type","lat","lon","range"},
           "detections": [{"lat","lon","domain","alt","key","call","label","speed_kt","course_deg"}]}"""
    require_ingest(x_api_key)
    b = await request.json()
    s = b.get("sensor") or {}
    if not s.get("id"):
        raise HTTPException(400, "sensor.id is required")
    engine.register_sensor(s)
    return {"accepted": engine.push(s["id"], b.get("detections") or [])}


@app.post("/api/ingest/aircraft-json")
async def ingest_aircraft(request: Request, sensor_id: str, lat: float, lon: float, name: str = "Local ADS-B receiver",
                          x_api_key: str | None = Header(default=None)):
    """Post the aircraft.json file from readsb / dump1090 / tar1090 as-is."""
    require_ingest(x_api_key)
    b = await request.json()
    engine.register_sensor(dict(id=sensor_id, name=name, type="adsb", lat=lat, lon=lon, range=250))
    items = _from_readsb(b.get("aircraft") or b.get("ac") or [])
    for it in items:
        it["interval"] = 2
    return {"accepted": engine.push(sensor_id, items)}
