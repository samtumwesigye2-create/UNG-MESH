"""Live boat positions from aisstream.io (free API key).

Covers all of Lake Victoria. Boats with AIS transponders (ferries, cargo,
larger fishing vessels) appear as LIVE surface tracks. Small canoes and most
fishing boats carry no AIS, so they will still only be seen by radar/cameras.

Setting: AISSTREAM_API_KEY. Without it this feed stays off.
"""
import asyncio
import json
import logging
import os

import websockets

log = logging.getLogger("ais")

URL = "wss://stream.aisstream.io/v0/stream"
# Lake Victoria: [[south-west], [north-east]] as [lat, lon]
BOX = [[[-3.1, 31.5], [0.65, 34.9]]]
TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"]


def parse(msg):
    """Turn one aisstream message into a UNG-MESH detection, or None."""
    mtype = msg.get("MessageType")
    if mtype not in TYPES:
        return None
    body = (msg.get("Message") or {}).get(mtype) or {}
    meta = msg.get("MetaData") or {}
    lat = body.get("Latitude", meta.get("latitude", meta.get("Latitude")))
    lon = body.get("Longitude", meta.get("longitude", meta.get("Longitude")))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None
    mmsi = str(meta.get("MMSI") or body.get("UserID") or "").strip()
    if not mmsi:
        return None
    name = (meta.get("ShipName") or "").strip() or f"MMSI {mmsi}"
    sog, cog = body.get("Sog"), body.get("Cog")
    if sog is not None and sog >= 102.3:  # 102.3 = "not available"
        sog = None
    if cog is not None and cog >= 360:
        cog = None
    return dict(lat=lat, lon=lon, alt=0, domain="surface", key="MMSI " + mmsi, call=name,
                label="Vessel (AIS" + (" class B)" if "ClassB" in mtype else ")"),
                speed_kt=sog if cog is not None else None, course_deg=cog if sog is not None else None, interval=60)


class AisFeed:
    def __init__(self, engine, api_key):
        self.engine = engine
        self.key = api_key
        self.count = 0
        self.connected = False
        self.vessels = set()

    async def run(self):
        backoff = 5
        while True:
            try:
                async with websockets.connect(URL, open_timeout=15, ping_interval=30) as ws:
                    await ws.send(json.dumps({"APIKey": self.key, "BoundingBoxes": BOX, "FilterMessageTypes": TYPES}))
                    if not self.connected:
                        self.engine.ev("info", "Live AIS feed connected (aisstream.io, Lake Victoria)", "AIS-NET")
                    self.connected = True
                    backoff = 5
                    log.info("AIS connected, listening for Lake Victoria vessels")
                    last_report = asyncio.get_event_loop().time()
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if "error" in msg:
                            log.warning("aisstream error: %s", msg["error"])
                            self.engine.ev("warn", f"AIS feed refused: {msg['error']}", "AIS-NET")
                            await asyncio.sleep(300)
                            break
                        now = asyncio.get_event_loop().time()
                        if now - last_report > 600:
                            last_report = now
                            log.info("AIS status: %d messages, %d vessels so far", self.count, len(self.vessels))
                        det = parse(msg)
                        if det:
                            self.count += 1
                            self.vessels.add(det["key"])
                            self.engine.push("AIS-NET", [det])
            except Exception as e:  # noqa: BLE001
                log.warning("AIS connection problem: %s", e)
                if self.connected:
                    self.engine.ev("warn", "Live AIS feed disconnected, retrying", "AIS-NET")
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
