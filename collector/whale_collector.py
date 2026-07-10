"""
WhaleCollector (V10) — nouvelles sources on-chain Hyperliquid, à LOGGER
(exploitation ultérieure via backtest ; rien n'est câblé au moteur live).

Collecte :
  1. whale_positions      : positions perp des plus gros comptes (leaderboard),
                            avec prix d'entrée, levier, PRIX DE LIQUIDATION.
  2. liquidation_clusters : agrégation des liquidationPx par coin en buckets de
                            prix → carte des zones de liquidation.
  3. whale_flows          : flux nets (dépôts/retraits USDC) des comptes suivis
                            → proxy du flux net exchange.

Sources :
  - leaderboard : https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
    (endpoint NON officiel → best-effort ; WHALE_ADDRESSES en secours)
  - clearinghouseState / userNonFundingLedgerUpdates : API info officielle.

Rythme : positions toutes les WHALE_POLL_INTERVAL s (défaut 180s, ~30 adresses
= ~10 req/min, bien sous les limites), leaderboard toutes les 6 h.
"""

import time
from collections import defaultdict

import requests
from pymongo import MongoClient, ASCENDING

from config import (
    MONGO_URL, MONGO_DB, COLLECT_PAIRS,
    MONGO_COLLECTION_WHALE_POSITIONS, MONGO_COLLECTION_LIQ_CLUSTERS,
    MONGO_COLLECTION_WHALE_FLOWS, MONGO_COLLECTION_HEARTBEATS,
    WHALE_POLL_INTERVAL, WHALE_LEADERBOARD_REFRESH_SEC, WHALE_TOP_N,
    WHALE_ADDRESSES, LIQ_CLUSTER_BUCKET_PCT, LIQ_CLUSTER_RANGE_PCT,
)
from collector.heartbeat import Heartbeat

COINS = set(pair.split("/")[0] for pair in COLLECT_PAIRS)
API_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def build_liq_clusters(positions, mark_prices, bucket_pct=LIQ_CLUSTER_BUCKET_PCT,
                       range_pct=LIQ_CLUSTER_RANGE_PCT):
    """Fonction PURE : agrège les prix de liquidation en clusters par coin.

    positions   : liste de dicts {coin, liquidation_px, position_value, szi}
    mark_prices : {coin: mark_px}
    Retourne {coin: [{px, notional, n, side}]} — px = centre du bucket.
    Les liquidations hors ±range_pct du mark sont ignorées (bruit lointain).
    """
    clusters = defaultdict(lambda: defaultdict(lambda: {"notional": 0.0, "n": 0,
                                                        "long": 0, "short": 0}))
    for p in positions:
        coin = p.get("coin")
        liq = p.get("liquidation_px")
        mark = mark_prices.get(coin)
        if not coin or not liq or not mark or mark <= 0:
            continue
        if abs(liq - mark) / mark > range_pct:
            continue
        bucket_w = mark * bucket_pct
        bucket = round(liq / bucket_w) * bucket_w
        c = clusters[coin][bucket]
        c["notional"] += abs(float(p.get("position_value") or 0))
        c["n"] += 1
        if float(p.get("szi") or 0) > 0:
            c["long"] += 1
        else:
            c["short"] += 1

    out = {}
    for coin, buckets in clusters.items():
        out[coin] = sorted(
            [{"px": px, "notional": round(v["notional"], 2), "n": v["n"],
              "n_long": v["long"], "n_short": v["short"]}
             for px, v in buckets.items()],
            key=lambda x: -x["notional"],
        )
    return out


class WhaleCollector:
    def __init__(self):
        self.mongo = None
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._ensure_indexes()
                print("[WHALES] MongoDB connecte.")
            except Exception as e:
                print(f"[WHALES][ERREUR] MongoDB: {e}")
        self.heartbeat = Heartbeat(self.mongo, MONGO_COLLECTION_HEARTBEATS)
        self._running = True
        self._addresses = list(WHALE_ADDRESSES)
        self._last_leaderboard = 0
        self._last_flow_check_ms = int(time.time() * 1000)
        self._leaderboard_warned = False

    def _ensure_indexes(self):
        for col in (MONGO_COLLECTION_WHALE_POSITIONS, MONGO_COLLECTION_LIQ_CLUSTERS,
                    MONGO_COLLECTION_WHALE_FLOWS):
            try:
                self.mongo[col].create_index(
                    [("coin", ASCENDING), ("timestamp", ASCENDING)])
            except Exception as e:
                print(f"[WHALES] Index {col}: {e}")

    def stop(self):
        self._running = False

    # ────────────────────── LEADERBOARD ──────────────────────

    def _refresh_leaderboard(self):
        """Rafraîchit la liste des adresses suivies (top WHALE_TOP_N + forcées)."""
        try:
            resp = requests.get(LEADERBOARD_URL, timeout=15)
            resp.raise_for_status()
            rows = resp.json().get("leaderboardRows", [])
            # Trier par valeur de compte décroissante (champ accountValue si présent)
            def acct_val(r):
                try:
                    return float(r.get("accountValue", 0))
                except (TypeError, ValueError):
                    return 0.0
            rows.sort(key=acct_val, reverse=True)
            top = [r.get("ethAddress") for r in rows[:WHALE_TOP_N] if r.get("ethAddress")]
            merged = list(dict.fromkeys(top + WHALE_ADDRESSES))  # dédup, ordre stable
            if merged:
                self._addresses = merged
                self._leaderboard_warned = False
                print(f"[WHALES] Leaderboard rafraîchi — {len(self._addresses)} adresses suivies")
        except Exception as e:
            if not self._leaderboard_warned:
                print(f"[WHALES][WARN] Leaderboard indisponible ({e}) — "
                      f"on continue avec {len(self._addresses)} adresses connues")
                self._leaderboard_warned = True
        self._last_leaderboard = time.time()

    # ────────────────────── POSITIONS + LIQUIDATIONS ──────────────────────

    def _fetch_positions(self, address):
        """clearinghouseState d'une adresse → liste de positions parsées."""
        resp = requests.post(API_URL, json={"type": "clearinghouseState",
                                            "user": address}, timeout=10)
        resp.raise_for_status()
        state = resp.json()
        out = []
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", 0) or 0)
            if coin not in COINS or szi == 0:
                continue
            lev = pos.get("leverage") or {}
            out.append({
                "address": address,
                "coin": coin,
                "szi": szi,                                   # >0 long, <0 short
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "position_value": float(pos.get("positionValue", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                "liquidation_px": float(pos.get("liquidationPx") or 0) or None,
                "leverage": float(lev.get("value", 0) or 0),
                "margin_used": float(pos.get("marginUsed", 0) or 0),
            })
        return out

    def _mark_prices(self):
        """Mark prices courants par coin (pour le clustering)."""
        try:
            resp = requests.post(API_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            universe = data[0].get("universe", [])
            marks = {}
            for i, ctx in enumerate(data[1]):
                if i < len(universe):
                    name = universe[i].get("name", "")
                    if name in COINS:
                        marks[name] = float(ctx.get("markPx", 0) or 0)
            return marks
        except Exception as e:
            print(f"[WHALES][ERREUR] mark prices: {e}")
            return {}

    def _poll_cycle(self):
        now_ms = int(time.time() * 1000)
        all_positions = []
        for addr in self._addresses:
            try:
                all_positions.extend(self._fetch_positions(addr))
                time.sleep(0.2)   # courtoisie rate-limit
            except Exception as e:
                print(f"[WHALES][ERREUR] positions {addr[:10]}…: {e}")

        if not all_positions:
            return

        # 1. Snapshot des positions
        if self.mongo is not None:
            try:
                docs = [{**p, "timestamp": now_ms} for p in all_positions]
                self.mongo[MONGO_COLLECTION_WHALE_POSITIONS].insert_many(docs)
                self.heartbeat.beat("whale_positions", "global",
                                    meta={"n_positions": len(docs),
                                          "n_addresses": len(self._addresses)})
            except Exception as e:
                print(f"[WHALES][ERREUR][Mongo] positions: {e}")

        # 2. Clusters de liquidation par coin
        marks = self._mark_prices()
        clusters = build_liq_clusters(all_positions, marks)
        if self.mongo is not None and clusters:
            try:
                docs = [{"timestamp": now_ms, "coin": coin, "mark_px": marks.get(coin),
                         "clusters": cl} for coin, cl in clusters.items()]
                self.mongo[MONGO_COLLECTION_LIQ_CLUSTERS].insert_many(docs)
                self.heartbeat.beat("liq_clusters", "global",
                                    meta={"n_coins": len(docs)})
            except Exception as e:
                print(f"[WHALES][ERREUR][Mongo] clusters: {e}")

    # ────────────────────── FLUX NETS (dépôts/retraits) ──────────────────────

    def _poll_flows(self):
        """Dépôts/retraits USDC des adresses suivies depuis le dernier check."""
        since_ms = self._last_flow_check_ms
        now_ms = int(time.time() * 1000)
        deposits = withdrawals = 0.0
        n_dep = n_wd = 0
        for addr in self._addresses:
            try:
                resp = requests.post(API_URL, json={
                    "type": "userNonFundingLedgerUpdates",
                    "user": addr, "startTime": since_ms}, timeout=10)
                resp.raise_for_status()
                for upd in resp.json():
                    delta = upd.get("delta", {})
                    dtype = delta.get("type", "")
                    usdc = abs(float(delta.get("usdc", 0) or 0))
                    if dtype == "deposit":
                        deposits += usdc; n_dep += 1
                    elif dtype == "withdraw":
                        withdrawals += usdc; n_wd += 1
                time.sleep(0.2)
            except Exception as e:
                print(f"[WHALES][ERREUR] flows {addr[:10]}…: {e}")
        self._last_flow_check_ms = now_ms

        if (n_dep + n_wd) == 0 or self.mongo is None:
            return
        try:
            self.mongo[MONGO_COLLECTION_WHALE_FLOWS].insert_one({
                "timestamp": now_ms,
                "coin": "ALL",                     # flux compte, pas par coin
                "window_start": since_ms,
                "deposits_usdc": round(deposits, 2),
                "withdrawals_usdc": round(withdrawals, 2),
                "net_flow_usdc": round(deposits - withdrawals, 2),
                "n_deposits": n_dep,
                "n_withdrawals": n_wd,
                "n_addresses": len(self._addresses),
            })
            self.heartbeat.beat("whale_flows", "global")
        except Exception as e:
            print(f"[WHALES][ERREUR][Mongo] flows: {e}")

    # ────────────────────── BOUCLE ──────────────────────

    def collect_loop(self):
        print(f"[WHALES] Demarrage (interval={WHALE_POLL_INTERVAL}s, "
              f"top {WHALE_TOP_N} + {len(WHALE_ADDRESSES)} adresses forcées)")
        while self._running:
            try:
                if time.time() - self._last_leaderboard > WHALE_LEADERBOARD_REFRESH_SEC \
                        or not self._addresses:
                    self._refresh_leaderboard()
                if self._addresses:
                    self._poll_cycle()
                    self._poll_flows()
                else:
                    print("[WHALES] Aucune adresse à suivre — nouveau essai au prochain cycle")
            except Exception as e:
                print(f"[WHALES][ERREUR] cycle: {e}")
            time.sleep(WHALE_POLL_INTERVAL)


if __name__ == "__main__":
    collector = WhaleCollector()
    try:
        collector.collect_loop()
    except KeyboardInterrupt:
        collector.stop()
        print("[WHALES] Arret.")
