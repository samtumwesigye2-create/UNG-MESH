"""SQLite storage: event log and restricted zones survive restarts."""
import json
import zlib
import os
import sqlite3
import threading


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, sev TEXT, msg TEXT, ref TEXT, ack INTEGER);
            CREATE TABLE IF NOT EXISTS zones(id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS snaps(t REAL PRIMARY KEY, data BLOB);
            """)
            self.db.commit()

    def add_event(self, t, sev, msg, ref):
        ack = 0 if sev == "crit" else 1
        with self.lock:
            cur = self.db.execute("INSERT INTO events(t,sev,msg,ref,ack) VALUES(?,?,?,?,?)", (t, sev, msg, ref, ack))
            self.db.commit()
        return dict(id=cur.lastrowid, t=t, sev=sev, msg=msg, ref=ref, ack=bool(ack))

    def recent_events(self, n):
        with self.lock:
            rows = self.db.execute("SELECT id,t,sev,msg,ref,ack FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(id=r[0], t=r[1], sev=r[2], msg=r[3], ref=r[4], ack=bool(r[5])) for r in rows]

    def search_events(self, limit=500, before_id=None):
        q, args = "SELECT id,t,sev,msg,ref,ack FROM events", []
        if before_id:
            q += " WHERE id<?"
            args.append(before_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [dict(id=r[0], t=r[1], sev=r[2], msg=r[3], ref=r[4], ack=bool(r[5])) for r in rows]

    def ack_all(self):
        with self.lock:
            self.db.execute("UPDATE events SET ack=1 WHERE ack=0")
            self.db.commit()

    def load_zones(self):
        with self.lock:
            rows = self.db.execute("SELECT data FROM zones ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_zone(self, z):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO zones(id,data) VALUES(?,?)", (z["id"], json.dumps(z)))
            self.db.commit()

    def delete_zone(self, zid):
        with self.lock:
            self.db.execute("DELETE FROM zones WHERE id=?", (zid,))
            self.db.commit()

    def next_track_number(self):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k='track_base'").fetchone()
            base = int(r[0]) if r else 1
            # leave room so numbers don't repeat after a restart
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('track_base',?)", (str(base + 5000),))
            self.db.commit()
        return base

    # ---------- replay history ----------
    def add_snapshot(self, t, obj):
        blob = zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO snaps(t,data) VALUES(?,?)", (t, blob))
            self.db.commit()

    def get_snapshot(self, t):
        with self.lock:
            r = self.db.execute("SELECT t,data FROM snaps WHERE t<=? ORDER BY t DESC LIMIT 1", (t,)).fetchone()
            if not r:
                r = self.db.execute("SELECT t,data FROM snaps ORDER BY t ASC LIMIT 1").fetchone()
        if not r:
            return None
        return dict(json.loads(zlib.decompress(r[1])), t=r[0])

    def snapshot_range(self):
        with self.lock:
            r = self.db.execute("SELECT MIN(t),MAX(t),COUNT(*) FROM snaps").fetchone()
        return {"start": r[0], "end": r[1], "count": r[2]}

    def prune_snapshots(self, before):
        with self.lock:
            self.db.execute("DELETE FROM snaps WHERE t<?", (before,))
            self.db.commit()
