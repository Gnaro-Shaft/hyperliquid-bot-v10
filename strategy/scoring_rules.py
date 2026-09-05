"""
Les 13 règles de scoring du moteur de signaux — le cœur de la stratégie.

Extraites de compute_signals(), méthode de 470 lignes où elles s'enchaînaient en
mutant deux variables locales (`score` et `debug`). Chacune est désormais une
fonction pure : elle reçoit un contexte et retourne (points, texte). La table
REGLES rend la pondération visible en un seul endroit — auparavant il fallait
lire 205 lignes pour savoir ce qui pesait le plus.

Les textes de debug sont reproduits au format près : ils sont archivés dans
signal_evaluations depuis juillet, et les changer romprait la continuité du
dataset.

ATTENTION — couplage conservé de v8 : la règle 13 (âge de tendance) lit
`ema_bull`, que la règle 11 (open interest) réassigne quand elle dispose d'une
valeur d'OI. Les deux valeurs coïncident tant qu'EMA9 et EMA21 ne sont pas NaN,
et restent toutes deux falsy sinon — l'effet est donc nul en pratique, mais la
dépendance est réelle. Elle est ici explicite : `ema_bull` est calculé une fois
dans le contexte, au lieu de dépendre de l'ordre d'exécution.
"""

import pandas as pd


def age_tendance(ema_dir_series):
    """Nombre de bougies consécutives passées dans le sens actuel de l'EMA.

    Alimente la règle 13 : elle se mesure sur la série entière, pas sur la
    seule dernière bougie.
    """
    if len(ema_dir_series) == 0:
        return 0
    sens = ema_dir_series.iloc[-1]
    age = 0
    for i in range(len(ema_dir_series) - 1, -1, -1):
        if ema_dir_series.iloc[i] == sens:
            age += 1
        else:
            break
    return age


def contexte(row, prev, mkt, adx_val, confirms_bull_1m, confirms_bear_1m, ema_age):
    """Rassemble tout ce dont les règles ont besoin, une fois pour toutes."""
    return {
        "row": row,
        "prev": prev,
        "mkt": mkt,
        "adx_val": adx_val,
        "confirms_bull_1m": confirms_bull_1m,
        "confirms_bear_1m": confirms_bear_1m,
        "ema_age": ema_age,
        "ema_bull": bool(row["EMA9"] > row["EMA21"])
                    if pd.notna(row["EMA9"]) and pd.notna(row["EMA21"]) else False,
        "rsi_val": row["RSI"] if pd.notna(row["RSI"]) else 50.0,
        # Valeurs dérivées réutilisées hors scoring (document signal, gate ML) :
        # les exposer ici évite de les recalculer — et évite surtout qu'elles
        # disparaissent quand les règles bougent.
        "bb_pctb": row["BB_pctB"] if pd.notna(row["BB_pctB"]) else 0.5,
        "vol_r": row["vol_ratio"] if pd.notna(row["vol_ratio"]) else 1.0,
        "funding": mkt["funding_rate"],
        "imbalance": mkt["ob_imbalance"],
        "oi_chg": mkt["oi_change_pct"],
    }


def ema_trend(ctx):
    """1. Tendance EMA + pente (poids x2 si saine, x1 si elle décélère)."""
    row = ctx["row"]
    slope = row["EMA9_slope"] if pd.notna(row["EMA9_slope"]) else 0.0
    ema_bull = ctx["ema_bull"]
    if ema_bull and slope > 0:
        return 2, f"BULLISH+ACC slope={slope:.4f}% (+2)"
    if ema_bull:
        return 1, f"BULLISH+DECL slope={slope:.4f}% (+1)"
    if not ema_bull and slope < 0:
        return -2, f"BEARISH+ACC slope={slope:.4f}% (-2)"
    return -1, f"BEARISH+DECL slope={slope:.4f}% (-1)"


def macd(ctx):
    """2. Momentum MACD (poids x2)."""
    row = ctx["row"]
    if row["MACD"] > row["MACD_signal"]:
        return 2, "BULLISH (+2)"
    return -2, "BEARISH (-2)"


def macd_hist(ctx):
    """3. Histogramme MACD : croisement zéro frais (±2) ou continuation (±1)."""
    row, prev = ctx["row"], ctx["prev"]
    sign_changed = (row["MACD_hist"] > 0) != (prev["MACD_hist"] > 0)
    growing = row["MACD_hist"] > prev["MACD_hist"]
    if sign_changed:
        if row["MACD_hist"] > 0:
            return 2, f"FRESH BULL CROSS {row['MACD_hist']:.4f} (+2)"
        return -2, f"FRESH BEAR CROSS {row['MACD_hist']:.4f} (-2)"
    if growing:
        return 1, f"GROWING {row['MACD_hist']:.4f} (+1)"
    return -1, f"SHRINKING {row['MACD_hist']:.4f} (-1)"


def rsi(ctx):
    """4. RSI — contrarian aux extrêmes (poids x1)."""
    v = ctx["rsi_val"]
    if v > 65:
        return -1, f"OVERBOUGHT {v:.1f} (-1)"
    if v < 35:
        return 1, f"OVERSOLD {v:.1f} (+1)"
    return 0, f"NEUTRAL {v:.1f} (0)"


def bollinger(ctx):
    """5. Bollinger %B, confirmé par le RSI (poids x1)."""
    row = ctx["row"]
    pctb = ctx["bb_pctb"]
    rsi_val = ctx["rsi_val"]
    if pctb > 0.85 and rsi_val > 55:
        return -1, f"OVEREXTENDED %B={pctb:.2f} (-1)"
    if pctb < 0.15 and rsi_val < 45:
        return 1, f"OVERSOLD ZONE %B={pctb:.2f} (+1)"
    return 0, f"INSIDE %B={pctb:.2f} (0)"


def vwap(ctx):
    """6. Position par rapport au VWAP (poids x1)."""
    row = ctx["row"]
    if pd.notna(row["VWAP"]) and row["VWAP"] > 0:
        if row["close"] > row["VWAP"]:
            return 1, f"ABOVE {row['VWAP']:.2f} (+1)"
        return -1, f"BELOW {row['VWAP']:.2f} (-1)"
    return 0, "N/A (0)"


def volume(ctx):
    """7. Pic de volume — dans le sens de la bougie (poids x1)."""
    row = ctx["row"]
    vol_r = ctx["vol_r"]
    if vol_r > 1.8:
        candle_dir = 1 if row["close"] > row["open"] else -1
        return candle_dir, f"SPIKE x{vol_r:.1f} ({'+' if candle_dir > 0 else ''}{candle_dir})"
    return 0, f"NORMAL x{vol_r:.1f} (0)"


def adx_bonus(ctx):
    """8. Bonus de tendance forte, dans le sens des DI (poids x1)."""
    row, adx_val = ctx["row"], ctx["adx_val"]
    if adx_val >= 30:
        plus_di = row["PLUS_DI"] if pd.notna(row["PLUS_DI"]) else 0
        minus_di = row["MINUS_DI"] if pd.notna(row["MINUS_DI"]) else 0
        if plus_di > minus_di:
            return 1, f"STRONG TREND +DI>-DI ({plus_di:.1f}>{minus_di:.1f}) (+1)"
        return -1, f"STRONG TREND -DI>+DI ({minus_di:.1f}>{plus_di:.1f}) (-1)"
    return 0, f"MODERATE TREND ADX={adx_val:.1f} (0)"


def momentum_1m(ctx):
    """9. Momentum 1 minute — timing précis d'entrée (poids x1)."""
    if ctx["confirms_bull_1m"]:
        return 1, "BULLISH (+1)"
    if ctx["confirms_bear_1m"]:
        return -1, "BEARISH (-1)"
    return 0, "NO DATA (0)"


def funding(ctx):
    """10. Funding rate — contrarian, doublé si la pente aggrave (poids x1 ou x2)."""
    mkt = ctx["mkt"]
    valeur = ctx["funding"]
    pente = mkt["funding_slope"]          # None si moins de 2 relevés
    if valeur is None:
        return 0, "NO DATA (0)"
    slope_str = f" slope={pente*100:.5f}%" if pente is not None else ""
    if valeur > 0.0002:
        if pente is not None and pente > 0:
            return -2, f"LONGS SUREXTENDUS+MONTANT {valeur*100:.4f}%{slope_str} (-2)"
        return -1, f"LONGS SUREXTENDUS {valeur*100:.4f}%{slope_str} (-1)"
    if valeur < -0.0002:
        if pente is not None and pente < 0:
            return 2, f"SHORTS SUREXTENDUS+MONTANT {valeur*100:.4f}%{slope_str} (+2)"
        return 1, f"SHORTS SUREXTENDUS {valeur*100:.4f}%{slope_str} (+1)"
    return 0, f"NEUTRAL {valeur*100:.4f}%{slope_str} (0)"


def open_interest(ctx):
    """11. Open interest sur 30 min, croisé avec la tendance EMA (poids x1).

    Un OI qui grossit dans le sens de la tendance la confirme ; un OI qui
    grossit à contre-sens signale des positions qui s'accumulent du mauvais
    côté. Un OI qui décroît traduit un désengagement, donc un affaiblissement.

    ACTIVÉE le 05/09/2026. Cette règle n'avait JAMAIS rapporté de points depuis
    l'origine : v8 testait `ema_bull is True` contre un numpy.bool_, qui n'est
    jamais le singleton Python, et le flux tombait dans les cas par défaut.
    Mesuré sur 1 458 913 évaluations archivées avant activation : 31,7 % des
    scores et 5,57 % des niveaux de signal auraient changé (11 294 entrées
    supplémentaires, 2 327 supprimées).

    Le dataset porte donc une discontinuité à cette date — voir docs/SCHEMA.md.
    """
    mkt, ema_bull = ctx["mkt"], ctx["ema_bull"]
    oi_trend = mkt.get("oi_trend_30m")
    oi_val = oi_trend if oi_trend is not None else mkt["oi_change_pct"]
    if oi_val is None:
        return 0, "NO DATA (0)"
    src = "30m" if oi_trend is not None else "1poll"
    if abs(oi_val) < 0.002:               # variation < 0.2% : non significative
        return 0, f"OI STABLE {oi_val*100:.3f}% ({src}) (0)"
    if oi_val > 0:
        if ema_bull:
            return 1, f"OI GROWING +{oi_val*100:.3f}% ({src}) BULL (+1)"
        return -1, f"OI GROWING +{oi_val*100:.3f}% ({src}) BEAR (-1)"
    if ema_bull:
        return -1, f"OI DECLINING {oi_val*100:.3f}% ({src}) BULL WEAKENING (-1)"
    return 1, f"OI DECLINING {oi_val*100:.3f}% ({src}) BEAR WEAKENING (+1)"


def ob_imbalance(ctx):
    """12. Déséquilibre du carnet, moyenné sur 5 min (poids x1)."""
    mkt = ctx["mkt"]
    moyenne = mkt.get("ob_imbalance_avg")
    val = moyenne if moyenne is not None else mkt["ob_imbalance"]
    if val is None:
        return 0, "NO DATA (0)"
    src = "5min_avg" if moyenne is not None else "snapshot"
    if val > 0.20:
        return 1, f"BID WALL {val:.3f} ({src}) (+1)"
    if val < -0.20:
        return -1, f"ASK WALL {val:.3f} ({src}) (-1)"
    return 0, f"BALANCED {val:.3f} ({src}) (0)"


def ema_age(ctx):
    """13. Âge de la tendance EMA — anti-entrée tardive (poids x1).

    Pénalise dans le sens dominant si la tendance dure depuis plus de 20 bougies
    de 15 min (5 heures), la récompense si elle a moins de 6 bougies.
    """
    age, ema_bull = ctx["ema_age"], ctx["ema_bull"]
    if age > 20:
        penalite = -1 if ema_bull else 1
        return penalite, f"OLD TREND {age}x15m={age*15}min ({penalite:+d})"
    if age <= 5:
        bonus = 1 if ema_bull else -1
        return bonus, f"FRESH TREND {age}x15m={age*15}min ({bonus:+d})"
    return 0, f"MATURE TREND {age}x15m={age*15}min (0)"


# Ordre et clés de debug identiques à v8 — le dataset archivé en dépend.
REGLES = [
    ("ema_trend", ema_trend),
    ("macd", macd),
    ("macd_hist", macd_hist),
    ("rsi", rsi),
    ("bb", bollinger),
    ("vwap", vwap),
    ("volume", volume),
    ("adx_bonus", adx_bonus),
    ("momentum_1m", momentum_1m),
    ("funding", funding),
    ("oi", open_interest),
    ("ob_imbalance", ob_imbalance),
    ("ema_age", ema_age),
]


def scorer(ctx):
    """Applique les 13 règles. Retourne (score_brut, {clé_debug: texte})."""
    score = 0
    debug = {}
    for cle, regle in REGLES:
        points, texte = regle(ctx)
        score += points
        debug[cle] = texte
    return score, debug
