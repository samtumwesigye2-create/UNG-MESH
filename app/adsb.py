"""Live aircraft feed from public ADS-B networks.

Sources, tried in order until one answers:
  adsb.lol        https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}
  airplanes.live  https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}
  OpenSky         https://opensky-network.org/api/states/all?lamin=..&lomin=..&lamax=..&lomax=..
Coverage over Uganda depends on volunteer receivers nearby, so some hours
will show few or no aircraft. A local receiver (RTL-SDR + readsb) posting to
/api/ingest/aircraft-json fills that gap.
"""
import asyncio
import logging

import httpx

log = logging.getLogger("adsb")


def _from_readsb(ac_list):
    out = []
    for a in ac_list:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_baro", a.get("alt_geom", 0))
        if alt == "ground" or alt is None:
            alt = 0
        hexid = (a.get("hex") or "").strip().upper()
        call = (a.get("flight") or "").strip() or hexid
        out.append(dict(lat=a["lat"], lon=a["lon"], alt=float(alt), domain="air", key="ICAO " + hexid, call=call,
                        label="Aircraft" + (f" ({a['t']})" if a.get("t") else ""), speed_kt=a.get("gs"),
                        course_deg=a.get("track"), interval=10))
    return out


def _from_opensky(states):
    out = []
    for s in states or []:
        if s[5] is None or s[6] is None:
            continue
        hexid = (s[0] or "").upper()
        call = (s[1] or "").strip() or hexid
        out.append(dict(lat=s[6], lon=s[5], alt=(s[7] or 0) * 3.28084, domain="air", key="ICAO " + hexid, call=call,
                        label="Aircraft", speed_kt=(s[9] or 0) * 1.94384 if s[9] is not None else None,
                        course_deg=s[10], interval=15))
    return out


class AdsbFeed:
    def __init__(self, engine, preferred="adsb.lol", lat=0.15, lon=32.55, nm=150, every=10):
        self.engine, self.lat, self.lon, self.nm, self.every = engine, lat, lon, nm, every
        order = ["adsb.lol", "airplanes.live", "opensky"]
        self.order = [preferred] + [o for o in order if o != preferred] if preferred in order else order
        self.active = None
        self.last_ok = None
        self.count = 0

    async def fetch(self, client, src):
        if src in ("adsb.lol", "airplanes.live"):
            host = "api.adsb.lol" if src == "adsb.lol" else "api.airplanes.live"
            r = await client.get(f"https://{host}/v2/point/{self.lat}/{self.lon}/{self.nm}")
            r.raise_for_status()
            return _from_readsb(r.json().get("ac") or r.json().get("aircraft") or [])
        d = self.nm * 1.852 / 111.2
        r = await client.get("https://opensky-network.org/api/states/all",
                             params=dict(lamin=self.lat - d, lamax=self.lat + d, lomin=self.lon - d, lomax=self.lon + d))
        r.raise_for_status()
        return _from_opensky(r.json().get("states"))

    async def run(self):
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "UNG-MESH/1.0"}) as client:
            while True:
                got = None
                for src in self.order:
                    try:
                        got = await self.fetch(client, src)
                        if self.active != src:
                            self.engine.ev("info", f"Live ADS-B feed connected via {src}", "NET")
                            s = self.engine.sensor("NET")
                            if s:
                                s["name"] = f"Live ADS-B network ({src})"
                        self.active = src
                        break
                    except Exception as e:  # noqa: BLE001
                        log.warning("ADS-B source %s failed: %s", src, e)
                if got is None:
                    if self.active is not None:
                        self.engine.ev("warn", "Live ADS-B feed unreachable, retrying", "NET")
                    self.active = None
                else:
                    self.count = len(got)
                    self.engine.push("NET", got)
                await asyncio.sleep(self.every)
