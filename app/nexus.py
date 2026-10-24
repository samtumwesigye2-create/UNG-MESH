"""Link between UNG-MESH and the UNG-NEXUS hub.

Out: critical alerts and operator actions are reported to NEXUS (/v1/inbound) as
     standard NEXUS envelopes, so every UNG system can see them in one audit trail.
In:  NEXUS (or any UNG system routing through it) can POST envelopes to
     /v1/nexus/inbound here. Supported message types:
       system.health         -> replies accepted
       mesh.detections       -> payload {"sensor": {...}, "detections": [...]} (same as /api/ingest)
       mesh.zone.create      -> payload {"name","lat","lon","r","dom"}
       mesh.tracks.query     -> replies with the current tracks (summary)

Settings: NEXUS_BASE_URL, NEXUS_TOKEN (the UNG-MESH service token NEXUS accepts),
          JANUS_BASE_URL (to check tokens from other UNG systems).
"""
import asyncio
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

log = logging.getLogger("nexus")

NEXUS_BASE_URL = os.environ.get("NEXUS_BASE_URL", "").rstrip("/")
NEXUS_TOKEN = os.environ.get("NEXUS_TOKEN", "").strip()
JANUS_BASE_URL = os.environ.get("JANUS_BASE_URL", "https://ung-iam-production.up.railway.app").rstrip("/")
SYSTEM_ID = "UNG-MESH"


def envelope(message_type, payload, target="UNG-NEXUS", priority=50, classification="internal", correlation_id=None):
    return dict(source_system=SYSTEM_ID, target_system=target, message_type=message_type, payload=payload,
                message_id=str(uuid.uuid4()), correlation_id=correlation_id, trace_id=None, schema_version="1.0",
                priority=priority, classification=classification, sent_at=datetime.now(timezone.utc).isoformat())


def check_token(authorization):
    """Accept the shared UNG-MESH service token, or a JANUS token with service/admin rights."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if NEXUS_TOKEN and hmac.compare_digest(token, NEXUS_TOKEN):
        return {"id": "nexus-service-token"}
    req = urllib.request.Request(JANUS_BASE_URL + "/v1/auth/introspect", data=b"", method="POST",
                                 headers={"Authorization": authorization, "User-Agent": "UNG-MESH/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None
    p = data.get("principal") or {}
    perms = set(p.get("permissions") or [])
    if perms & {"platform:service", "ung.admin", "mesh.write"}:
        return p
    return None


class NexusLink:
    def __init__(self, engine):
        self.engine = engine
        self.queue = asyncio.Queue(maxsize=500)
        self.sent = 0
        self.failed = 0
        self.received = 0
        self.last_ok = None
        self.last_error = None
        self._seen_event = max((e["id"] for e in engine.events), default=0)

    @property
    def configured(self):
        return bool(NEXUS_BASE_URL and NEXUS_TOKEN)

    def status(self):
        return dict(configured=self.configured, sent=self.sent, failed=self.failed, received=self.received,
                    lastOk=self.last_ok, lastError=self.last_error)

    def report(self, message_type, payload, priority=50, correlation_id=None):
        if not self.configured:
            return
        try:
            self.queue.put_nowait(envelope(message_type, payload, priority=priority, correlation_id=correlation_id))
        except asyncio.QueueFull:
            self.failed += 1

    def collect_events(self):
        """Forward new critical alerts and operator actions to NEXUS."""
        new = [e for e in self.engine.events if e["id"] > self._seen_event]
        if not new:
            return
        self._seen_event = max(e["id"] for e in new)
        for e in reversed(new):
            if e["sev"] == "crit":
                tr = self.engine.tracks.get(e["ref"]) if e["ref"] else None
                self.report("mesh.alert", dict(severity="critical", message=e["msg"], ref=e["ref"], at=e["t"],
                                               track=None if not tr else dict(id=tr["id"], domain=tr["domain"],
                                                                             lat=round(tr["lat"], 6), lon=round(tr["lon"], 6),
                                                                             affiliation=tr["aff"], label=tr["label"],
                                                                             simulated=tr["sim"])),
                            priority=90, correlation_id=e["ref"])
            elif e["msg"].startswith("Operator "):
                self.report("mesh.operator_action", dict(message=e["msg"], ref=e["ref"], at=e["t"]), priority=40)

    def _post(self, env):
        req = urllib.request.Request(NEXUS_BASE_URL + "/v1/inbound", data=json.dumps(env).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + NEXUS_TOKEN,
                                              "User-Agent": "UNG-MESH/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status

    async def run(self):
        if not self.configured:
            log.info("NEXUS link off (NEXUS_BASE_URL / NEXUS_TOKEN not set)")
            return
        self.report("system.health", {"status": "online", "service": SYSTEM_ID})
        while True:
            env = await self.queue.get()
            ok = False
            for attempt in range(3):
                try:
                    code = await asyncio.to_thread(self._post, env)
                    ok = True
                    self.sent += 1
                    self.last_ok = datetime.now(timezone.utc).timestamp()
                    if env["message_type"] == "system.health":
                        log.info("NEXUS_LINK status=ok code=%s", code)
                    break
                except urllib.error.HTTPError as e:
                    self.last_error = f"http_{e.code}"
                    if 400 <= e.code < 500 and e.code not in (408, 429):
                        break
                except Exception as e:  # noqa: BLE001
                    self.last_error = type(e).__name__
                await asyncio.sleep(1 + attempt * 2)
            if not ok:
                self.failed += 1
                log.warning("NEXUS_LINK delivery failed (%s): %s", env["message_type"], self.last_error)

    def handle_inbound(self, env):
        """Process one envelope from NEXUS. Returns the response body."""
        self.received += 1
        mtype = env.get("message_type", "")
        payload = env.get("payload") or {}
        mid = env.get("message_id") or str(uuid.uuid4())
        src = env.get("source_system", "unknown")
        if mtype == "system.health":
            return {"accepted": True, "message_id": mid, "service": SYSTEM_ID}
        if mtype == "mesh.detections":
            s = payload.get("sensor") or {}
            if not s.get("id"):
                return {"accepted": False, "message_id": mid, "error": "payload.sensor.id is required"}
            s = dict(s, id=f"{src}:{s['id']}" if ":" not in s["id"] else s["id"])
            self.engine.register_sensor(s)
            n = self.engine.push(s["id"], payload.get("detections") or [])
            return {"accepted": True, "message_id": mid, "detections_accepted": n}
        if mtype == "mesh.zone.create":
            try:
                z = self.engine.add_zone(str(payload["name"])[:60], float(payload["lat"]), float(payload["lon"]),
                                         max(0.1, min(50.0, float(payload["r"]))),
                                         [d for d in payload.get("dom", ["air", "surface", "ground"])
                                          if d in ("air", "surface", "ground")] or ["air"])
            except (KeyError, TypeError, ValueError):
                return {"accepted": False, "message_id": mid, "error": "zone needs name, lat, lon, r"}
            return {"accepted": True, "message_id": mid, "zone": z}
        if mtype == "mesh.tracks.query":
            tracks = [dict(id=t["id"], domain=t["domain"], lat=round(t["lat"], 6), lon=round(t["lon"], 6), call=t["call"],
                           affiliation=t["aff"], label=t["label"], status=t["status"], simulated=t["sim"])
                      for t in self.engine.tracks.values() if t["status"] != "lost"]
            return {"accepted": True, "message_id": mid, "tracks": tracks}
        return {"accepted": False, "message_id": mid, "error": f"unsupported message_type {mtype}"}
