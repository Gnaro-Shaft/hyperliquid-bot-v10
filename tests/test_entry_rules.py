"""Décisions de la logique d'entrée — première couverture.

Ces règles décident si le bot entre en position et à quel prix. Elles vivaient
dans _try_open_position (97 lignes), mêlées aux appels au risk manager, au
notifier et à l'exchange : rien ne les vérifiait.
"""

import pytest

from config import PULLBACK_PCT
from strategy import entry_rules as regles


# ── Force du signal ──────────────────────────────────────────────────────────

def test_seuls_les_signaux_extremes_ouvrent():
    assert regles.signal_est_fort(2) and regles.signal_est_fort(-2)
    for faible in (-1, 0, 1):
        assert not regles.signal_est_fort(faible)


def test_side_depuis_score():
    assert regles.side_depuis_score(2) == "buy"
    assert regles.side_depuis_score(-2) == "sell"


# ── Confirmation par série ───────────────────────────────────────────────────

def test_serie_s_allonge_dans_le_meme_sens():
    streak, direction = regles.maj_streak(2, 2, 1)
    assert (streak, direction) == (2, 2)


def test_changement_de_sens_repart_a_un_pas_a_zero():
    """Le signal qui change de camp compte déjà pour lui-même."""
    streak, direction = regles.maj_streak(-2, 2, 3)
    assert (streak, direction) == (1, -2)


def test_premier_signal_depuis_l_etat_neutre():
    streak, direction = regles.maj_streak(2, 0, 0)
    assert (streak, direction) == (1, 2)


def test_confirmation_au_seuil_exact():
    assert regles.est_confirme(3, 3) is True
    assert regles.est_confirme(2, 3) is False


# ── Cooldown ─────────────────────────────────────────────────────────────────

def test_cooldown_restant():
    assert regles.cooldown_restant(1000, 900, 600) == 500
    assert regles.cooldown_restant(2000, 900, 600) == 0.0


def test_cooldown_jamais_negatif():
    """Un cooldown largement écoulé ne doit pas produire de valeur négative."""
    assert regles.cooldown_restant(10_000, 0, 600) == 0.0


# ── Taille ───────────────────────────────────────────────────────────────────

def test_boost_de_correlation_plafonne_a_la_taille_pleine():
    assert regles.taille_avec_boost(0.5, 0.15) == pytest.approx(0.65)
    assert regles.taille_avec_boost(0.95, 0.15) == 1.0


# ── Pullback ─────────────────────────────────────────────────────────────────

def test_cible_pullback_sous_le_prix_en_achat():
    assert regles.cible_pullback("buy", 100, 0.01) < 100
    assert regles.cible_pullback("sell", 100, 0.01) > 100


def test_cible_pullback_arrondie_pour_les_petits_prix():
    """Coin-agnostique (V10) : kPEPE cote avec bien plus de décimales que BTC."""
    cible = regles.cible_pullback("buy", 0.0034567, PULLBACK_PCT)
    assert 0 < cible < 0.0034567
    assert len(repr(cible)) < 12          # pas de flottant à rallonge


def test_pullback_atteint_dans_les_deux_sens():
    assert regles.pullback_atteint("buy", 99.0, 99.5) is True
    assert regles.pullback_atteint("buy", 99.6, 99.5) is False
    assert regles.pullback_atteint("sell", 101.0, 100.5) is True
    assert regles.pullback_atteint("sell", 100.4, 100.5) is False


def test_egalite_vaut_pullback_atteint():
    assert regles.pullback_atteint("buy", 99.5, 99.5) is True
    assert regles.pullback_atteint("sell", 100.5, 100.5) is True


# ── Inversion ────────────────────────────────────────────────────────────────

def test_serie_opposee_s_allonge():
    assert regles.maj_reverse_streak(True, "buy", -2, 1) == 2
    assert regles.maj_reverse_streak(True, "sell", 2, 1) == 2


def test_signal_non_oppose_remet_la_serie_a_zero():
    """Seule une série ininterrompue justifie de fermer une position."""
    assert regles.maj_reverse_streak(True, "buy", 2, 5) == 0
    assert regles.maj_reverse_streak(True, "buy", 0, 5) == 0


def test_sans_position_ouverte_la_serie_est_nulle():
    assert regles.maj_reverse_streak(False, "buy", -2, 5) == 0


def test_inversion_confirmee_au_seuil():
    assert regles.inversion_confirmee(3, 3) is True
    assert regles.inversion_confirmee(2, 3) is False


# ── Paramètres de trailing ───────────────────────────────────────────────────

def test_parametres_trailing_suivent_la_volatilite():
    p = regles.parametres_trailing(0.02, 0.006, 0.012, 0.003)
    assert p["trail_distance"] == pytest.approx(0.03)   # 0.02 × 1.5
    assert p["trail_trigger"] == pytest.approx(0.04)    # 0.02 × 2.0
    assert p["trail_step"] == pytest.approx(0.01)       # 0.02 × 0.5


def test_planchers_appliques_sur_marche_calme():
    """Sans planchers, un ATR quasi nul ferait déclencher le stop sur du bruit."""
    p = regles.parametres_trailing(0.0001, 0.006, 0.012, 0.003)
    assert p["trail_distance"] == 0.006
    assert p["trail_trigger"] == 0.012
    assert p["trail_step"] == 0.003
