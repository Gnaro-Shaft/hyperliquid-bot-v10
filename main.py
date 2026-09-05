"""
Trading Bot V10 — moteur de décision v8 conservé, plomberie refondue.

Câblage V10 :
  - MarketContextStore partagé : collectors → store → moteur (carry-forward) ;
  - SignalLogger : une ligne complète par évaluation, pour les 10 coins ;
  - lien trade ↔ signal : chaque position porte le signal_id et le snapshot
    des features de son signal d'entrée, propagés dans le journal des trades ;
  - WhaleCollector (positions baleines, clusters de liquidation, flux nets) ;
  - HealthMonitor V10 (alerte « collecteur muet > 5 min » par flux).
"""

import time
import signal
import threading
import asyncio
from datetime import datetime, timezone


from config import (
    PAIRS, COINS, DEBUG, PAPER_MODE,
    KILL_SWITCH_FILE,
    LOOP_INTERVAL, TRAILING_CHECK_INTERVAL,
    MIN_COLLATERAL, RESERVE_BALANCE_PCT,
    SIGNAL_THRESHOLD_DEFAULT,
    MONGO_URL,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_ORDERBOOK,
    COOLDOWN_BASE_SEC,
    WHALE_ENABLED,
)
from strategy.strategy_engine import StrategyEngine
from trader.ccxt_trader import HyperliquidTrader
from trader.paper_trader import PaperTrader
from collector.context_store import MarketContextStore
from collector.candle_store import CandleStore
from collector.websocket_collector import WebSocketCollector
from collector.rest_collector import RestCollector
from collector.whale_collector import WhaleCollector
from datalog.signal_logger import SignalLogger
from risk.risk_manager import RiskManager
from risk import guards
from strategy.entry import EntryManager
from trader.position_manager import PositionManager, empty_position
from datalog import reporting
from utils import mongo
from utils.notifier import Notifier
from monitor.health import HealthMonitor



class TradingBot:
    def __init__(self):
        # ── Plomberie V10 : stores partagés + logger de signaux ──
        self.context_store = MarketContextStore(COINS)
        self.candle_store = CandleStore(COINS)   # cache bougies (anti-throttle M0)
        self.signal_logger = SignalLogger()

        self.collector = WebSocketCollector(context_store=self.context_store,
                                            candle_store=self.candle_store)
        self.rest_collector = RestCollector(context_store=self.context_store)
        self.whale_collector = WhaleCollector() if WHALE_ENABLED else None
        self.trader = PaperTrader() if PAPER_MODE else HyperliquidTrader()
        self.risk = RiskManager()
        self.notifier = Notifier()
        self._shutdown = False
        self._last_daily_reset = None

        # --- État multi-paires (un dict par coin) ---
        self.positions = {coin: empty_position() for coin in COINS}
        self.engines = {}                          # initialisés dans start()
        self._last_trade_times = {c: 0 for c in COINS}

        # --- Auto-calibration & corrélation ---
        self._last_signal_scores = {c: 0 for c in COINS}
        self._signal_threshold = SIGNAL_THRESHOLD_DEFAULT
        self._last_autocal_date = None

        # --- Cooldown dynamique ---
        self._cooldowns = {c: COOLDOWN_BASE_SEC for c in COINS}

        # --- Compteur d'erreurs ---
        self._err_count = 0

        # --- Gestion des positions ouvertes ---
        # Reçoit le dict `positions` lui-même : les entrées sont modifiées en
        # place des deux côtés, jamais réaffectées en bloc.
        self.pos_mgr = PositionManager(
            self.trader, self.risk, self.notifier,
            self.positions, self._cooldowns, self._last_trade_times)

        # --- Décision d'entrée ---
        # Possède ses séries de confirmation ; partage les dicts que la
        # fermeture de position touche aussi.
        self.entry = EntryManager(
            self.trader, self.risk, self.notifier, self.positions,
            self._cooldowns, self._last_trade_times, self._last_signal_scores, COINS)

    # ──────────────────────────────────────────────────────────
    # Démarrage
    # ──────────────────────────────────────────────────────────

    def start(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        # Health-check MongoDB — dépendance dure : toute la pipeline live passe
        # par Mongo (collector → MongoDB → stratégie). Sans elle, aucun signal.
        if not self._check_mongo_health():
            try:
                self.notifier.error(
                    "🔴 <b>DÉMARRAGE AVORTÉ</b>\n"
                    "MongoDB injoignable — le bot ne peut pas produire de signaux.\n"
                    "Nouvelle tentative au prochain redémarrage."
                )
            except Exception:
                pass
            print("[BOT] ❌ Arrêt : MongoDB indisponible au démarrage.")
            time.sleep(30)   # throttle la boucle de restart Fly (policy=always)
            import sys
            sys.exit(1)

        # Amorcer le carry-forward depuis Mongo (dernières valeurs connues)
        try:
            db = mongo.get_db()
            self.context_store.seed_from_mongo(
                db, MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI,
                MONGO_COLLECTION_ORDERBOOK)
            print("[BOT] 🔁 ContextStore amorcé depuis MongoDB (carry-forward)")
        except Exception as e:
            print(f"[BOT] ContextStore non amorcé ({e}) — il se remplira en live")

        # Collectors en threads dédiés
        threading.Thread(target=self._run_collector, daemon=True).start()
        threading.Thread(target=self.rest_collector.collect_loop, daemon=True).start()
        if self.whale_collector is not None:
            threading.Thread(target=self.whale_collector.collect_loop,
                             daemon=True, name="WhaleCollector").start()
            print("[BOT] 🐋 WhaleCollector démarré (positions + liquidations + flux)")

        # Attendre les premières données WS
        print("[BOT] Attente des premières données du collector...")
        for _ in range(60):
            if self.collector.is_alive:
                break
            time.sleep(2)
        if not self.collector.is_alive:
            print("[BOT] Collector muet après 2 min — démarrage quand même...")

        # Init engines pour chaque coin — store + logger partagés (V10)
        for coin in COINS:
            self.engines[coin] = StrategyEngine(
                coin=coin,
                context_store=self.context_store,
                signal_logger=self.signal_logger,
                candle_store=self.candle_store,
            )

        # Healthcheck autonome — surveillance + alertes Telegram sur transition
        self._health_monitor = HealthMonitor(bot=self, notifier=self.notifier)
        threading.Thread(
            target=self._health_monitor.run_loop,
            daemon=True,
            name="HealthMonitor",
        ).start()
        print("[BOT] 🚑 HealthMonitor démarré (collecteur muet > 5 min ⇒ alerte)")

        # Init risk manager
        balance = self.trader._get_total_balance()
        self.risk.reset_daily(balance)
        self._last_daily_reset = datetime.now(timezone.utc).date()

        # Sync positions existantes sur l'exchange
        self.pos_mgr.sync_on_start()

        pairs_str = " | ".join(COINS)
        mode = "📝 PAPER" if PAPER_MODE else "💸 LIVE"
        self.notifier.send(f"🚀 <b>Bot V10 démarré [{mode}]</b> — {pairs_str} | Solde: <code>{balance:.2f} USDC</code>")
        print(f"\n=== Trading Bot V10 [{mode}] | {pairs_str} | Solde: {balance:.2f} USDC ===\n")

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        self._trading_loop()

    def _trading_loop(self):
        while not self._shutdown:
            try:
                # Reset journalier + rapport + auto-calibration
                today = datetime.now(timezone.utc).date()
                if today != self._last_daily_reset:
                    if self._last_daily_reset is not None:
                        reporting.send_daily_report(
                            self.notifier, self.trader, self.positions)
                    balance = self.trader._get_total_balance()
                    self.risk.reset_daily(balance)
                    self._last_daily_reset = today
                    if self._last_autocal_date is None or \
                       (today - self._last_autocal_date).days >= 7:
                        self._signal_threshold = reporting.auto_calibrate(
                            self._signal_threshold, self.notifier)
                        self._last_autocal_date = today
                    self.notifier.send(f"📅 <b>Nouveau jour</b> — Solde: <code>{balance:.2f} USDC</code>")

                if self._check_kill_switch():
                    time.sleep(60)
                    continue

                if not self.collector.is_alive:
                    print("[BOT] ⚠️ Collector inactif — données potentiellement stales")

                # Traiter chaque paire — isolation par coin (une erreur ne bloque pas les autres)
                for pair in PAIRS:
                    coin = pair.split("/")[0]
                    try:
                        self._process_pair(pair, coin)
                    except Exception as e:
                        import traceback
                        print(f"[BOT][ERREUR][{coin}] {e}")
                        print(traceback.format_exc())

            except Exception as e:
                import traceback
                print(f"[BOT][ERREUR] {e}")
                print(traceback.format_exc())
                self._err_count += 1
                if self._err_count <= 3 or self._err_count % 10 == 0:
                    self.notifier.error(f"[{self._err_count}] {str(e)[:200]}")

            # Boucle courte : positions actives OU pending entries
            any_active = any(self.positions[c]["active"] for c in COINS)
            any_pending = any(self.positions[c].get("pending_entry") for c in COINS)
            if any_active or any_pending:
                for _ in range(int(LOOP_INTERVAL / TRAILING_CHECK_INTERVAL)):
                    time.sleep(TRAILING_CHECK_INTERVAL)
                    if self._shutdown:
                        break
                    for pair in PAIRS:
                        coin = pair.split("/")[0]
                        pos = self.positions[coin]
                        live = self.collector.get_live_price(coin)
                        if not live:
                            continue
                        self.trader.pair = pair
                        if pos.get("pending_entry") and not pos["active"]:
                            self.entry.check_pending(coin, live)
                        if not pos["active"]:
                            continue
                        if pos["trailing_active"]:
                            self.pos_mgr.manage_trailing(coin, live)
                        if self.positions[coin]["active"]:
                            self.pos_mgr.check_tp_sl_hit(coin, live)
            else:
                time.sleep(LOOP_INTERVAL)

        self._cleanup()

    # ──────────────────────────────────────────────────────────
    # Traitement d'une paire
    # ──────────────────────────────────────────────────────────

    def _process_pair(self, pair, coin):
        """Cycle complet pour une paire : signal → sync → gestion → ouverture."""
        self.trader.pair = pair

        # Vérifier le solde disponible pour cette paire
        balance = self.trader._get_total_balance()
        usable = balance * (1 - RESERVE_BALANCE_PCT)
        min_col = MIN_COLLATERAL.get(pair, 10)
        if usable < min_col and not self.positions[coin]["active"]:
            if DEBUG:
                print(f"  [{coin}] Solde insuffisant ({usable:.2f} < {min_col}) — skip")
            return

        # 1. Signal (avec seuil auto-calibré) — journalisé par le moteur (V10)
        sig = self.engines[coin].compute_signals(score_threshold=self._signal_threshold)
        last_price = sig["debug"].get("close", 0)
        live_price = self.collector.get_live_price(coin) or last_price
        self._last_signal_scores[coin] = sig["score"]
        # Annuler pending entry si signal a changé de sens
        pending = self.positions[coin].get("pending_entry")
        if pending and sig["score"] != 0 and sig["score"] != pending.get("score"):
            if (pending["direction"] == "buy" and sig["score"] < 0) or \
               (pending["direction"] == "sell" and sig["score"] > 0):
                self.positions[coin]["pending_entry"] = None
                print(f"[BOT][{coin}] ⚠️ Pending annulé — signal a changé de sens")

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        live_tag = f" | live: {live_price:.6g}" if live_price != last_price else ""
        print(f"[{ts}][{coin}] {sig['color']} Score: {sig['score']} (raw: {sig['raw_score']}) | "
              f"{sig['label']} | {last_price:.6g}{live_tag}")

        # 2. Sync exchange
        has_pos, pos_info = self.trader.has_open_position()

        # Position fermée par l'exchange (TP/SL atteint)
        if not has_pos and self.positions[coin]["active"]:
            self.pos_mgr.handle_exchange_closure(coin, live_price)

        # 3. Gestion position existante
        if has_pos and self.positions[coin]["active"]:
            if self.entry.should_reverse(coin, sig):
                print(f"[BOT][{coin}] Signal opposé confirmé — fermeture")
                result = self.trader.close_position(
                    reason="signal_reverse",
                    context=self.pos_mgr.context(coin, closing_signal_id=sig.get("signal_id")))
                if result:
                    self.risk.register_trade_result(result["pnl"])
                    guards.adjust_cooldown(self._cooldowns, coin, result["pnl"])
                self._last_trade_times[coin] = time.time()
                self.positions[coin] = empty_position()
            else:
                self.pos_mgr.manage_trailing(coin, live_price)

        # 4. Ouverture si pas de position
        elif not has_pos:
            self.entry.try_open(coin, sig, live_price)

        if DEBUG:
            risk_status = self.risk.status()
            trailing_state = "ON" if self.positions[coin]["trailing_active"] else "OFF"
            print(f"  [DEBUG][{coin}] pos={has_pos} | trailing={trailing_state} | "
                  f"losses={risk_status['consecutive_losses']} | pnl_day={risk_status['pnl_today']:+.2f}")

    # ──────────────────────────────────────────────────────────
    # Logique de trading (moteur conservé)
    # ──────────────────────────────────────────────────────────

    def _check_mongo_health(self) -> bool:
        """Vérifie que MongoDB est joignable au démarrage (3 tentatives)."""
        if not MONGO_URL:
            print("[BOT] ❌ MONGO_URL absent de l'environnement.")
            return False
        for attempt in range(3):
            try:
                mongo.ping()
                print("[BOT] ✅ MongoDB joignable.")
                return True
            except Exception as e:
                print(f"[BOT] ⚠️ MongoDB injoignable (tentative {attempt + 1}/3): {e}")
                time.sleep(5)
        return False

    def _check_kill_switch(self):
        import os
        if os.path.exists(KILL_SWITCH_FILE):
            print("[BOT] 🛑 KILL SWITCH actif")
            return True
        return False

    def _run_collector(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.collector.collect())
        except Exception as e:
            print(f"[COLLECTOR][FATAL] {e}")
        finally:
            loop.close()

    def _handle_shutdown(self, signum, frame):
        print(f"\n[BOT] Signal {signum} reçu, arrêt en cours...")
        self._shutdown = True

    def _cleanup(self):
        self.collector.stop()
        self.rest_collector.stop()
        if self.whale_collector is not None:
            self.whale_collector.stop()
        balance = self.trader._get_total_balance()
        risk_status = self.risk.status()
        self.notifier.bot_stopped(
            f"PnL jour: {risk_status['pnl_today']:+.2f} | Solde: {balance:.2f}"
        )
        mongo.close()
        print("[BOT] Arrêt complet.")


if __name__ == "__main__":
    import sys
    bot = TradingBot()
    bot.start()
    # Arrêt propre = code 0. Le redémarrage en prod est garanti par
    # fly.toml ([[restart]] policy = 'always'), pas par un code d'erreur.
    sys.exit(0)
