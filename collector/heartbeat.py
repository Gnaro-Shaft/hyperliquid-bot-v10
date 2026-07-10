"""
Heartbeats de collecte (V10) — chaque composant de collecte signale sa dernière
écriture dans la collection `collector_health`. Le HealthMonitor (et le watchdog
externe) lisent ces docs pour détecter un « collector muet > 5 min ».

Un doc par flux : _id = "<component>:<coin>" (ou "<component>:global").
Écriture rate-limitée (au plus une fois toutes les MIN_WRITE_INTERVAL_S par clé)
pour ne pas marteler Mongo depuis les handlers WebSocket.
"""

import time
import threading


class Heartbeat:
    MIN_WRITE_INTERVAL_S = 10

    def __init__(self, db, collection_name):
        self.db = db
        self.col_name = collection_name
        self._last_write = {}
        self._lock = threading.Lock()

    def beat(self, component, coin="global", meta=None):
        """Signale que <component> vient d'écrire une donnée pour <coin>."""
        if self.db is None:
            return
        key = f"{component}:{coin}"
        now = time.time()
        with self._lock:
            if now - self._last_write.get(key, 0) < self.MIN_WRITE_INTERVAL_S:
                return
            self._last_write[key] = now
        try:
            doc = {
                "_id": key,
                "component": component,
                "coin": coin,
                "last_write_ms": int(now * 1000),
            }
            if meta:
                doc["meta"] = meta
            self.db[self.col_name].replace_one({"_id": key}, doc, upsert=True)
        except Exception as e:
            print(f"[HEARTBEAT] {key}: {e}")


def read_heartbeats(db, collection_name):
    """Retourne la liste des heartbeats {_id, component, coin, last_write_ms}."""
    try:
        return list(db[collection_name].find({}))
    except Exception:
        return []


def stale_heartbeats(heartbeats, now_ms, max_age_s):
    """Fonction PURE : retourne les flux muets depuis plus de max_age_s.

    heartbeats : liste de docs {_id, component, coin, last_write_ms}
    Retourne une liste de {key, component, coin, age_s}.
    """
    stale = []
    for hb in heartbeats:
        last = hb.get("last_write_ms")
        if last is None:
            continue
        age_s = (now_ms - int(last)) / 1000.0
        if age_s > max_age_s:
            stale.append({
                "key": hb.get("_id", "?"),
                "component": hb.get("component", "?"),
                "coin": hb.get("coin", "?"),
                "age_s": round(age_s, 1),
            })
    return stale
