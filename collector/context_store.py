"""
MarketContextStore (V10) — magasin de contexte marché avec carry-forward.

C'est LA correction du bloqueur n°1 de v8 (sentiment rempli à ~38%, spread 2%) :
la fréquence d'écriture du REST/orderbook est découplée de l'écriture du signal.
Les collectors POUSSENT leurs valeurs ici dès qu'elles arrivent ; la stratégie
LIT la dernière valeur connue (jamais un vide) accompagnée de son âge en ms,
pour que la fraîcheur reste analysable dans le dataset.

- Thread-safe (les collectors écrivent depuis leurs threads, le bot lit).
- Amorcé depuis MongoDB au démarrage (dernières valeurs connues par coin),
  pour ne pas repartir de zéro après un restart.
- Conserve de petits historiques en mémoire pour reproduire EXACTEMENT les
  fenêtres que le moteur v8 calculait via Mongo :
    funding  : 6 derniers polls  (≈30 min) → funding_slope
    OI       : 6 derniers polls  (≈30 min) → oi_trend_30m
    orderbook: 10 derniers snaps (≈5 min)  → ob_imbalance_avg, ob_depth_ratio
"""

import threading
import time
from collections import deque


def _now_ms():
    return int(time.time() * 1000)


class MarketContextStore:
    FUNDING_WINDOW = 6      # ≈30 min à 300s/poll
    OI_WINDOW = 6           # ≈30 min
    OB_WINDOW = 10          # ≈5 min à 30s/snapshot

    def __init__(self, coins):
        self._lock = threading.Lock()
        self._funding = {c: deque(maxlen=self.FUNDING_WINDOW) for c in coins}
        self._oi = {c: deque(maxlen=self.OI_WINDOW) for c in coins}
        self._ob = {c: deque(maxlen=self.OB_WINDOW) for c in coins}
        self.coins = list(coins)

    # ────────────────────── ÉCRITURE (collectors) ──────────────────────

    def update_funding(self, coin, funding_rate, ts_ms=None, premium=None, mark_price=None):
        if coin not in self._funding:
            return
        with self._lock:
            self._funding[coin].append({
                "ts": ts_ms or _now_ms(),
                "funding_rate": float(funding_rate),
                "premium": premium,
                "mark_price": mark_price,
            })

    def update_oi(self, coin, open_interest, oi_change_pct, ts_ms=None, mark_price=None):
        if coin not in self._oi:
            return
        with self._lock:
            self._oi[coin].append({
                "ts": ts_ms or _now_ms(),
                "open_interest": float(open_interest),
                "oi_change_pct": float(oi_change_pct),
                "mark_price": mark_price,
            })

    def update_orderbook(self, coin, imbalance, spread_pct, bid_depth_5, ask_depth_5, ts_ms=None):
        if coin not in self._ob:
            return
        with self._lock:
            self._ob[coin].append({
                "ts": ts_ms or _now_ms(),
                "imbalance": float(imbalance),
                "spread_pct": float(spread_pct),
                "bid_depth_5": float(bid_depth_5),
                "ask_depth_5": float(ask_depth_5),
            })

    # ────────────────────── LECTURE (stratégie / logger) ──────────────────────

    def get_context(self, coin, now_ms=None):
        """Contexte marché avec carry-forward : mêmes clés que v8
        `StrategyEngine.get_market_context` + valeurs brutes + âges en ms.

        Les valeurs ne sont JAMAIS remises à None une fois connues : on renvoie
        la dernière valeur + son âge (`*_age_ms`). Un champ n'est None que si
        aucune valeur n'a encore été observée depuis le démarrage + l'amorçage.
        """
        now = now_ms or _now_ms()
        ctx = {
            "funding_rate": None, "funding_slope": None, "funding_age_ms": None,
            "open_interest": None, "oi_change_pct": None, "oi_trend_30m": None,
            "oi_age_ms": None,
            "ob_imbalance": None, "ob_imbalance_avg": None,
            "spread_pct": None, "ob_depth_ratio": None,
            "bid_depth_5": None, "ask_depth_5": None, "ob_age_ms": None,
        }
        with self._lock:
            fnd = list(self._funding.get(coin, ()))
            ois = list(self._oi.get(coin, ()))
            obs = list(self._ob.get(coin, ()))

        if fnd:
            last = fnd[-1]
            ctx["funding_rate"] = last["funding_rate"]
            ctx["funding_age_ms"] = max(0, now - last["ts"])
            if len(fnd) >= 2:
                ctx["funding_slope"] = fnd[-1]["funding_rate"] - fnd[0]["funding_rate"]

        if ois:
            last = ois[-1]
            ctx["open_interest"] = last["open_interest"]
            ctx["oi_change_pct"] = last["oi_change_pct"]
            ctx["oi_age_ms"] = max(0, now - last["ts"])
            if len(ois) >= 2 and ois[0]["open_interest"] > 0:
                ctx["oi_trend_30m"] = (
                    (ois[-1]["open_interest"] - ois[0]["open_interest"])
                    / ois[0]["open_interest"]
                )

        if obs:
            last = obs[-1]
            ctx["ob_imbalance"] = last["imbalance"]
            ctx["spread_pct"] = last["spread_pct"]
            ctx["bid_depth_5"] = last["bid_depth_5"]
            ctx["ask_depth_5"] = last["ask_depth_5"]
            ctx["ob_age_ms"] = max(0, now - last["ts"])
            if len(obs) >= 3:
                vals = [o["imbalance"] for o in obs]
                ctx["ob_imbalance_avg"] = round(sum(vals) / len(vals), 4)
                depths = [(o["bid_depth_5"] + o["ask_depth_5"]) for o in obs]
                depths = [d for d in depths if d > 0]
                if len(depths) >= 3 and (avg := sum(depths) / len(depths)) > 0:
                    # dernier snapshot vs moyenne récente (même formule que v8)
                    last_depth = obs[-1]["bid_depth_5"] + obs[-1]["ask_depth_5"]
                    ctx["ob_depth_ratio"] = round(last_depth / avg, 3)

        return ctx

    # ────────────────────── AMORÇAGE (Mongo → mémoire) ──────────────────────

    def seed_from_mongo(self, db, funding_col, oi_col, ob_col):
        """Amorce les historiques depuis MongoDB au démarrage (carry-forward
        inter-redémarrages). Best-effort : toute erreur laisse le store vide."""
        for coin in self.coins:
            try:
                docs = list(db[funding_col].find(
                    {"coin": coin}, sort=[("timestamp", -1)]).limit(self.FUNDING_WINDOW))
                for d in reversed(docs):
                    self.update_funding(
                        coin, float(d.get("funding_rate", 0)), ts_ms=int(d["timestamp"]),
                        premium=d.get("premium"), mark_price=d.get("mark_price"))
            except Exception as e:
                print(f"[CTX_STORE] seed funding {coin}: {e}")
            try:
                docs = list(db[oi_col].find(
                    {"coin": coin}, sort=[("timestamp", -1)]).limit(self.OI_WINDOW))
                for d in reversed(docs):
                    self.update_oi(
                        coin, float(d.get("open_interest", 0)),
                        float(d.get("oi_change_pct", 0)), ts_ms=int(d["timestamp"]),
                        mark_price=d.get("mark_price"))
            except Exception as e:
                print(f"[CTX_STORE] seed OI {coin}: {e}")
            try:
                docs = list(db[ob_col].find(
                    {"coin": coin}, sort=[("timestamp", -1)]).limit(self.OB_WINDOW))
                for d in reversed(docs):
                    self.update_orderbook(
                        coin, float(d.get("imbalance", 0)), float(d.get("spread_pct", 0)),
                        float(d.get("bid_depth_5", 0)), float(d.get("ask_depth_5", 0)),
                        ts_ms=int(d["timestamp"]))
            except Exception as e:
                print(f"[CTX_STORE] seed orderbook {coin}: {e}")
