# V10 — Schéma des collections / tables

Hygiène commune : **UTC millisecondes partout** (`timestamp` int ms), clé de
jointure **`(coin, timestamp)`** sur toutes les collections time-series,
**brut + dérivé** stockés (le `debug` brut accompagne les colonnes à plat).

Export Parquet : `data/parquet/<collection>/coin=<COIN>/date=<YYYY-MM-DD>.parquet`
(`python -m scripts.export_parquet`), requêtable en DuckDB :

```sql
SELECT * FROM read_parquet('data/parquet/signal_evaluations/**/*.parquet')
WHERE coin = 'BTC' AND gate_passed ORDER BY timestamp;
```

---

## signal_evaluations — ★ la table centrale (une ligne PAR évaluation, ~15 s × 10 coins)

| Champ | Type | Description |
|---|---|---|
| `signal_id` | str (uuid hex) | Identifiant unique — **clé du lien trade ↔ signal** |
| `timestamp` | int ms UTC | Heure de l'évaluation |
| `candle_ts` | int ms UTC | Timestamp de la bougie 15m source (null si données insuffisantes) |
| `coin` | str | BTC, ETH, SOL, HYPE, DOGE, WIF, PEPE, SUI, INJ, TIA |
| `strategy_id` / `strategy_version` | str | `feux` / `10.0.0` |
| `score` (`signal_level`) | int | Niveau -2..+2 |
| `raw_score` | int | Score brut -17..+17 |
| `gate_passed` | bool | False si bloqué (régime, gate 1h, ML, données insuffisantes) |
| `gate_reason` | str\|null | `regime:RANGE`, `gate_1h`, `gate_ml`, `insufficient_data: …` |
| `threshold_used` | int\|null | Seuil ±2 effectif (auto-cal + ajustement régime) |
| `regime` | str | **Toujours présent** : STRONG / WEAK / HIGH_VOL / RANGE / SQUEEZE |
| **Features 15m** | | `close`, `close_15m`, `open/high/low/volume_15m`, `candle_range_pct`, `ema9`, `ema21`, `ema9_slope`, `ema_age_candles`, `rsi_14`, `macd`, `macd_signal`, `macd_hist`, `bb_upper/lower/pctb/width`, `vwap`, `atr`, `atr_pct`, `vol_ratio`, `adx_14`, `plus_di`, `minus_di` |
| **Sentiment (carry-forward)** | | `funding_rate`, `funding_slope`, `open_interest`, `oi_change_pct`, `oi_trend_30m`, `ob_imbalance`, `ob_imbalance_avg`, `spread_pct`, `ob_depth_ratio`, `bid_depth_5`, `ask_depth_5` — **toujours remplis** (dernière valeur connue) |
| **Âges du sentiment** | int ms | `funding_age_ms`, `oi_age_ms`, `ob_age_ms` — fraîcheur traçable |
| **Résultat** | | `dynamic_tp`, `dynamic_sl`, `trend_1h`, `trend_1m`, `regime_size_mult`, `ml_confidence`, `is_squeeze` |
| `debug` | dict | Brut complet (composantes du score, verdicts textuels) |

Index : `(coin, timestamp)`, `signal_id` (unique).

## trades

Champs v8 (`pair`, `side`, `action` open/close, `entry_price`, `exit_price`,
`size`, `pnl`, `reason`, `tp_price`, `sl_price`) **plus le lien signal (V10)** :

| Champ | Description |
|---|---|
| `signal_id` | id de l'évaluation qui a déclenché l'entrée |
| `closing_signal_id` | id du signal opposé (fermetures `signal_reverse`) |
| `entry_features` | snapshot des features à l'entrée (dict brut) |
| `signal_score` / `raw_score` / `regime` | photo de la décision |
| `coin`, `strategy_id`, `strategy_version`, `timestamp` (ms), `datetime` | traçabilité |

`reason` ∈ `signal` (open), `trailing_stop`, `signal_reverse`, `tp_sl_exchange`, `manual`.
`paper_trades` : même schéma + `paper: true`.

## ohlc_1m / ohlc_15m / ohlc_1h

`timestamp` (début bougie, ms), `timestamp_end`, `minute`, `coin`, `interval`,
`open/high/low/close/volume`, `n` (nb trades). Unique sur `(coin, timestamp)`.

## orderbook_snapshots (~30 s / coin, TTL 90 j)

`timestamp`, `coin`, `best_bid`, `best_ask`, `spread`, `spread_pct`,
`bid_depth_5`, `ask_depth_5`, `imbalance`, `created_at`.

## funding_rates / open_interest (~300 s / coin)

`timestamp`, `coin`, `funding_rate`, `premium`, `mark_price` /
`open_interest`, `oi_change_pct`, `mark_price`. Unique `(coin, timestamp)`.

## market_trades (agrégé par minute)

`timestamp` (minute), `coin`, `buy_volume`, `sell_volume`, `buy_notional`,
`sell_notional`, `trade_count`, `buy_count`, `sell_count`,
`large_trades` (≥ LARGE_TRADE_USD), `large_notional`.

## whale_positions (~180 s, top comptes leaderboard) — nouvelle source V10

`timestamp`, `address`, `coin`, `szi` (>0 long), `entry_px`, `position_value`,
`unrealized_pnl`, **`liquidation_px`**, `leverage`, `margin_used`.

## liquidation_clusters — nouvelle source V10

`timestamp`, `coin`, `mark_px`, `clusters`: liste `{px, notional, n, n_long,
n_short}` — buckets de 0.5% du mark, fenêtre ±25%, triés par notionnel.

## whale_flows — nouvelle source V10

`timestamp`, `window_start`, `deposits_usdc`, `withdrawals_usdc`,
`net_flow_usdc`, `n_deposits`, `n_withdrawals`, `n_addresses`. (`coin`="ALL" :
flux au niveau compte.)

## agent_outputs — hooks agents LLM (anti-fuite temporelle)

`agent_output_id`, `agent_id`, `coin` (null = global), `produced_at`,
`logged_at`, **`valid_from`** (= `timestamp`), `model`, `prompt_version`,
`payload`. *Règle backtest : ne jamais joindre une sortie à un instant
antérieur à `valid_from`.*

## decisions (journal d'entrée accepté/refusé)

Champs v8 (contrat GCN Dashboard : `status`, `motif`, `created_at`) + V10 :
`signal_id`, `regime`.

## Observabilité

- **collector_health** : `_id`=`<component>:<coin>`, `component`
  (`ws_candles`, `ws_orderbook`, `ws_trades`, `rest_funding_oi`,
  `whale_positions`, `liq_clusters`, `whale_flows`), `coin`, `last_write_ms`.
  → alerte « muet > 5 min » (HealthMonitor embarqué + watchdog externe).
- **bot_status** : doc unique `_id="current"` (heartbeat bot, contrat dashboard)
  + `stale_streams` (V10).
- **risk_state**, **paper_state** : états persistés.

## Jointures type (DuckDB)

```sql
-- Trades avec leur signal d'entrée complet
SELECT t.*, s.regime, s.funding_rate, s.adx_14, s.raw_score
FROM read_parquet('data/parquet/trades/**/*.parquet') t
JOIN read_parquet('data/parquet/signal_evaluations/**/*.parquet') s
  ON t.signal_id = s.signal_id;

-- ASOF join sentiment ↔ bougies sur (coin, timestamp)
SELECT c.*, f.funding_rate
FROM read_parquet('data/parquet/ohlc_15m/**/*.parquet') c
ASOF JOIN read_parquet('data/parquet/funding_rates/**/*.parquet') f
  ON c.coin = f.coin AND c.timestamp >= f.timestamp;
```

## Anomalies connues du dataset

À lire avant tout backtest ou toute analyse : ces trous sont réels et documentés,
ce ne sont pas des bugs de lecture.

### Interruption du 20/08 au 05/09/2026 — 16 jours

La machine Fly.io a été tuée par l'OOM killer le **22/08/2026 à 08:10 UTC**
(`exit_code=137`), et n'a été relancée que le **05/09/2026 à 05:16 UTC**, sur un VPS
OVH. Aucune donnée n'a été collectée dans l'intervalle.

Deux fenêtres distinctes, car l'export Parquet s'était arrêté avant le bot :

| Période | Collectes de marché | Six collections purgeables* |
|---|---|---|
| jusqu'au 20/08 02:05 | présent | présent |
| 20/08 02:05 → 22/08 08:09 | **récupéré** depuis Mongo le 05/09 | **perdu** |
| 22/08 08:09 → 05/09 05:16 | néant (bot arrêté) | néant |

\* `signal_evaluations`, `orderbook_snapshots`, `market_trades`, `whale_positions`,
`liquidation_clusters`, `ohlc_1m` — purgées de Mongo après archivage, donc absentes
des deux côtés pour cette fenêtre. Soit ~111 000 évaluations au maximum, si le moteur
tournait encore : `bot_status` s'arrête au 20/08 02:04, ce qui laisse penser que non.

### `paper_trades` — ligne d'ouverture manquante le 05/09/2026

La position BTC ouverte le **05/09/2026 à 05:29:38 UTC** (`entry 79554.0`,
`size 0.001545`, TP 81670, SL 78707) n'a **pas** de ligne `action=open` dans
`paper_trades` : elle a été supprimée par erreur lors du nettoyage de faux trades
insérés par une exécution de `pytest` le même jour (voir la mise en garde ci-dessous).
Sa ligne `action=close` existera normalement.

Choix délibéré de ne pas la reconstituer : dans un dataset destiné au backtest, une
ligne fabriquée serait indiscernable d'une ligne réelle. Un trou identifié vaut mieux.

### Règle 11 (open interest) activée le 05/09/2026 — DISCONTINUITÉ

Un indicateur sur treize n'a **jamais** contribué au score entre le 10/07 et le
05/09/2026. Le moteur testait `ema_bull is True` alors qu'`ema_bull` provient
d'une comparaison pandas, donc un `numpy.bool_`, qui n'est jamais le singleton
Python. Les branches attribuant ±1 n'étaient jamais atteintes.

**Toute analyse portant à cheval sur le 05/09/2026 mélange deux moteurs.**

Impact mesuré sur les 1 458 913 évaluations archivées avant activation
(gate passé, en rejouant la règle telle qu'activée) :

| | |
|---|---|
| Score brut modifié (±1) | 462 178 — 31,7 % |
| Niveau de signal modifié | 81 210 — 5,57 % |
| Entrées supplémentaires (±2 gagné) | 11 294 |
| Entrées supprimées (±2 perdu) | 2 327 |

L'écart est **exactement reproductible** : la règle est déterministe à partir de
`oi_trend_30m` (ou `oi_change_pct` à défaut), `ema9` et `ema21`, toutes trois
présentes dans l'archive. Pour homogénéiser une analyse, recalculer le delta
plutôt que de rescorer — les décisions prises à l'époque l'ont été sur les
scores d'époque, les réécrire falsifierait l'historique.

Le comportement d'origine est documenté par un test qui lit l'archive
(`tests/test_scoring_golden.py::test_l_archive_temoigne_de_l_ancienne_inertie`).

### Géométrie SL/TP multipliée par 5 le 05/09/2026 — DISCONTINUITÉ

Toutes les distances de sortie ont été mises à l'échelle ×5 le même jour, et
rien d'autre n'a changé — les rapports entre elles sont ceux d'avant.

| | avant | après |
|---|---|---|
| `dynamic_sl` (médiane) | 0,600 % | **3,000 %** |
| `dynamic_tp` (médiane, régime STRONG) | 2,500 % | **12,500 %** |
| déclenchement du trailing | 1,0 % | **5,0 %** |
| distance du trailing | 0,6 % | **3,0 %** |
| notionnel par trade | 24 % du solde | **4,8 %** |
| risque par trade | 0,1440 % du solde | **0,1440 %** (inchangé) |

**Motif** : le handicap de frais. Avec les anciennes barrières, il fallait
battre la marche aléatoire de 5,42 points pour seulement couvrir les frais,
alors que 1 074 setups n'en détectent que ~3,5 — le handicap dépassait la
résolution de la mesure. Il tombe à 1,12 point.
Voir `falsification-2026-09-05.md` et `protocole-pre-enregistre.md`.

**Conséquences pour l'analyse :**

- Les colonnes `dynamic_sl` et `dynamic_tp` changent d'échelle à cette date.
  Une analyse à cheval mélange deux géométries : les comparer directement n'a
  pas de sens.
- Les scores, niveaux et features ne sont **pas** touchés. Seules les sorties
  le sont. Une étude portant sur le signal seul reste homogène de part et
  d'autre — sous réserve de la discontinuité de la règle 11, ci-dessus.
- La durée de détention change d'ordre de grandeur : médiane ~2 h avant,
  ~43 h attendues après. Les trades des deux périodes ne sont pas comparables.
- Nouvelle cause de clôture dans `paper_trades` : `max_hold`, à 7 jours
  (`MAX_HOLD_SEC`), alignée sur l'horizon du test de barrière. 17 % des setups
  ne se résolvent pas sous ce délai.
- Le notionnel minimal (12 USDC) est désormais atteint sous **833 USDC** de
  solde, contre 150 auparavant. Sous ce seuil, `taille_ordre` remonte la taille
  au minimum — le risque effectif y devient supérieur au nominal calculé, tout
  en restant sous 0,144 %.

Les invariants qui justifient ces valeurs sont vérifiés par
`tests/test_geometrie.py`, dont la capacité à détecter une mise à l'échelle
incomplète a été prouvée en réintroduisant les quatre fautes possibles.

### `pytest` écrivait dans la base de production — corrigé le 05/09/2026

`tests/test_paper_trader.py` neutralisait `PaperTrader._connect`, mais `TradeLogger`
ouvrait alors son propre client vers `MONGO_URL` et insérait de vrais documents dans
`paper_trades` : c'est l'origine des deux anomalies ci-dessus.

`tests/conftest.py` rend désormais la suite hermétique — `MongoClient` y lève une
exception, dans `pymongo` comme dans chaque module du projet. `tests/test_isolation.py`
vérifie que la garde est active, y compris pour un module importé en cours de session.
Toute donnée de `paper_trades` antérieure au 05/09/2026 05:00 UTC peut donc contenir
des artefacts de tests ; au-delà, non.
