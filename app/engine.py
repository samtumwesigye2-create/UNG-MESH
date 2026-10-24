"""UNG-MESH fusion engine.

Takes detections from many sensors (radar, ADS-B, AIS, cameras, field assets,
and anything posted to the ingest API), merges them into one set of tracks,
checks restricted zones, raises alerts and moves tasked assets.

Boats, drones, ground vehicles and the local radars/cameras are SIMULATED
until real feeds are connected. Aircraft come from a live ADS-B network when
ADSB_SOURCE is not "off". Every track carries a `sim` flag so the map can say
which is which.
"""
import math
import random
import time

KM = 111.2  # km per degree (good enough near the equator)


def dist(a, b, c, d):
    return math.hypot((a - c) * KM, (b - d) * KM)


def brg(a, b, c, d):
    return (math.degrees(math.atan2(d - b, c - a)) + 360) % 360


def kms(kt):
    return kt * 1.852 / 3600


def move_toward(o, la, lo, km):
    d = dist(o["lat"], o["lon"], la, lo)
    if d <= km or d < 1e-6:
        o["lat"], o["lon"] = la, lo
        return True
    f = km / d
    o["lat"] += (la - o["lat"]) * f
    o["lon"] += (lo - o["lon"]) * f
    return False


# Stylised shoreline of Lake Victoria around Entebbe / Murchison Bay
LAKE = [[0.30, 32.95], [0.30, 32.72], [0.295, 32.68], [0.285, 32.655], [0.27, 32.645], [0.245, 32.640], [0.24, 32.625],
        [0.255, 32.610], [0.235, 32.595], [0.205, 32.585], [0.17, 32.575], [0.14, 32.560], [0.115, 32.535],
        [0.095, 32.510], [0.075, 32.495], [0.055, 32.485], [0.035, 32.470], [0.020, 32.455], [0.015, 32.440],
        [0.025, 32.425], [0.045, 32.415], [0.060, 32.395], [0.085, 32.380], [0.095, 32.350], [0.080, 32.320],
        [0.050, 32.290], [0.020, 32.250], [0.0, 32.10], [-0.40, 32.10], [-0.40, 32.95]]
ROAD = [[0.315, 32.582], [0.27, 32.575], [0.235, 32.565], [0.198, 32.552], [0.16, 32.535], [0.12, 32.51],
        [0.09, 32.49], [0.065, 32.475], [0.05, 32.462]]
AIRPORT = (0.0424, 32.4435)
CENTER = (0.15, 32.55)


def in_lake(la, lo):
    c = False
    j = len(LAKE) - 1
    for i in range(len(LAKE)):
        yi, xi = LAKE[i]
        yj, xj = LAKE[j]
        if (yi > la) != (yj > la) and lo < (xj - xi) * (la - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


DEFAULT_ZONES = [
    {"id": "Z1", "name": "Entebbe Airport (HUEN) core", "lat": 0.0424, "lon": 32.4435, "r": 3.0, "dom": ["air"]},
    {"id": "Z2", "name": "State House Entebbe", "lat": 0.0655, "lon": 32.4730, "r": 1.0, "dom": ["air", "ground", "surface"]},
    {"id": "Z3", "name": "Port Bell harbour", "lat": 0.288, "lon": 32.652, "r": 1.5, "dom": ["surface", "air"]},
    {"id": "Z4", "name": "Kampala CBD no-drone zone", "lat": 0.315, "lon": 32.582, "r": 2.0, "dom": ["air"]},
]

CALLS = ["UR 302", "UR 441", "KQ 414", "ET 334", "QR 1385", "UR 216", "RW 462", "EK 729", "UR 100", "TC 642"]
BOATS = ["FV Nalubaale 12", "MV Kalangala", "FV Ssese Star", "Ferry Gaba Express", "FV Bugala 4", "MV Lutembe"]


def _small(o):
    return o["rcs"] == "small"


def _make_sensors(live_adsb_name):
    s = [
        dict(id="R1", name="Entebbe ASR radar", type="radar", lat=0.045, lon=32.448, range=45, noise=0.30,
             sees=lambda o: o["domain"] == "air" and o["alt"] > 150, pd=lambda o: 0.25 if _small(o) else 0.93),
        dict(id="R2", name="Port Bell coastal radar", type="radar", lat=0.284, lon=32.655, range=24, noise=0.15,
             sees=lambda o: o["domain"] == "surface" or (o["domain"] == "air" and o["alt"] < 3000),
             pd=lambda o: 0.45 if _small(o) else 0.9),
        dict(id="R3", name="Entebbe coastal radar", type="radar", lat=0.030, lon=32.480, range=24, noise=0.15,
             sees=lambda o: o["domain"] == "surface" or (o["domain"] == "air" and o["alt"] < 3000),
             pd=lambda o: 0.45 if _small(o) else 0.9),
        dict(id="S1", name="AIS shore station Gaba", type="ais", lat=0.247, lon=32.636, range=40, noise=0.02,
             sees=lambda o: o["tx"] == "ais", pd=lambda o: 0.9),
        dict(id="E1", name="EO/IR tower Kitooro", type="eo", lat=0.058, lon=32.468, range=6, noise=0.05,
             sees=lambda o: True, pd=lambda o: 0.95),
        dict(id="E2", name="EO/IR tower Kajjansi", type="eo", lat=0.205, lon=32.555, range=6, noise=0.05,
             sees=lambda o: True, pd=lambda o: 0.95),
        dict(id="E3", name="EO/IR tower Port Bell", type="eo", lat=0.292, lon=32.648, range=6, noise=0.05,
             sees=lambda o: True, pd=lambda o: 0.95),
    ]
    for x in s:
        x["sim"] = True
    if live_adsb_name:
        s.append(dict(id="NET", name=live_adsb_name, type="adsb", lat=CENTER[0], lon=CENTER[1], range=278,
                      noise=0, sees=None, pd=None, sim=False))
    else:
        s.append(dict(id="A1", name="ADS-B receiver Kampala (simulated)", type="adsb", lat=0.330, lon=32.580,
                      range=250, noise=0.02, sees=lambda o: o["tx"] == "adsb", pd=lambda o: 0.98, sim=True))
    for x in s:
        x.update(on=True, hits=[], last_seen=None, external=False)
    return s


def _make_assets():
    a = [
        dict(id="UAV-1", name="Patrol UAV 1", domain="air", home=[0.050, 32.455], homeName="Entebbe base", spd=65, eo=2.0, batt=92.0),
        dict(id="UAV-2", name="Patrol UAV 2", domain="air", home=[0.198, 32.552], homeName="Kajjansi airfield", spd=65, eo=2.0, batt=100.0),
        dict(id="PB-1", name="Patrol boat 1", domain="surface", home=[0.286, 32.650], homeName="Port Bell", spd=28, eo=1.5, batt=None),
        dict(id="GT-1", name="Ground team 1", domain="ground", home=[0.070, 32.470], homeName="Entebbe", spd=30, eo=1.0, batt=None),
    ]
    for x in a:
        x.update(lat=x["home"][0], lon=x["home"][1], task={"type": "idle"}, hdg=0, ang=0.0, trail=[], sim=True)
    return a


class Engine:
    def __init__(self, store, live_adsb_name=None, sim=True):
        self.store = store
        self.sim = sim
        self.live_adsb = bool(live_adsb_name)
        self.now = time.time()
        self.truths = {}
        self.tracks = {}
        self.raw = []
        self.det_log = []
        self.pending = []  # detections pushed in from outside (ADS-B network, ingest API)
        self.next_truth = 1
        self.next_track = store.next_track_number()
        self.sensors = _make_sensors(live_adsb_name)
        self.assets = _make_assets()
        self.zones = store.load_zones() or [dict(z) for z in DEFAULT_ZONES]
        if not store.load_zones():
            for z in self.zones:
                store.save_zone(z)
        self.events = store.recent_events(150)
        self.ev("info", f"UNG-MESH started: {len(self.sensors)} sensors, {len(self.assets)} assets"
                + (", live ADS-B on" if self.live_adsb else ""))
        self.assets[0]["task"] = {"type": "patrol", "target": self.zones[0]["id"]}
        if len(self.zones) > 2:
            self.assets[2]["task"] = {"type": "patrol", "target": self.zones[2]["id"]}
        if sim:
            self._seed()

    # ---------- events ----------
    def ev(self, sev, msg, ref=None):
        e = self.store.add_event(self.now, sev, msg, ref)
        self.events.insert(0, e)
        del self.events[200:]

    def ack_all(self):
        self.store.ack_all()
        for e in self.events:
            e["ack"] = True

    # ---------- simulated world ----------
    def _uniq(self, lst):
        used = {o.get("call") for o in self.truths.values()}
        free = [c for c in lst if c not in used]
        return random.choice(free) if free else f"{random.choice(lst)} {random.randint(2, 99)}"

    def _new(self, o):
        o["id"] = f"X{self.next_truth}"
        self.next_truth += 1
        o.setdefault("ri", 0)
        self.truths[o["id"]] = o
        return o

    def rand_lake(self):
        for _ in range(300):
            la, lo = random.uniform(-0.18, 0.28), random.uniform(32.3, 32.85)
            if in_lake(la, lo) and in_lake(la + 0.01, lo) and in_lake(la - 0.01, lo):
                return [la, lo]
        return [-0.05, 32.6]

    def rand_land(self, la, lo, r):
        for _ in range(300):
            a, b = la + random.uniform(-r, r), lo + random.uniform(-r, r)
            if not in_lake(a, b):
                return [a, b]
        return [la, lo]

    def spawn_arrival(self, progress=0.0):
        frm = random.choice([[0.55, 32.80], [0.55, 32.30], [0.40, 32.95], [0.50, 32.15]])
        fix = [0.13, 32.42]
        o = self._new(dict(kind="arrival", domain="air", lat=frm[0], lon=frm[1], alt=10000, spd=250, route=[fix, list(AIRPORT)],
                           tx="adsb", call=self._uniq(CALLS), rcs="large", susp=False, label="Airliner",
                           idLabel="Airliner (scheduled flight)"))
        o["lat"] += (fix[0] - o["lat"]) * progress
        o["lon"] += (fix[1] - o["lon"]) * progress

    def spawn_departure(self):
        to = random.choice([[0.55, 32.70], [-0.25, 32.40], [0.55, 32.20], [-0.25, 32.90]])
        self._new(dict(kind="departure", domain="air", lat=AIRPORT[0], lon=AIRPORT[1], alt=0, spd=160, route=[[-0.02, 32.47], to],
                       tx="adsb", call=self._uniq(CALLS), rcs="large", susp=False, label="Airliner", idLabel="Airliner (scheduled flight)"))

    def spawn_boat(self, at=None, ais=None, susp=False, route=None, life=None):
        p = at or self.rand_lake()
        ais = (random.random() < 0.6) if ais is None else ais
        self._new(dict(kind="boat", domain="surface", lat=p[0], lon=p[1], alt=0, spd=random.uniform(6, 12),
                       route=route or [self.rand_lake()], tx="ais" if ais else None, call=self._uniq(BOATS) if ais else None,
                       rcs="med", susp=susp, life=life or random.uniform(400, 900),
                       label="Vessel" if ais else "Small boat",
                       idLabel="Unregistered boat, no lights, no AIS" if susp else ("Fishing / passenger vessel" if ais else "Fishing boat, crew of 2")))

    def spawn_drone(self, susp, zone=None, at=None):
        air_zones = [z for z in self.zones if "air" in z["dom"]] or self.zones
        z = zone or random.choice(air_zones)
        p = at or self.rand_land(z["lat"], z["lon"], 0.04)
        self._new(dict(kind="drone", domain="air", lat=p[0], lon=p[1], alt=random.uniform(200, 400), spd=random.uniform(24, 32),
                       route=[[z["lat"] + random.uniform(-.003, .003), z["lon"] + random.uniform(-.003, .003)]] if susp else [self.rand_land(p[0], p[1], 0.02)],
                       tx=None, call=None, rcs="small", susp=susp, zone=z, loiterEnd=None, life=random.uniform(300, 600),
                       label="Small quadcopter",
                       idLabel="Quadcopter, no Remote ID, camera payload" if susp else "Hobby drone, operator in view"))

    def spawn_vehicle(self, mid=False):
        r = ROAD[:] if random.random() < 0.5 else ROAD[::-1]
        o = self._new(dict(kind="vehicle", domain="ground", lat=r[0][0], lon=r[0][1], alt=0, spd=random.uniform(22, 34),
                           route=[list(p) for p in r[1:]], tx=None, call=None, rcs="med", susp=False, label="Vehicle",
                           idLabel=random.choice(["Minibus taxi", "Delivery truck", "Private car", "Boda boda"])))
        if mid:
            k = random.randint(0, len(o["route"]) - 2)
            o["lat"], o["lon"] = o["route"][k]
            o["ri"] = k + 1

    def _seed(self):
        if not self.live_adsb:
            self.spawn_arrival(0.35)
            self.spawn_arrival(0.75)
            self.spawn_departure()
        for _ in range(4):
            self.spawn_boat()
        self.spawn_boat(at=[0.262, 32.668], ais=False, susp=True, route=[[0.284, 32.656]], life=600)
        for _ in range(3):
            self.spawn_vehicle(mid=True)
        self.spawn_drone(True, zone=self.zones[1] if len(self.zones) > 1 else None, at=[0.085, 32.492])
        self.last_susp_drone = self.now

    def _step_truth(self, o, dt):
        if o["ri"] < len(o["route"]):
            tl, tn = o["route"][o["ri"]]
            o["hdg"] = brg(o["lat"], o["lon"], tl, tn)
            if move_toward(o, tl, tn, kms(o["spd"]) * dt):
                o["ri"] += 1
        k = o["kind"]
        if k == "arrival":
            d = dist(o["lat"], o["lon"], *AIRPORT)
            o["alt"] = min(10000, d * 330)
            o["spd"] = max(140, min(250, 140 + d * 3))
        if k == "departure":
            d = dist(o["lat"], o["lon"], *AIRPORT)
            o["alt"] = min(16000, d * 420)
            o["spd"] = min(280, 160 + d * 4)
        if "life" in o:
            o["life"] -= dt
        if o["ri"] >= len(o["route"]):
            if k == "arrival":
                return False
            if k in ("departure", "vehicle"):
                return False
            if k == "boat":
                if o["life"] <= 0:
                    return False
                o["route"], o["ri"] = [self.rand_lake()], 0
            if k == "drone":
                if o.get("exiting"):
                    return False
                if o["susp"]:
                    if o["loiterEnd"] is None:
                        o["loiterEnd"] = self.now + random.uniform(150, 260)
                    if self.now < o["loiterEnd"]:
                        a = random.uniform(0, 2 * math.pi)
                        z = o["zone"]
                        o["route"], o["ri"] = [[z["lat"] + math.cos(a) * 0.4 / KM, z["lon"] + math.sin(a) * 0.4 / KM]], 0
                    else:
                        o["exiting"] = True
                        o["route"], o["ri"] = [self.rand_land(o["lat"], o["lon"], 0.12)], 0
                else:
                    if o["life"] <= 0:
                        return False
                    o["route"], o["ri"] = [self.rand_land(o["lat"], o["lon"], 0.02)], 0
        return -0.25 < o["lat"] < 0.55 and 32.15 < o["lon"] < 32.95

    # ---------- sensing ----------
    def _det(self, sid, typ, o, noise):
        n = noise / KM
        return dict(sid=sid, type=typ, lat=o["lat"] + random.gauss(0, 1) * n, lon=o["lon"] + random.gauss(0, 1) * n,
                    alt=o["alt"], domain=o["domain"], key=o["call"] if typ in ("adsb", "ais") else None,
                    call=o["call"] if typ in ("adsb", "ais") else None,
                    label=o["label"] if typ == "eo" else ("Airliner" if typ == "adsb" else "Vessel" if typ == "ais" else None),
                    close=False, truth=o["id"], t=self.now, sim=True, vel=None)

    def _sense(self):
        dets = []
        for s in self.sensors:
            if not s["on"] or s["sees"] is None:
                continue
            for o in self.truths.values():
                if not s["sees"](o) or dist(s["lat"], s["lon"], o["lat"], o["lon"]) > s["range"]:
                    continue
                if random.random() > s["pd"](o):
                    continue
                dets.append(self._det(s["id"], s["type"], o, s["noise"]))
                s["hits"].append(self.now)
        for a in self.assets:
            for o in self.truths.values():
                d = dist(a["lat"], a["lon"], o["lat"], o["lon"])
                if d > a["eo"]:
                    continue
                x = self._det(a["id"], "eo", o, 0.03)
                x["close"] = d < 1.0
                dets.append(x)
        return dets

    # ---------- external input ----------
    def sensor(self, sid):
        return next((s for s in self.sensors if s["id"] == sid), None)

    def register_sensor(self, info):
        s = self.sensor(info["id"])
        if not s:
            s = dict(id=info["id"], name=info.get("name", info["id"]), type=info.get("type", "other"),
                     lat=info.get("lat", CENTER[0]), lon=info.get("lon", CENTER[1]), range=info.get("range", 10),
                     noise=0, sees=None, pd=None, sim=False, on=True, hits=[], last_seen=None, external=True)
            self.sensors.append(s)
            self.ev("info", f"New sensor connected: {s['id']} {s['name']}", s["id"])
        else:
            for k in ("name", "type", "lat", "lon", "range"):
                if k in info:
                    s[k] = info[k]
        s["last_seen"] = time.time()
        return s

    def push(self, sid, items):
        """Queue real detections. Each item: lat, lon, domain, and optionally alt (ft), key (unique id such
        as ICAO hex or MMSI), call (display name), label, speed_kt, course_deg."""
        s = self.sensor(sid)
        if s is None or not s["on"]:
            return 0
        now = time.time()
        n = 0
        for it in items:
            try:
                lat, lon = float(it["lat"]), float(it["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            vel = None
            if it.get("speed_kt") is not None and it.get("course_deg") is not None:
                v = kms(float(it["speed_kt"])) / KM
                c = math.radians(float(it["course_deg"]))
                vel = (v * math.cos(c), v * math.sin(c))
            self.pending.append(dict(sid=sid, type=s["type"], lat=lat, lon=lon, alt=float(it.get("alt") or 0),
                                     domain=it.get("domain", "air"), key=it.get("key"), call=it.get("call") or it.get("key"),
                                     label=it.get("label"), close=False, truth=None, t=now, sim=False, vel=vel,
                                     interval=float(it.get("interval", 1))))
            n += 1
        s["hits"].extend([now] * n)
        s["last_seen"] = now
        return n

    # ---------- fusion ----------
    @staticmethod
    def _gate(dom):
        return 1.6 if dom == "air" else 0.7

    def _fuse(self, dets):
        now = self.now
        for d in dets:
            self.raw.append(d)
            self.det_log.append(now)
            best, bd = None, 1e9
            for tr in self.tracks.values():
                if tr["domain"] != d["domain"] or tr["status"] == "lost":
                    continue
                if d["key"] and tr["key"]:
                    if d["key"] == tr["key"]:
                        best, bd = tr, 0
                        break
                    continue
                dt = now - tr["last"]
                pl, po = tr["lat"] + tr["vlat"] * dt, tr["lon"] + tr["vlon"] * dt
                dd = dist(pl, po, d["lat"], d["lon"])
                if dd < self._gate(d["domain"]) and dd < bd:
                    bd, best = dd, tr
            if best is None:
                best = dict(id=f"T-{self.next_track:04d}", domain=d["domain"], lat=d["lat"], lon=d["lon"], vlat=0.0, vlon=0.0,
                            alt=d["alt"], src={}, key=None, call=None, aff="unknown", affSrc="auto", label="Unknown " + d["domain"],
                            first=now, last=now, hist=[], zones=set(), status="live", identified=False, truth=None, n=0,
                            sim=d["sim"], firstSid=d["sid"], warned=False, interval=1.0)
                self.next_track += 1
                self.tracks[best["id"]] = best
            else:
                dt = now - best["last"]
                pl, po = best["lat"] + best["vlat"] * dt, best["lon"] + best["vlon"] * dt
                a = 0.8 if d["type"] in ("adsb", "ais", "eo") else 0.3
                best["lat"], best["lon"] = pl + a * (d["lat"] - pl), po + a * (d["lon"] - po)
                best["last"], best["alt"] = now, d["alt"]
            if d["vel"]:
                best["vlat"], best["vlon"] = d["vel"]
                best["hasVel"] = True
            if not d["sim"]:
                best["interval"] = max(1.0, d.get("interval", 1.0))
            best["n"] += 1
            best["src"][d["sid"]] = now
            best["status"] = "live"
            if d["truth"]:
                best["truth"] = d["truth"]
            if d["key"] and not best["key"]:
                best["key"] = d["key"]
            if d["call"] and best["call"] != d["call"]:
                best["call"] = d["call"]
                if best["affSrc"] == "auto" and best["aff"] != "suspect":
                    best["aff"] = "neutral"
                    best["label"] = (d["label"] or ("Aircraft" if d["domain"] == "air" else "Vessel")) + " · " + d["call"]
            if d["label"] and not best["call"] and not best["identified"] and best["label"].startswith("Unknown"):
                best["label"] = d["label"] + (" (EO)" if d["type"] == "eo" else "")
            if d["close"] and not best["identified"] and not best["call"]:
                self._identify(best, d["sid"])
        self.raw = [r for r in self.raw if now - r["t"] < 4]
        self.det_log = [t for t in self.det_log if now - t < 60]
        self._merge()
        for tr in list(self.tracks.values()):
            if tr["last"] == now:
                tr["hist"].append([tr["lat"], tr["lon"], now])
                del tr["hist"][:-50]
                if not tr.get("hasVel"):
                    e = next((h for h in tr["hist"] if now - h[2] <= 12), None)
                    if e and now - e[2] >= 4:
                        span = now - e[2]
                        tr["vlat"] = tr["vlat"] * 0.5 + (tr["lat"] - e[0]) / span * 0.5
                        tr["vlon"] = tr["vlon"] * 0.5 + (tr["lon"] - e[1]) / span * 0.5
            if (not tr["warned"] and tr["domain"] == "air" and not tr["call"] and tr["status"] == "live"
                    and now - tr["first"] >= 4):
                tr["warned"] = True
                self.ev("warn", f"New unknown air track {tr['id']} (first seen by {self.sensor_name(tr['firstSid'])})", tr["id"])
            age = now - tr["last"]
            coast = 6 + tr["interval"] * 1.5
            if age > coast and tr["status"] == "live":
                tr["status"] = "coasting"
            if age > coast + 16 and tr["status"] != "lost":
                tr["status"] = "lost"
                if tr["aff"] in ("suspect", "unknown"):
                    self.ev("warn" if tr["aff"] == "suspect" else "info", f"Lost contact with {tr['id']} ({tr['label']})", tr["id"])
            if age > coast + 34:
                del self.tracks[tr["id"]]

    def _merge(self):
        live = [t for t in self.tracks.values() if t["status"] != "lost"]
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                A, B = live[i], live[j]
                if A["id"] not in self.tracks or B["id"] not in self.tracks or A["domain"] != B["domain"]:
                    continue
                if A["key"] and B["key"]:
                    continue
                if dist(A["lat"], A["lon"], B["lat"], B["lon"]) > (1.2 if A["domain"] == "air" else 0.35):
                    continue
                keep, drop = A, B
                if (B["key"] and not A["key"]) or (bool(B["key"]) == bool(A["key"]) and B["first"] < A["first"]):
                    keep, drop = B, A
                for k, v in drop["src"].items():
                    keep["src"][k] = max(keep["src"].get(k, 0), v)
                keep["zones"] |= drop["zones"]
                if drop["identified"] and not keep["identified"]:
                    keep.update(identified=True, label=drop["label"], aff=drop["aff"], affSrc=drop["affSrc"])
                if drop["affSrc"] == "operator" and keep["affSrc"] != "operator":
                    keep.update(aff=drop["aff"], affSrc="operator")
                if drop["aff"] == "suspect" and keep["aff"] == "unknown":
                    keep["aff"] = "suspect"
                keep["call"] = keep["call"] or drop["call"]
                keep["key"] = keep["key"] or drop["key"]
                keep["last"] = max(keep["last"], drop["last"])
                keep["sim"] = keep["sim"] and drop["sim"]
                del self.tracks[drop["id"]]
                for a in self.assets:
                    if a["task"]["type"] == "investigate" and a["task"].get("target") == drop["id"]:
                        a["task"]["target"] = keep["id"]

    def _identify(self, tr, by):
        o = self.truths.get(tr["truth"])
        if not o:
            return
        tr["identified"] = True
        tr["label"] = o["idLabel"] + (" · " + tr["call"] if tr["call"] else "")
        if tr["affSrc"] != "operator":
            tr["aff"] = "suspect" if o["susp"] else "neutral"
            tr["affSrc"] = "identified"
        self.ev("crit" if o["susp"] else "info", f"{by} identified {tr['id']}: {o['idLabel']}", tr["id"])

    def sensor_name(self, sid):
        s = self.sensor(sid)
        if s:
            return s["name"]
        a = next((a for a in self.assets if a["id"] == sid), None)
        return a["name"] if a else sid

    def conf(self, tr):
        fresh = sum(1 for t in tr["src"].values() if self.now - t < 6 + tr["interval"])
        base = min(0.97, 0.3 + 0.2 * fresh + (0.15 if tr["call"] else 0) + (0.2 if tr["identified"] else 0))
        return max(0.05, base - max(0, self.now - tr["last"] - 3 - tr["interval"]) * 0.03)

    # ---------- zones ----------
    def _zone_check(self):
        for tr in self.tracks.values():
            if tr["status"] == "lost":
                continue
            for z in self.zones:
                if tr["domain"] not in z["dom"]:
                    continue
                inside = dist(tr["lat"], tr["lon"], z["lat"], z["lon"]) < z["r"]
                if inside and z["id"] not in tr["zones"] and not tr["call"] and self.now - tr["first"] < 4:
                    continue  # give transponder feeds a few seconds to name the contact first
                if inside and z["id"] not in tr["zones"]:
                    tr["zones"].add(z["id"])
                    if tr["aff"] in ("unknown", "suspect"):
                        if tr["affSrc"] == "auto" and tr["aff"] == "unknown":
                            tr["aff"] = "suspect"
                        self.ev("crit", f"{tr['id']} ({tr['label']}) entered {z['name']}", tr["id"])
                elif not inside and z["id"] in tr["zones"]:
                    tr["zones"].discard(z["id"])

    def add_zone(self, name, lat, lon, r, dom):
        zid = "Z" + str(max([int(z["id"][1:]) for z in self.zones if z["id"][1:].isdigit()] + [0]) + 1)
        z = dict(id=zid, name=name, lat=lat, lon=lon, r=r, dom=dom)
        self.zones.append(z)
        self.store.save_zone(z)
        self.ev("info", f"Operator added zone {zid} {name} ({r} km)", zid)
        return z

    def delete_zone(self, zid):
        z = next((z for z in self.zones if z["id"] == zid), None)
        if not z:
            return False
        self.zones.remove(z)
        self.store.delete_zone(zid)
        for t in self.tracks.values():
            t["zones"].discard(zid)
        self.ev("info", f"Operator removed zone {zid} {z['name']}", None)
        return True

    # ---------- assets ----------
    def _step_asset(self, a, dt):
        step = kms(a["spd"]) * dt
        T = a["task"]
        home = a["home"]
        at_home = dist(a["lat"], a["lon"], *home) < 0.1
        if a["batt"] is not None:
            if T["type"] == "idle" and at_home:
                a["batt"] = min(100.0, a["batt"] + 0.4 * dt)
            else:
                a["batt"] = max(0.0, a["batt"] - 0.035 * dt)
            if a["batt"] < 20 and T["type"] != "rtb" and not (T["type"] == "idle" and at_home):
                a["task"] = {"type": "rtb"}
                T = a["task"]
                self.ev("warn", f"{a['id']} battery {a['batt']:.0f}%, returning to {a['homeName']}", a["id"])
        tgt = None
        if T["type"] == "rtb":
            tgt = home
            if dist(a["lat"], a["lon"], *home) < 0.05:
                a["task"] = {"type": "idle"}
                self.ev("info", f"{a['id']} back at {a['homeName']}", a["id"])
        elif T["type"] == "investigate":
            tr = self.tracks.get(T["target"])
            if not tr or tr["status"] == "lost":
                self.ev("warn", f"{a['id']}: target {T['target']} lost, holding position", a["id"])
                a["task"] = {"type": "idle"}
            else:
                age = self.now - tr["last"]
                tgt = [tr["lat"] + tr["vlat"] * age, tr["lon"] + tr["vlon"] * age]
                if dist(a["lat"], a["lon"], *tgt) < 0.35:
                    tgt = None
                    if not T.get("onStation"):
                        T["onStation"] = True
                        self.ev("info", f"{a['id']} on station with {tr['id']}", tr["id"])
        elif T["type"] == "patrol":
            z = next((z for z in self.zones if z["id"] == T["target"]), None)
            if not z:
                a["task"] = {"type": "idle"}
            else:
                R = (z["r"] + 0.4)
                d = dist(a["lat"], a["lon"], z["lat"], z["lon"])
                if abs(d - R) > 0.4:
                    a["ang"] = math.atan2(a["lon"] - z["lon"], a["lat"] - z["lat"])
                else:
                    a["ang"] += step / R
                tgt = [z["lat"] + math.cos(a["ang"]) * R / KM, z["lon"] + math.sin(a["ang"]) * R / KM]
        if tgt:
            a["hdg"] = brg(a["lat"], a["lon"], *tgt)
            move_toward(a, tgt[0], tgt[1], step)
        a["trail"].append([a["lat"], a["lon"]])
        del a["trail"][:-40]

    def task_asset(self, aid, task):
        a = next((a for a in self.assets if a["id"] == aid), None)
        if not a:
            return "Unknown asset"
        t = task.get("type")
        if t == "investigate" and task.get("target") not in self.tracks:
            return "That track no longer exists"
        if t == "patrol" and not any(z["id"] == task.get("target") for z in self.zones):
            return "That zone no longer exists"
        if t not in ("investigate", "patrol", "rtb", "idle"):
            return "Unknown task"
        a["task"] = {"type": t, "target": task.get("target")}
        what = {"investigate": f"investigate {task.get('target')}",
                "patrol": f"patrol {next((z['name'] for z in self.zones if z['id'] == task.get('target')), '')}",
                "rtb": f"return to {a['homeName']}", "idle": "hold position"}[t]
        self.ev("info", f"Operator tasked {a['id']} to {what}", a["id"])
        return None

    def classify(self, tid, aff):
        tr = self.tracks.get(tid)
        if not tr or aff not in ("friendly", "neutral", "unknown", "suspect"):
            return "Track or classification not recognised"
        tr["aff"], tr["affSrc"] = aff, "operator"
        self.ev("info", f"Operator set {tid} to {aff}", tid)
        return None

    def toggle_sensor(self, sid):
        s = self.sensor(sid)
        if not s:
            return "Unknown sensor"
        s["on"] = not s["on"]
        self.ev("info" if s["on"] else "warn", f"{s['id']} {s['name']} {'back online' if s['on'] else 'taken offline'}", s["id"])
        return None

    # ---------- main tick ----------
    def tick(self):
        prev = self.now
        self.now = time.time()
        dt = min(5.0, max(0.2, self.now - prev))
        if self.sim:
            for k, o in list(self.truths.items()):
                if not self._step_truth(o, dt):
                    if o["kind"] == "arrival":
                        self.ev("info", f"{o['call']} landed at Entebbe (HUEN)")
                    del self.truths[k]
        for a in self.assets:
            self._step_asset(a, dt)
        dets = self._sense() if self.sim else []
        dets += self.pending
        self.pending = []
        self._fuse(dets)
        self._zone_check()
        if self.sim and random.random() < dt / 5:
            c = lambda k: sum(1 for o in self.truths.values() if o["kind"] == k)
            if not self.live_adsb:
                if c("arrival") < 2 and random.random() < 0.25:
                    self.spawn_arrival()
                if c("departure") < 1 and random.random() < 0.12:
                    self.spawn_departure()
            if c("boat") < 6 and random.random() < 0.3:
                self.spawn_boat()
            if c("vehicle") < 5 and random.random() < 0.35:
                self.spawn_vehicle()
            if c("drone") < 2 and random.random() < 0.03:
                self.spawn_drone(False)
            if c("drone") < 2 and self.now - self.last_susp_drone > 420:
                self.spawn_drone(True)
                self.last_susp_drone = self.now
        for s in self.sensors:
            s["hits"] = [t for t in s["hits"] if self.now - t < 60]

    # ---------- output ----------
    def snapshot(self, include_raw=False):
        now = self.now
        tracks = []
        for t in self.tracks.values():
            tracks.append(dict(id=t["id"], domain=t["domain"], lat=round(t["lat"], 6), lon=round(t["lon"], 6),
                               vlat=t["vlat"], vlon=t["vlon"], alt=round(t["alt"]), call=t["call"], aff=t["aff"],
                               affSrc=t["affSrc"], label=t["label"], first=t["first"], last=t["last"], status=t["status"],
                               zones=sorted(t["zones"]), identified=t["identified"], sim=t["sim"],
                               conf=round(self.conf(t), 2), src={k: round(v, 1) for k, v in t["src"].items()},
                               hist=[[round(h[0], 5), round(h[1], 5)] for h in t["hist"][-30:]]))
        assets = [dict(id=a["id"], name=a["name"], domain=a["domain"], home=a["home"], homeName=a["homeName"], spd=a["spd"],
                       eo=a["eo"], batt=None if a["batt"] is None else round(a["batt"], 1), lat=round(a["lat"], 6),
                       lon=round(a["lon"], 6), task=a["task"], trail=[[round(p[0], 5), round(p[1], 5)] for p in a["trail"][::2]],
                       sim=a["sim"]) for a in self.assets]
        sensors = [dict(id=s["id"], name=s["name"], type=s["type"], lat=s["lat"], lon=s["lon"], range=s["range"],
                        noise=s["noise"], on=s["on"], sim=s["sim"], external=s["external"], hits=len(s["hits"]),
                        lastSeen=s["last_seen"]) for s in self.sensors]
        out = dict(now=now, tracks=tracks, assets=assets, sensors=sensors, zones=self.zones,
                   events=self.events[:120], detPerMin=len(self.det_log), liveAdsb=self.live_adsb, sim=self.sim)
        if include_raw:
            out["raw"] = [dict(lat=round(r["lat"], 5), lon=round(r["lon"], 5), type=r["type"], t=r["t"]) for r in self.raw[-400:]]
        return out
