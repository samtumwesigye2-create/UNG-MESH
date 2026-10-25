"""Send boats from your own Raspberry Pi + RTL-SDR (AIS-catcher) to UNG-MESH.

What you need: a Raspberry Pi, an RTL-SDR dongle, and a marine VHF antenna
(about 162 MHz) placed as high as you can near the lake shore.

1. Install AIS-catcher on the Pi (see https://docs.aiscatcher.org for the
   one-line installer), then start it so it sends decoded boats to this script:
       AIS-catcher -d:0 -u 127.0.0.1 10110 JSON on
2. Copy this file to the Pi and edit the settings below.
3. Run it in a second window:  python3 pi_ais_feeder.py
   (No extra packages needed.)
"""
import json
import socket
import time
import urllib.request

SERVER = "https://ung-mesh-production.up.railway.app"
INGEST_KEY = "paste-your-INGEST_KEY-here"
SENSOR_ID = "PI-AIS1"
SENSOR_NAME = "AIS receiver Port Bell"
LAT, LON = 0.288, 32.652  # where the antenna is
RANGE_KM = 40

LISTEN_PORT = 10110   # must match the port in the AIS-catcher command
SEND_EVERY = 5        # seconds between uploads
FORGET_AFTER = 180    # drop a boat if not heard for this many seconds

boats = {}  # mmsi -> latest info


def handle(line):
    line = line.strip()
    if not line:
        return
    if line.startswith("!"):
        print("Got raw NMEA, not JSON. Add 'JSON on' to the AIS-catcher -u option.")
        return
    try:
        m = json.loads(line)
    except ValueError:
        return
    mmsi = m.get("mmsi")
    if not mmsi:
        return
    b = boats.setdefault(mmsi, {"mmsi": mmsi})
    if m.get("shipname"):
        b["name"] = str(m["shipname"]).strip()
    lat, lon = m.get("lat"), m.get("lon")
    if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
        b.update(lat=lat, lon=lon, seen=time.time())
        sp, co = m.get("speed"), m.get("course")
        b["speed"] = sp if sp is not None and sp < 102.3 else None
        b["course"] = co if co is not None and co < 360 else None


def upload():
    now = time.time()
    for k in [k for k, b in boats.items() if now - b.get("seen", 0) > FORGET_AFTER and "lat" in b]:
        del boats[k]
    dets = []
    for b in boats.values():
        if "lat" not in b:
            continue
        both = b["speed"] is not None and b["course"] is not None
        dets.append(dict(lat=b["lat"], lon=b["lon"], domain="surface", key=f"MMSI {b['mmsi']}",
                         call=b.get("name") or f"MMSI {b['mmsi']}", label="Vessel (local AIS)",
                         speed_kt=b["speed"] if both else None, course_deg=b["course"] if both else None, interval=10))
    body = json.dumps({"sensor": {"id": SENSOR_ID, "name": SENSOR_NAME, "type": "ais", "lat": LAT, "lon": LON,
                                  "range": RANGE_KM}, "detections": dets}).encode()
    req = urllib.request.Request(SERVER + "/api/ingest", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": INGEST_KEY})
    with urllib.request.urlopen(req, timeout=8) as r:
        print(time.strftime("%H:%M:%S"), f"{len(dets)} boats sent", json.loads(r.read()))


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    sock.settimeout(1.0)
    print(f"Listening for AIS-catcher on UDP port {LISTEN_PORT}...")
    last = 0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            for line in data.decode(errors="ignore").splitlines():
                handle(line)
        except socket.timeout:
            pass
        if time.time() - last >= SEND_EVERY:
            last = time.time()
            try:
                upload()
            except Exception as e:  # keep going if the network drops
                print("send failed:", e)


if __name__ == "__main__":
    main()
