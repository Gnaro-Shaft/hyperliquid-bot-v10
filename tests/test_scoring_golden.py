"""Golden master des règles de scoring, rejouées sur des évaluations RÉELLES.

Le moteur de signaux n'était couvert par rien. Plutôt que d'inventer des cas,
ce test rejoue des évaluations archivées — produites en production entre le
10/07 et le 22/08/2026 — et compare, règle par règle, les points calculés par
le code extrait à ceux enregistrés à l'époque.

Six semaines de fonctionnement réel comme oracle : aucun jeu de cas écrit à la
main n'aurait cette couverture de combinaisons.

Deux limites, assumées et signalées :
- règle 3 (histogramme MACD) : elle dépend de la bougie précédente, absente
  d'une ligne isolée. Ses points sont donc lus dans l'archive, pas recalculés.
- règle 12 (déséquilibre du carnet) : son verdict textuel est écrasé dans le
  `debug` archivé par la valeur numérique du même nom — collision de clé dans
  le moteur (strategy_engine.py:594). Elle est donc validée indirectement, par
  la somme, et non comparée ligne à ligne.
"""

import ast
import collections
import glob
import re

import pytest

from strategy import scoring_rules as sr

ARCHIVE = "/Users/dgnaro/V10-archive/parquet/signal_evaluations"
POINTS = re.compile(r"\(([+-]?\d)\)\s*$")

# Règles comparables une à une : leur texte survit dans l'archive et leurs
# entrées tiennent dans une seule ligne.
# "oi" en est SORTI le 05/09/2026 : la règle a été activée, l'archive a été
# produite avec sa version inerte. Son écart est modélisé explicitement dans le
# test de somme, ce qui garde le golden master exploitable.
COMPARABLES = ["ema_trend", "macd", "rsi", "bb", "vwap", "volume",
               "adx_bonus", "momentum_1m", "funding", "ema_age"]


def _points_archives(texte):
    """Extrait les points du verdict textuel : 'BULLISH+ACC ... (+2)' → 2."""
    if not isinstance(texte, str):
        return None
    m = POINTS.search(texte)
    return int(m.group(1)) if m else None


def _contexte_depuis_ligne(l):
    """Reconstruit le contexte de scoring depuis une ligne archivée."""
    row = {
        "EMA9": l["ema9"], "EMA21": l["ema21"], "EMA9_slope": l["ema9_slope"],
        "MACD": l["macd"], "MACD_signal": l["macd_signal"], "MACD_hist": l["macd_hist"],
        "RSI": l["rsi_14"], "BB_pctB": l["bb_pctb"], "VWAP": l["vwap"],
        "close": l["close_15m"], "open": l["open_15m"], "vol_ratio": l["vol_ratio"],
        "PLUS_DI": l["plus_di"], "MINUS_DI": l["minus_di"],
    }
    mkt = {
        "funding_rate": l["funding_rate"], "funding_slope": l["funding_slope"],
        "oi_trend_30m": l["oi_trend_30m"], "oi_change_pct": l["oi_change_pct"],
        "ob_imbalance_avg": l["ob_imbalance_avg"], "ob_imbalance": l["ob_imbalance"],
    }
    trend_1m = l["trend_1m"]
    return sr.contexte(
        row=row, prev={"MACD_hist": 0.0}, mkt=mkt, adx_val=l["adx_14"],
        confirms_bull_1m=(trend_1m == "bull"), confirms_bear_1m=(trend_1m == "bear"),
        ema_age=int(l["ema_age_candles"]) if l["ema_age_candles"] is not None else 0,
    )


def _echantillon(max_lignes=4000):
    """Lignes archivées exploitables, réparties sur plusieurs coins et jours."""
    import polars as pl
    fichiers = sorted(glob.glob(f"{ARCHIVE}/coin=*/date=*.parquet"))
    if not fichiers:
        pytest.skip(f"archive absente : {ARCHIVE}")
    pas = max(1, len(fichiers) // 12)
    lignes = []
    for f in fichiers[::pas]:
        df = pl.read_parquet(f)
        if "debug" not in df.columns:
            continue
        for l in df.head(400).to_dicts():
            try:
                l["_debug"] = ast.literal_eval(l["debug"])
            except Exception:
                continue
            if not isinstance(l["_debug"], dict):
                continue
            if l["ema_age_candles"] is None or l["raw_score"] is None:
                continue
            if not all(k in l["_debug"] for k in COMPARABLES):
                continue
            lignes.append(l)
            if len(lignes) >= max_lignes:
                return lignes
    return lignes


@pytest.fixture(scope="module")
def echantillon():
    lignes = _echantillon()
    if len(lignes) < 200:
        pytest.skip(f"échantillon insuffisant ({len(lignes)} lignes)")
    return lignes


def test_taille_de_l_echantillon(echantillon):
    print(f"\n  échantillon : {len(echantillon)} évaluations réelles")
    assert len(echantillon) >= 200


@pytest.mark.parametrize("cle", COMPARABLES)
def test_regle_reproduit_la_production(cle, echantillon):
    """Chaque règle doit rendre exactement les points enregistrés à l'époque."""
    regle = dict(sr.REGLES)[cle]
    ecarts = []
    compares = 0
    for l in echantillon:
        attendu = _points_archives(l["_debug"][cle])
        if attendu is None:
            continue
        obtenu, _ = regle(_contexte_depuis_ligne(l))
        compares += 1
        if obtenu != attendu:
            ecarts.append((l["coin"], l["datetime"], attendu, obtenu, l["_debug"][cle]))
    assert compares >= 100, f"{cle} : trop peu de lignes comparables ({compares})"
    assert not ecarts, (f"{cle} : {len(ecarts)}/{compares} écarts. "
                       f"3 premiers : {ecarts[:3]}")


def test_somme_des_13_regles_egale_le_raw_score(echantillon):
    """Vérification de bout en bout, avec l'écart de la règle 11 modélisé.

    La règle 3 est reprise de l'archive (elle dépend de la bougie précédente).
    La règle 11 a été ACTIVÉE le 05/09/2026 alors que l'archive a été produite
    avec sa version inerte : son apport est donc retranché pour retrouver le
    raw_score d'époque. Si une autre règle dérivait, ce test le verrait encore.
    """
    ecarts = []
    for l in echantillon:
        ctx = _contexte_depuis_ligne(l)
        total = 0
        for cle, regle in sr.REGLES:
            if cle == "macd_hist":
                p = _points_archives(l["_debug"].get("macd_hist"))
                if p is None:
                    total = None
                    break
                total += p
            elif cle == "oi":
                # Règle activée après coup : neutralisée pour comparer à l'archive
                continue
            else:
                total += regle(ctx)[0]
        if total is not None and total != l["raw_score"]:
            ecarts.append((l["coin"], l["datetime"], l["raw_score"], total))
    taux = 100 * (1 - len(ecarts) / len(echantillon))
    print(f"\n  somme conforme sur {taux:.2f}% des {len(echantillon)} évaluations")
    assert not ecarts, f"{len(ecarts)}/{len(echantillon)} écarts. 3 premiers : {ecarts[:3]}"


def _ctx_oi(oi, ema9, ema21):
    return sr.contexte(
        row={"EMA9": ema9, "EMA21": ema21, "EMA9_slope": 0.1, "RSI": 50.0,
             "BB_pctB": 0.5, "vol_ratio": 1.0},
        prev={"MACD_hist": 0.0},
        mkt={"funding_rate": None, "funding_slope": None,
             "oi_trend_30m": oi, "oi_change_pct": oi,
             "ob_imbalance_avg": None, "ob_imbalance": None},
        adx_val=25.0, confirms_bull_1m=False, confirms_bear_1m=False, ema_age=10,
    )


@pytest.mark.parametrize("oi,ema9,ema21,attendu", [
    (0.05, 100, 90, 1),    # OI grossit dans le sens de la tendance : confirmation
    (0.05, 90, 100, -1),   # OI grossit à contre-sens : positions du mauvais côté
    (-0.05, 100, 90, -1),  # OI décroît en tendance haussière : désengagement
    (-0.05, 90, 100, 1),   # OI décroît en tendance baissière : short qui se vide
    (0.001, 100, 90, 0),   # variation < 0.2% : non significative
])
def test_table_de_decision_de_la_regle_oi(oi, ema9, ema21, attendu):
    """La règle 11, ACTIVÉE le 05/09/2026 après six semaines d'inertie.

    Elle testait `ema_bull is True` contre un numpy.bool_ — jamais le singleton
    Python — et tombait donc systématiquement dans les cas par défaut. Mesuré
    avant activation sur 1 458 913 évaluations : 31,7 % des scores et 5,57 % des
    niveaux auraient changé.
    """
    points, _ = sr.open_interest(_ctx_oi(oi, ema9, ema21))
    assert points == attendu


def test_l_archive_temoigne_de_l_ancienne_inertie():
    """Trace historique : avant activation, la règle ne rapportait jamais rien.

    Ce test lit l'archive, pas le code : il documente ce qui s'est passé, et
    explique pourquoi "oi" ne figure plus dans COMPARABLES.
    """
    lignes = _echantillon(600)
    if len(lignes) < 100:
        pytest.skip("échantillon insuffisant")
    points = [_points_archives(l["_debug"]["oi"]) for l in lignes
              if isinstance(l["_debug"].get("oi"), str)]
    points = [p for p in points if p is not None]
    assert points, "aucun verdict OI lisible dans l'archive"
    assert set(points) == {0}, (
        "l'archive contient des points OI non nuls : l'hypothèse d'inertie "
        f"est fausse ({collections.Counter(points)})")
