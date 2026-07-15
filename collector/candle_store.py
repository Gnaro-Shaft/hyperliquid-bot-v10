"""
CandleStore (V10) — cache mémoire des bougies, alimenté par le WebSocket.

Pourquoi : le moteur relisait ~200 bougies × 10 coins dans Mongo toutes les
15 s (héritage v8) ≈ 3,5 GB/jour de lecture Atlas. Le 14/07, le tier M0 a
franchi son plafond de transfert et s'est fait THROTTLER : requête indexée de
150 docs passée de ~20 ms à ~377 ms → cycle 16,5 s → 21 s → évaluations
2 200/h → 1 700/h. Or le collector reçoit déjà chaque bougie en temps réel :
le moteur lit désormais la mémoire, Mongo ne sert qu'à l'amorçage.

- Thread-safe (WS écrit, moteur lit).
- Une bougie en cours se met à jour en place (clé = timestamp d'ouverture).
- Amorçage paresseux : si le store n'a pas assez d'historique pour un
  (coin, tf), le moteur y verse le résultat de SA requête Mongo de secours.
"""

import threading

# Profondeur conservée par timeframe — dimensionnée sur les besoins du moteur
# (150×15m, 20×1m, 30×1h) avec de la marge.
LIMITS = {"1m": 60, "15m": 220, "1h": 60}


class CandleStore:
    def __init__(self, coins):
        self._lock = threading.Lock()
        self._data = {}          # {(coin, tf): {timestamp: candle_doc}}
        self.coins = list(coins)
        for c in coins:
            for tf in LIMITS:
                self._data[(c, tf)] = {}

    def update(self, coin, tf, candle):
        """Insère ou met à jour une bougie (clé = timestamp d'ouverture)."""
        key = (coin, tf)
        if key not in self._data:
            return
        limit = LIMITS[tf]
        with self._lock:
            bucket = self._data[key]
            bucket[int(candle["timestamp"])] = dict(candle)
            if len(bucket) > limit:
                for ts in sorted(bucket)[:len(bucket) - limit]:
                    del bucket[ts]

    def seed_many(self, coin, tf, candles):
        """Amorce un (coin, tf) avec une liste de bougies (ordre libre)."""
        for c in candles:
            self.update(coin, tf, c)

    def get_last_n(self, coin, tf, n):
        """Les n dernières bougies, ordre chronologique ASCENDANT (liste de dicts)."""
        key = (coin, tf)
        if key not in self._data:
            return []
        with self._lock:
            bucket = self._data[key]
            return [dict(bucket[ts]) for ts in sorted(bucket)[-n:]]

    def count(self, coin, tf):
        key = (coin, tf)
        with self._lock:
            return len(self._data.get(key, ()))
