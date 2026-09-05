"""
Gates du moteur de signaux : ce qui bloque une entrée, et à quel seuil.

Trois barrières successives, extraites de compute_signals() :

1. le filtre anti-chop, qui refuse d'opérer dans un marché sans direction
   (ADX faible, bandes de Bollinger resserrées) et fixe le régime ;
2. le gate 1 h, qui refuse un signal à contre-courant de la tendance horaire ;
3. le gate ML, optionnel, qui bloque ou pénalise selon la confiance du modèle —
   inactif tant qu'aucun modèle n'est embarqué.

La normalisation [-2, +2] est ici plutôt que dans la construction du signal :
c'est elle qui transforme un score brut en niveau, les gates agissent sur ce
niveau, et le gate ML la rejoue après pénalité. Dans v8 elle était écrite deux
fois à l'identique, dix lignes chacune.
"""

from config import REGIME_ADAPTIVE, REGIME_HIGH_VOL_ATR_PCT
from utils.regime import regime_preset

ML_BLOCK_THRESHOLD = 0.38     # en dessous → entrée bloquée (signal très peu probable)
ML_PENALTY_THRESHOLD = 0.48   # en dessous → pénalité -1 (signal douteux)

SEUIL_SQUEEZE_BB = 0.004      # bandes plus serrées que ça = marché atone
SEUIL_ADX_TENDANCE = 25       # en deçà, pas de tendance exploitable
SEUIL_ADX_FORTE = 30          # au-delà, tendance franche (presets legacy)


def evaluer_regime(adx_val, bb_w, atr_pct):
    """Détermine le régime de marché et décide si l'entrée est ouverte.

    Retourne un dict : regime, threshold_adj, tp_mult, sl_mult, size_mult,
    blocked, is_squeeze, is_trending, et les textes de debug associés.

    Deux comportements selon REGIME_ADAPTIVE : les presets adaptatifs de V10,
    ou le comportement legacy v8.4 aux presets neutres. Le second est conservé
    tel quel — c'est un interrupteur de configuration, pas du code mort.
    """
    is_squeeze = bb_w < SEUIL_SQUEEZE_BB
    is_trending = adx_val >= SEUIL_ADX_TENDANCE

    if REGIME_ADAPTIVE:
        preset = regime_preset(adx_val, bb_w, atr_pct,
                               high_vol_atr=REGIME_HIGH_VOL_ATR_PCT)
        resultat = {
            "regime": preset["regime"],
            "threshold_adj": preset["threshold_adj"],
            "tp_mult": preset["tp_mult"],
            "sl_mult": preset["sl_mult"],
            "size_mult": preset["size_mult"],
            "blocked": preset["blocked"],
        }
    else:
        # Comportement legacy v8.4 (presets neutres)
        if adx_val >= SEUIL_ADX_FORTE:
            regime, threshold_adj = "STRONG", 0
        elif adx_val >= SEUIL_ADX_TENDANCE:
            regime, threshold_adj = "WEAK", 1
        else:
            regime, threshold_adj = "RANGE", 0
        resultat = {
            "regime": regime, "threshold_adj": threshold_adj,
            "tp_mult": 1.0, "sl_mult": 1.0, "size_mult": 1.0,
            "blocked": (not is_trending) or is_squeeze,
        }

    resultat["is_squeeze"] = is_squeeze
    resultat["is_trending"] = is_trending
    regime = resultat["regime"]
    resultat["debug"] = {
        "adx": f"{adx_val:.1f} ({regime})",
        "bb_width_filter": f"{bb_w:.4f} ({'OK' if not is_squeeze else 'SQUEEZE — BLOCKED'})",
        "gate": (f"BLOCKED — {regime} (ADX={adx_val:.1f}, BBw={bb_w:.4f})"
                 if resultat["blocked"] else f"PASSED ({regime})"),
    }
    return resultat


def niveau_depuis_score(score, seuil):
    """Normalise un score brut en niveau [-2, +2].

    ±2 exige d'atteindre le seuil (ajusté par le régime) ; ±1 se contente de 4.
    Seul ±2 déclenche une entrée — ±1 sert à mesurer la conviction.
    """
    if score >= seuil:
        return 2
    if score >= 4:
        return 1
    if score <= -seuil:
        return -2
    if score <= -4:
        return -1
    return 0


def gate_1h(level, trend_1h):
    """Refuse un signal fort à contre-courant de la tendance horaire.

    Retourne (bloqué, texte de debug). Seuls les ±2 sont concernés : un signal
    faible ne déclenche rien, inutile de le bloquer.
    """
    if level == 2 and trend_1h == "bear":
        return True, "BLOCKED — 1h BEARISH vs signal BULLISH"
    if level == -2 and trend_1h == "bull":
        return True, "BLOCKED — 1h BULLISH vs signal BEARISH"
    return False, f"OK (trend_1h={trend_1h})"


def verdict_ml(confiance):
    """Traduit une confiance du modèle en décision.

    Retourne ("blocked" | "penalty" | "ok", texte de debug). La pénalité retire
    un point au score brut, ce qui impose une renormalisation par l'appelant.
    """
    if confiance < ML_BLOCK_THRESHOLD:
        return "blocked", f"BLOCKED — confidence={confiance:.3f} < {ML_BLOCK_THRESHOLD}"
    if confiance < ML_PENALTY_THRESHOLD:
        return "penalty", f"PENALTY — confidence={confiance:.3f} < {ML_PENALTY_THRESHOLD} (-1)"
    return "ok", f"OK — confidence={confiance:.3f}"
