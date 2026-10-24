"""Send aircraft from your own Raspberry Pi + RTL-SDR receiver to UNG-MESH.

1. On the Pi, install readsb (it decodes aircraft signals from the RTL-SDR):
     sudo bash -c "$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"
2. Copy this file to the Pi and edit the four settings below.
3. Run it:  python3 pi_adsb_feeder.py
   (No extra packages needed.)
"""
import json
import time
import urllib.request

SERVER = "https://ung-mesh-production.up.railway.app"
INGEST_KEY = "paste-your-INGEST_KEY-here"
SENSOR_ID = "PI1"
LAT, LON = 0.3476, 32.5825  # where the antenna is

AIRCRAFT_FILE = "/run/readsb/aircraft.json"
url = f"{SERVER}/api/ingest/aircraft-json?sensor_id={SENSOR_ID}&lat={LAT}&lon={LON}&name=Local%20ADS-B%20receiver"

while True:
    try:
        with open(AIRCRAFT_FILE, "rb") as f:
            body = f.read()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-API-Key": INGEST_KEY})
        with urllib.request.urlopen(req, timeout=5) as r:
            print(time.strftime("%H:%M:%S"), json.loads(r.read()))
    except Exception as e:  # keep going if the network drops
        print("send failed:", e)
    time.sleep(2)
