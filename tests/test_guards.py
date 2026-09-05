"""Garde-fous d'entrée — première couverture de ces règles.

Extraits de main.py le 05/09/2026, ils n'étaient testés par rien : ni le filtre
de corrélation, ni le garde-fou d'exposition, ni les cooldowns. C'est pourtant
ce qui empêche le bot de concentrer son risque sur une seule direction.
"""

from config import (COOLDOWN_MAX_SEC, COOLDOWN_MIN_SEC, COOLDOWN_LOSS_MULT,
                    COOLDOWN_WIN_MULT, POSITION_SIZE_PCT, RESERVE_BALANCE_PCT)
from risk import guards

TRIO = ["BTC", "ETH", "SOL"]


def _pos(active=False, side=None, size=0.0, entry=0.0, pending=None):
    return {"active": active, "side": side, "size": size, "entry": entry,
            "pending_entry": pending}


def _positions(**etats):
    base = {c: _pos() for c in TRIO}
    base.update(etats)
    return base


# ── Filtre de corrélation ────────────────────────────────────────────────────

def test_correlation_bloque_une_paire_soeur_de_meme_direction():
    """Le cœur de la règle : deux longs simultanés concentrent le drawdown."""
    positions = _positions(ETH=_pos(active=True, side="buy"))
    bloque, boost = guards.correlation_filter("BTC", "buy", positions, {}, TRIO)
    assert bloque is True and boost == 0.0


def test_correlation_laisse_passer_la_direction_opposee():
    """Un short pendant qu'un long tourne n'aggrave pas la concentration."""
    positions = _positions(ETH=_pos(active=True, side="buy"))
    bloque, boost = guards.correlation_filter("BTC", "sell", positions, {}, TRIO)
    assert bloque is False and boost == 0.0


def test_correlation_boost_si_paire_soeur_confirmee_sans_position():
    positions = _positions()
    bloque, boost = guards.correlation_filter("BTC", "buy", positions, {"ETH": 2}, TRIO)
    assert bloque is False and boost == 0.15


def test_correlation_pas_de_boost_pour_un_signal_faible():
    positions = _positions()
    bloque, boost = guards.correlation_filter("BTC", "buy", positions, {"ETH": 1}, TRIO)
    assert (bloque, boost) == (False, 0.0)


def test_correlation_ignore_le_coin_lui_meme():
    """Sa propre position ne doit pas se bloquer elle-même."""
    positions = _positions(BTC=_pos(active=True, side="buy"))
    assert guards.correlation_filter("BTC", "buy", positions, {}, TRIO) == (False, 0.0)


# ── Notionnel ────────────────────────────────────────────────────────────────

def test_notional_applique_reserve_et_taille():
    attendu = 1000 * (1 - RESERVE_BALANCE_PCT) * POSITION_SIZE_PCT
    assert guards.notional(1000, 1.0) == attendu


def test_notional_borne_le_facteur_entre_0_3_et_1():
    assert guards.notional(1000, 5.0) == guards.notional(1000, 1.0)
    assert guards.notional(1000, 0.01) == guards.notional(1000, 0.3)


# ── Garde-fou d'exposition ───────────────────────────────────────────────────

def test_exposition_compte_les_entrees_en_attente():
    """Le point subtil : une entrée validée mais pas encore remplie engage déjà
    du capital. L'ignorer permettrait d'ouvrir au-delà du plafond."""
    positions = _positions(
        ETH=_pos(pending={"direction": "buy", "size_factor": 1.0}),
        SOL=_pos(pending={"direction": "buy", "size_factor": 1.0}),
    )
    autorise, raison = guards.exposure_guard("BTC", "buy", 1.0, 1000, positions, TRIO)
    assert autorise is False
    assert "positions" in raison.lower()


def test_exposition_autorise_quand_le_terrain_est_libre():
    autorise, _ = guards.exposure_guard("BTC", "buy", 1.0, 1000, _positions(), TRIO)
    assert autorise is True


def test_exposition_limite_par_direction():
    """MAX_POSITIONS_PER_DIR = 1 : un second long doit être refusé."""
    positions = _positions(ETH=_pos(active=True, side="buy", size=1, entry=10))
    autorise, _ = guards.exposure_guard("BTC", "buy", 1.0, 1000, positions, TRIO)
    assert autorise is False


# ── Cooldowns ────────────────────────────────────────────────────────────────

def test_cooldown_allonge_apres_une_perte():
    cooldowns = {"BTC": 600}
    assert guards.adjust_cooldown(cooldowns, "BTC", -1.0) == round(600 * COOLDOWN_LOSS_MULT)
    assert cooldowns["BTC"] == round(600 * COOLDOWN_LOSS_MULT)


def test_cooldown_raccourcit_apres_un_gain():
    cooldowns = {"BTC": 600}
    assert guards.adjust_cooldown(cooldowns, "BTC", 1.0) == round(600 * COOLDOWN_WIN_MULT)


def test_cooldown_plafonne_et_plancher():
    haut = {"BTC": COOLDOWN_MAX_SEC}
    assert guards.adjust_cooldown(haut, "BTC", -1.0) == COOLDOWN_MAX_SEC
    bas = {"BTC": COOLDOWN_MIN_SEC}
    assert guards.adjust_cooldown(bas, "BTC", 1.0) == COOLDOWN_MIN_SEC


def test_cooldown_pnl_nul_compte_comme_un_gain():
    """Frontière explicite : `pnl < 0` — zéro n'est pas une perte."""
    cooldowns = {"BTC": 600}
    assert guards.adjust_cooldown(cooldowns, "BTC", 0.0) < 600


# ── Circuit breaker marché ───────────────────────────────────────────────────

# Attention : ces métriques sont des FRACTIONS, pas des pourcentages.
# CB_MAX_SPREAD_PCT = 0.0005 vaut 0,05 % — malgré le suffixe _PCT du nom.
MARCHE_CALME = {"atr_pct": 0.005, "funding_rate": 0.00005,
                "candle_range_pct": 0.01, "spread_pct": 0.0002,
                "ob_depth_ratio": 1.0}


def test_market_breaker_laisse_passer_un_marche_calme():
    tripped, raisons = guards.market_breaker({"debug": dict(MARCHE_CALME)})
    assert tripped is False, raisons


def test_market_breaker_declenche_sur_spread_trop_large():
    metriques = dict(MARCHE_CALME, spread_pct=0.01)   # 1 %, vingt fois le seuil
    tripped, raisons = guards.market_breaker({"debug": metriques})
    assert tripped is True
    assert any("spread" in r for r in raisons)


def test_market_breaker_declenche_sur_liquidite_effondree():
    metriques = dict(MARCHE_CALME, ob_depth_ratio=0.05)
    tripped, raisons = guards.market_breaker({"debug": metriques})
    assert tripped is True
    assert any("liquidité" in r for r in raisons)


def test_market_breaker_ignore_une_metrique_absente():
    """Une métrique manquante ne doit pas bloquer : le carry-forward peut ne
    pas encore l'avoir renseignée au démarrage."""
    tripped, _ = guards.market_breaker({"debug": {}})
    assert tripped is False


def test_les_seuils_sont_des_fractions_pas_des_pourcentages():
    """Piège d'unité : le nom CB_MAX_SPREAD_PCT suggère un pourcentage, mais un
    spread de 1 (soit 100 %) doit déclencher, pas passer pour 1 %."""
    tripped, _ = guards.market_breaker({"debug": dict(MARCHE_CALME, spread_pct=1.0)})
    assert tripped is True
