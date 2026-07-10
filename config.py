import os
from dotenv import load_dotenv

load_dotenv()

# === IDENTITÉ STRATÉGIE (V10 — traçabilité dataset) ===
# Chaque ligne de signal et chaque trade portent ces deux champs.
STRATEGY_ID = "feux"            # moteur de scoring conservé de v8 (système de feux)
STRATEGY_VERSION = "10.0.0"     # version de la plomberie/collecte

# === MODE PAPER TRADING ===
# Si PAPER_MODE=true (env), le bot reçoit les vrais prix mais N'ENVOIE AUCUN ordre.
PAPER_MODE = os.getenv("PAPER_MODE", "false").strip().lower() in ("1", "true", "yes")
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "1000"))

# === EXCHANGE & PAIRS (V10 : 10 coins, collecte + trading tiny-size) ===
# Noms CANONIQUES Hyperliquid (WS + clearinghouse + Mongo). PEPE n'existe pas
# sur HL : le contrat est kPEPE (1000 PEPE). Souscrire un coin inconnu fait
# fermer TOUTE la connexion WS sans message d'erreur (constaté le 10/07/2026).
# Côté ccxt le symbole s'écrit KPEPE/USDC:USDC — conversion à la frontière
# exchange uniquement (trader/ccxt_trader._ccxt_symbol).
V10_COINS = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "WIF", "kPEPE", "SUI", "INJ", "TIA"]
PAIRS = [f"{c}/USDC:USDC" for c in V10_COINS]
COLLECT_PAIRS = PAIRS           # tout ce qui est tradé est collecté (et inversement)

MIN_COLLATERAL = {pair: 10 for pair in PAIRS}   # notionnel minimum HL ~$10

# === MONEY MANAGEMENT (inchangé — collecte tiny-size) ===
POSITION_SIZE_PCT = 0.30        # 30% du solde par trade
RESERVE_BALANCE_PCT = 0.20      # 20% toujours en reserve

# === TIMEFRAMES ===
TIMEFRAMES = {
    "main": "1m",
    "confirm": "15m"
}

# === SIGNALS (SCORING 5 NIVEAUX) — moteur conservé ===
LEVELS = {
    -2: {"label": "Vente forte",  "color": "\U0001f534"},
    -1: {"label": "Vente legere", "color": "\U0001f7e0"},
     0: {"label": "Neutre",       "color": "⚪️"},
     1: {"label": "Achat leger",  "color": "\U0001f7e2"},
     2: {"label": "Achat fort",   "color": "\U0001f7e9"},
}

# === SL / TP / TRAILING (moteur conservé) ===
SL_PCT = 0.012                  # Stop Loss 1.2%
TP_PCT = 0.03                   # Take Profit 3% (R:R = 2.5:1)
MIN_TP_PCT = 0.02               # TP minimum 2%
TRAIL_PCT = 0.006               # Trailing Stop 0.6%
TRAILING_TRIGGER_PCT = 0.010    # Active le trailing apres +1.0%
TRAILING_STEP_PCT = 0.003       # Rehausse le stop tous les +0.3%

# === BREAKEVEN STOP ===
BREAKEVEN_TRIGGER_PCT = 0.010   # Protéger seulement après +1.0%
BREAKEVEN_OFFSET_PCT = 0.002    # SL placé à entry + 0.2%

# === ANTI-OVERTRADING ===
MAX_CONSECUTIVE_LOSSES = 3
PAUSE_DURATION_MINUTES = 15
MAX_DAILY_DRAWDOWN_PCT = 0.05   # Arret si -5% du solde initial du jour

# === LIMITES D'EXPOSITION GLOBALE ===
MAX_OPEN_POSITIONS       = 2     # Nb max de positions simultanees (toutes paires)
MAX_POSITIONS_PER_DIR    = 1     # Nb max de positions dans la meme direction
MAX_TOTAL_EXPOSURE_PCT   = 0.60  # Exposition notionnelle totale max (% du solde)

# === HEALTHCHECK / OBSERVABILITÉ (V10) ===
HEALTH_CHECK_INTERVAL_SEC = 60     # Verification toutes les 60s (was 300 — garde-fou anti-trous)
HEALTH_MAX_1M_AGE_SEC     = 300    # Bougie 1m la plus recente doit dater de < 5 min
HEALTH_MAX_15M_AGE_SEC    = 2400   # Bougie 15m la plus recente doit dater de < 40 min
HEALTH_MAX_CONSEC_ERRORS  = 5      # Alerte au-dela de N erreurs consecutives
COLLECTOR_SILENT_ALERT_SEC = 300   # Alerte « collector muet » si aucun heartbeat < 5 min

# === CIRCUIT BREAKER MARCHE (seuils v8 recalibrés Axe A) ===
CB_MAX_ATR_PCT          = 0.02
CB_MAX_ABS_FUNDING      = 0.0002
CB_MAX_CANDLE_RANGE_PCT = 0.04
CB_MAX_SPREAD_PCT       = 0.0005
CB_MIN_OB_DEPTH_RATIO   = 0.25

# === COOLDOWN DYNAMIQUE ===
COOLDOWN_BASE_SEC  = 600
COOLDOWN_MIN_SEC   = 300
COOLDOWN_MAX_SEC   = 3600
COOLDOWN_LOSS_MULT = 1.5
COOLDOWN_WIN_MULT  = 0.75
COOLDOWN_BETWEEN_TRADES_SEC = COOLDOWN_BASE_SEC  # alias rétrocompat

# === SIGNAL CONFIRMATION ===
SIGNAL_CONFIRM_COUNT = 3

# === LOOP TIMING ===
LOOP_INTERVAL = 15              # Boucle principale (secondes)
TRAILING_CHECK_INTERVAL = 3     # Check trailing quand position active (secondes)

# === PULLBACK ENTRY ===
PULLBACK_PCT = 0.0015
PULLBACK_EXPIRY_SEC = 45

# === AUTO-CALIBRATION SEUIL ===
AUTOCAL_LOOKBACK_TRADES = 20
SIGNAL_THRESHOLD_DEFAULT = 9
SIGNAL_THRESHOLD_MIN = 7
SIGNAL_THRESHOLD_MAX = 10

# === NOTIFICATIONS TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === MONGODB ===
MONGO_URL = os.getenv("MONGO_URL", "")
MONGO_DB = os.getenv("MONGO_DB", "bot_hyperliquid_v10")
MONGO_COLLECTION_TRADES = "trades"
MONGO_COLLECTION_SIGNALS = "signal_evaluations"   # V10 : une ligne PAR évaluation
MONGO_COLLECTION_1M = "ohlc_1m"
MONGO_COLLECTION_15M = "ohlc_15m"
MONGO_COLLECTION_1H = "ohlc_1h"

# Collecte marché
MONGO_COLLECTION_ORDERBOOK = "orderbook_snapshots"
MONGO_COLLECTION_FUNDING = "funding_rates"
MONGO_COLLECTION_OI = "open_interest"
MONGO_COLLECTION_TRADES_MARKET = "market_trades"

# V10 — nouvelles sources (baleines / on-chain HL)
MONGO_COLLECTION_WHALE_POSITIONS = "whale_positions"
MONGO_COLLECTION_LIQ_CLUSTERS = "liquidation_clusters"
MONGO_COLLECTION_WHALE_FLOWS = "whale_flows"

# V10 — hooks agents LLM (sorties horodatées, backtest sans fuite temporelle)
MONGO_COLLECTION_AGENT_OUTPUTS = "agent_outputs"

# V10 — observabilité
MONGO_COLLECTION_HEARTBEATS = "collector_health"  # heartbeats par composant/coin
MONGO_COLLECTION_DECISIONS = "decisions"          # journal de decision (ouvert/refuse)
MONGO_COLLECTION_BOT_STATUS = "bot_status"        # heartbeat etat du bot (doc _id="current")

# Paper trading
MONGO_COLLECTION_PAPER_TRADES = "paper_trades"
MONGO_COLLECTION_PAPER_STATE = "paper_state"

# === ADAPTATION PAR RÉGIME (moteur conservé) ===
REGIME_ADAPTIVE = os.getenv("REGIME_ADAPTIVE", "true").strip().lower() in ("1", "true", "yes")
REGIME_HIGH_VOL_ATR_PCT = 0.010

# === INTERVALLES DE COLLECTE ===
DL_SNAPSHOT_INTERVAL = 30       # Secondes entre snapshots orderbook (par coin)
DL_REST_INTERVAL = 300          # Secondes entre polls REST (funding/OI)

# === COLLECTE BALEINES (V10) ===
WHALE_ENABLED = os.getenv("WHALE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
WHALE_POLL_INTERVAL = int(os.getenv("WHALE_POLL_INTERVAL", "180"))       # s entre cycles positions
WHALE_LEADERBOARD_REFRESH_SEC = 6 * 3600                                 # refresh top adresses
WHALE_TOP_N = int(os.getenv("WHALE_TOP_N", "30"))                        # nb d'adresses suivies
# Adresses forcées (CSV d'adresses 0x...) — utilisées EN PLUS du leaderboard,
# et seules si le leaderboard (endpoint non officiel) est indisponible.
WHALE_ADDRESSES = [a.strip() for a in os.getenv("WHALE_ADDRESSES", "").split(",") if a.strip()]
LIQ_CLUSTER_BUCKET_PCT = 0.005    # largeur des buckets de clusters de liquidation (0.5%)
# Fenêtre de clustering autour du mark. Large par défaut (±100%) : les top
# comptes du leaderboard sont PEU leveragés (liq à 37%..457%+ du marché,
# mesuré le 10/07/2026 — ±25% excluait 23 positions sur 23). On logge tout
# avec la distance ; le filtrage par pertinence se fait à l'analyse.
LIQ_CLUSTER_RANGE_PCT = float(os.getenv("LIQ_CLUSTER_RANGE_PCT", "1.0"))

# Seuil « gros trade » en NOTIONNEL USD (coin-agnostique — V10 multi-coins)
LARGE_TRADE_USD = float(os.getenv("LARGE_TRADE_USD", "25000"))

# === STOCKAGE / EXPORT PARQUET (V10) ===
DATA_DIR = "data"
PARQUET_DIR = os.getenv("PARQUET_DIR", os.path.join(DATA_DIR, "parquet"))

# === API KEYS ===
HYPERLIQUID_API_KEY = os.getenv("HYPERLIQUID_API_KEY", "")
HYPERLIQUID_API_SECRET = os.getenv("HYPERLIQUID_API_SECRET", "")

# === DEBUG ===
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# === KILL SWITCH ===
KILL_SWITCH_FILE = "KILL"  # Creer ce fichier pour arreter le bot
