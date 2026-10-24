# UNG-MESH

Common operating picture: combines radar, ADS-B, AIS, camera and field-asset reports into one set of tracks,
with restricted zones, alerts and asset tasking.

- Live aircraft: public ADS-B networks (adsb.lol → airplanes.live → OpenSky), tagged LIVE on the map.
- Boats, drones, vehicles, local radars and cameras: simulated (tagged SIM) until real sensors are connected. Turn off with `SIM=off`.
- Your own sensors: POST to `/api/ingest` (any detections) or `/api/ingest/aircraft-json` (readsb/dump1090 file) with header `X-API-Key: <INGEST_KEY>`.
  See `feeder/pi_adsb_feeder.py` for a Raspberry Pi + RTL-SDR feeder.

Settings (environment variables): `OPERATOR_PASSWORD`, `INGEST_KEY`, `SESSION_SECRET`, `ADSB_SOURCE`, `SIM`, `DATA_DIR`.

Run locally: `pip install -r requirements.txt && OPERATOR_PASSWORD=test uvicorn app.main:app --reload`
