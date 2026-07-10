# Bot Hyperliquid V10 — collecte propre & multi-coins

**Objectif de cette version : instrumentation & collecte, PAS la rentabilité.**
Produire sur ~3 mois un dataset complet, sans trous, horodaté et backtestable,
sur **10 coins** (BTC, ETH, SOL, HYPE, DOGE, WIF, PEPE, SUI, INJ, TIA), pour
déterminer s'il existe un edge exploitable et où.

## Ce qui est conservé de v8 (le moteur — non réécrit)

- Système de « feux » : scoring pondéré 13 indicateurs, niveaux -2..+2,
  raw_score ±17, gates ADX (≥25) / BB width / 1h ([strategy/strategy_engine.py](strategy/strategy_engine.py))
- Indicateurs EMA, RSI, MACD, Bollinger, VWAP, ATR, ADX, vol_ratio, slope
  ([strategy/indicators.py](strategy/indicators.py) — copie verbatim)
- Risk manager (drawdown journalier, pertes consécutives, cooldown, kill switch)
- Trailing stop + breakeven (seule sortie performante de v8)
- Circuit breaker marché, filtre corrélation, garde-fou exposition, pullback entry

Note : le gate ML optionnel de v8 n'est **pas embarqué** (module `ml/` absent →
le moteur le désactive proprement). Choix délibéré : signaux 100 % déterministes
pendant les 3 mois de collecte. Le hook (`try: from ml.predictor…`) est conservé.

## Ce qui change (la plomberie V10)

| Problème v8 | Correction V10 |
|---|---|
| Sentiment rempli à ~38 %, régime 9,7 %, spread 2 % | **[MarketContextStore](collector/context_store.py)** : les collectors poussent, la stratégie lit la *dernière valeur connue* + son âge (`*_age_ms`). Amorcé depuis Mongo au démarrage. |
| Signaux upsertés par bougie (historique écrasé), logging seulement si gate passé | **[SignalLogger](datalog/signal_logger.py)** : une ligne **par évaluation** (insert, `signal_id` unique), y compris gate bloqué et données insuffisantes. Régime sur chaque ligne. |
| Trades non reliés aux signaux | Chaque position porte `signal_id` + snapshot features ; propagés dans `trades` par le [TradeLogger](datalog/trade_logger.py) sur tous les chemins de sortie. |
| 2-3 coins | 10 coins, moteur par coin isolé (une erreur par coin ne bloque pas les autres), arrondi de prix en chiffres significatifs ([utils/prices.py](utils/prices.py)) pour PEPE/WIF/DOGE. |
| Pas de baleines / liquidations | **[WhaleCollector](collector/whale_collector.py)** : positions des top comptes (leaderboard), prix d'entrée + `liquidation_px`, clusters de liquidation par coin, flux nets USDC. À logger, pas câblé au moteur. |
| Trous de collecte invisibles | Heartbeats par flux×coin (`collector_health`) + [HealthMonitor](monitor/health.py) (alerte Telegram « muet > 5 min » sur transition) + [watchdog externe](scripts/external_watchdog.py) si le process meurt. |
| CSV + Mongo en vrac | Mongo (ingestion) → **Parquet** partitionné coin/jour ([scripts/export_parquet.py](scripts/export_parquet.py)) → DuckDB/Polars. |

Couche agents LLM : pas d'orchestrateur (non-objectif), mais le hook de logging
horodaté anti-fuite temporelle existe ([datalog/agent_hooks.py](datalog/agent_hooks.py)).

## Démarrage

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # remplir MONGO_URL, clés HL, Telegram
./venv/bin/python main.py                 # bot + tous les collectors
PAPER_MODE=true ./venv/bin/python main.py # sans ordres réels
```

Collectors seuls (sans trading) : `python -m collector.websocket_collector`,
`python -m collector.rest_collector`, `python -m collector.whale_collector`.

## Vérifier la collecte (critères d'acceptation)

```bash
# % de remplissage par colonne clé, trous d'évaluation, minutes 1m manquantes
# exit 1 si une colonne clé < 95% ou trou > 5 min → cron-able
./venv/bin/python -m scripts.coverage_report --hours 24

# Test de la chaîne d'alerte (envoie réellement un Telegram)
./venv/bin/python -m scripts.external_watchdog --force-alert
```

## Export & analyse

```bash
./venv/bin/python -m scripts.export_parquet --days 2    # quotidien (cron)
```

```python
import duckdb
duckdb.sql("""
  SELECT coin, regime, count(*) n, avg(raw_score) avg_raw
  FROM read_parquet('data/parquet/signal_evaluations/**/*.parquet')
  GROUP BY coin, regime ORDER BY coin, n DESC
""")
```

Schéma complet des collections/tables : [docs/SCHEMA.md](docs/SCHEMA.md).

## Déploiement (archi hybride)

- **Cloud (uptime-critique)** : bot + collectors + Mongo + alertes —
  `fly deploy` (fly.toml : worker `hyperliquid-bot-v10`, restart always).
- **homeserv01 (non-critique)** : cron `export_parquet` + `coverage_report` +
  `external_watchdog` (couvre la mort du process cloud), analyse DuckDB/Polars,
  Grafana, agents LLM plus tard.

Cron suggérés (homeserv01) :
```
*/5 * * * *  cd /path/V10 && ./venv/bin/python -m scripts.external_watchdog
17 2 * * *   cd /path/V10 && ./venv/bin/python -m scripts.export_parquet --days 2
27 2 * * *   cd /path/V10 && ./venv/bin/python -m scripts.coverage_report --hours 24 || true
```

## Tests

```bash
./venv/bin/python -m pytest tests/ -q    # 31 tests (carry-forward, logger,
                                         # alerte muet, couverture, clusters, prix)
```

## Volumétrie attendue

10 coins × 1 évaluation/15 s ≈ **57 600 lignes/jour** dans `signal_evaluations`
(~5 M lignes / 3 mois, quelques Go en Mongo, bien moins en Parquet zstd).
Orderbook : TTL Mongo 90 j (l'archive longue durée vit en Parquet).
