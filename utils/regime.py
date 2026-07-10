"""
Adaptation par régime de marché (Phase 4 / v8.12) — pur et testable.

Détecte le régime à partir d'ADX, BB width et ATR%, puis fournit un PRESET :
multiplicateurs TP/SL/taille + ajustement de seuil. Étend la détection v8.4
(STRONG/WEAK/RANGE) avec SQUEEZE et HIGH_VOL.

Précurseur du v9.0 (régime ML / HMM) : ici les règles sont explicites.
"""

# Multiplicateurs par régime appliqués aux TP/SL/taille + ajustement seuil ±2.
#
# size_mult NEUTRALISÉ (1.0) le 28/06/2026 — l'analyse Axe A a montré que le
# down-sizing du régime WEAK était contre-productif : WEAK a un meilleur win rate
# ET un meilleur PnL/trade que STRONG (qui concentre les pertes). On garde la
# détection de régime + threshold_adj (WEAK plus sélectif = sain) et les TP/SL
# mults, mais on ne biaise plus la TAILLE tant que ce n'est pas re-validé avec le
# filtre ML actif. (Re-tuner via ml/analyze_ml_filter / backtest segmenté par régime.)
REGIME_PRESETS = {
    # Tendance forte → laisser courir : TP plus large
    "STRONG":   {"tp_mult": 1.25, "sl_mult": 1.00, "size_mult": 1.0, "threshold_adj": 0, "blocked": False},
    # Tendance faible → plus sélectif (seuil +1), SL plus serré
    "WEAK":     {"tp_mult": 1.00, "sl_mult": 0.90, "size_mult": 1.0, "threshold_adj": 1, "blocked": False},
    # Forte volatilité → SL plus large (éviter les stops sur bruit)
    "HIGH_VOL": {"tp_mult": 1.10, "sl_mult": 1.20, "size_mult": 1.0, "threshold_adj": 1, "blocked": False},
    # Range / chop → bloqué (comportement v8.4)
    "RANGE":    {"tp_mult": 1.00, "sl_mult": 1.00, "size_mult": 0.0, "threshold_adj": 0, "blocked": True},
    # Squeeze (volatilité écrasée) → bloqué (comportement v8.4)
    "SQUEEZE":  {"tp_mult": 1.00, "sl_mult": 1.00, "size_mult": 0.0, "threshold_adj": 0, "blocked": True},
}


def detect_regime(adx, bb_width, atr_pct,
                  squeeze_thr=0.004, adx_range=25, adx_strong=30, high_vol_atr=0.015):
    """Retourne le label de régime. Ordre : squeeze → range → high_vol → strong/weak.

    Préserve le blocage v8.4 (RANGE si ADX<25, SQUEEZE si BB width trop faible).
    """
    if bb_width < squeeze_thr:
        return "SQUEEZE"
    if adx < adx_range:
        return "RANGE"
    if atr_pct is not None and atr_pct >= high_vol_atr:
        return "HIGH_VOL"
    if adx >= adx_strong:
        return "STRONG"
    return "WEAK"


def regime_preset(adx, bb_width, atr_pct, **kw):
    """Retourne le preset du régime détecté (copie + champ 'regime')."""
    regime = detect_regime(adx, bb_width, atr_pct, **kw)
    preset = dict(REGIME_PRESETS[regime])
    preset["regime"] = regime
    return preset
