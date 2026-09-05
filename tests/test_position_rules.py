"""Décisions de gestion de position — première couverture du trailing stop.

Le README décrit le trailing comme « la seule sortie performante de v8 ». Il
n'était vérifié par rien : il vivait dans une méthode de 118 lignes mêlant
calculs, appels à l'exchange et affichage.

Chaque règle est éprouvée sur les deux sens — un bug qui ne toucherait que les
positions vendeuses passerait inaperçu jusqu'au premier short perdant.
"""

import pytest

from trader import position_rules as regles


# ── Gain relatif ─────────────────────────────────────────────────────────────

def test_gain_positif_quand_la_position_est_dans_le_vert():
    assert regles.gain_pct(100, 110, "buy") == pytest.approx(0.10)
    assert regles.gain_pct(100, 90, "sell") == pytest.approx(0.10)


def test_gain_negatif_quand_elle_est_dans_le_rouge():
    assert regles.gain_pct(100, 90, "buy") == pytest.approx(-0.10)
    assert regles.gain_pct(100, 110, "sell") == pytest.approx(-0.10)


def test_gain_nul_si_entry_manquant():
    """Garde-fou : une position sans prix d'entrée ne doit rien déclencher."""
    assert regles.gain_pct(0, 110, "buy") == 0.0
    assert regles.gain_pct(None, 110, "buy") == 0.0


# ── Nouveau sommet ───────────────────────────────────────────────────────────

def test_nouveau_sommet_dans_les_deux_sens():
    assert regles.est_nouveau_sommet("buy", 110, 105, 100) is True
    assert regles.est_nouveau_sommet("buy", 104, 105, 100) is False
    assert regles.est_nouveau_sommet("sell", 90, 95, 100) is True
    assert regles.est_nouveau_sommet("sell", 96, 95, 100) is False


def test_sommet_retombe_sur_l_entree_si_best_price_absent():
    """Position fraîche : best_price vaut None avant le premier cycle."""
    assert regles.est_nouveau_sommet("buy", 101, None, 100) is True
    assert regles.est_nouveau_sommet("buy", 99, None, 100) is False


def test_egalite_stricte_n_est_pas_un_sommet():
    assert regles.est_nouveau_sommet("buy", 105, 105, 100) is False


# ── Ratchet du take-profit ───────────────────────────────────────────────────

def test_tp_suit_le_prix_a_la_moitie_de_sa_distance():
    assert regles.tp_ratchet("buy", 110, 0.04, 0) == pytest.approx(110 * 1.02)
    assert regles.tp_ratchet("sell", 90, 0.04, 100) == pytest.approx(90 * 0.98)


def test_tp_ne_recule_jamais():
    assert regles.tp_ratchet("buy", 110, 0.04, 200) is None
    assert regles.tp_ratchet("sell", 90, 0.04, 50) is None


def test_pas_de_ratchet_sans_distance_initiale():
    assert regles.tp_ratchet("buy", 110, 0, 0) is None
    assert regles.tp_ratchet("buy", 110, None, 0) is None


def test_asymetrie_conservee_de_v8_sur_le_tp_vendeur():
    """Comportement d'origine, volontairement non corrigé : une position
    vendeuse dont le TP vaut 0 ne voit jamais son TP bouger, puisque le
    nouveau TP calculé est toujours positif donc jamais < 0."""
    assert regles.tp_ratchet("sell", 90, 0.04, 0) is None
    # Avec un TP absent, la référence devient l'infini et le ratchet s'applique
    assert regles.tp_ratchet("sell", 90, 0.04, None) == pytest.approx(90 * 0.98)


# ── Breakeven ────────────────────────────────────────────────────────────────

def test_breakeven_decale_du_bon_cote():
    assert regles.niveau_breakeven("buy", 100, 0.002) > 100
    assert regles.niveau_breakeven("sell", 100, 0.002) < 100


def test_breakeven_ne_recule_pas_le_stop():
    assert regles.breakeven_ameliore("buy", 100.2, 99.0) is True
    assert regles.breakeven_ameliore("buy", 98.0, 99.0) is False
    assert regles.breakeven_ameliore("sell", 99.8, 101.0) is True
    assert regles.breakeven_ameliore("sell", 102.0, 101.0) is False


# ── Stop suiveur ─────────────────────────────────────────────────────────────

def test_niveau_trailing_se_place_sous_le_prix_en_achat():
    assert regles.niveau_trailing("buy", 100, 0.01) == pytest.approx(99.0)
    assert regles.niveau_trailing("sell", 100, 0.01) == pytest.approx(101.0)


def test_trailing_ne_bouge_que_au_dela_du_pas():
    """Sans ce pas, le stop suivrait chaque tick — bruit et appels inutiles."""
    # pas = entry × step = 100 × 0.005 = 0.5
    assert regles.trailing_doit_monter("buy", 99.6, 99.0, 100, 0.005) is True
    assert regles.trailing_doit_monter("buy", 99.4, 99.0, 100, 0.005) is False
    assert regles.trailing_doit_monter("sell", 100.4, 101.0, 100, 0.005) is True
    assert regles.trailing_doit_monter("sell", 100.6, 101.0, 100, 0.005) is False


def test_trailing_touche_dans_les_deux_sens():
    assert regles.trailing_touche("buy", 99.0, 99.0) is True     # égalité = touché
    assert regles.trailing_touche("buy", 99.1, 99.0) is False
    assert regles.trailing_touche("sell", 101.0, 101.0) is True
    assert regles.trailing_touche("sell", 100.9, 101.0) is False


def test_trailing_inactif_ne_declenche_rien():
    assert regles.trailing_touche("buy", 1.0, None) is False


# ── Franchissement TP / SL ───────────────────────────────────────────────────

def test_tp_sl_franchi_en_achat():
    assert regles.tp_sl_franchi("buy", 110, 105, 95) == (True, False)
    assert regles.tp_sl_franchi("buy", 94, 105, 95) == (False, True)
    assert regles.tp_sl_franchi("buy", 100, 105, 95) == (False, False)


def test_tp_sl_franchi_en_vente():
    assert regles.tp_sl_franchi("sell", 90, 95, 105) == (True, False)
    assert regles.tp_sl_franchi("sell", 106, 95, 105) == (False, True)
    assert regles.tp_sl_franchi("sell", 100, 95, 105) == (False, False)


def test_un_niveau_a_zero_signifie_non_place():
    """Sans cette règle, toute position déclencherait un SL au premier cycle."""
    assert regles.tp_sl_franchi("buy", 110, 0, 0) == (False, False)


def test_side_inconnu_ne_declenche_rien():
    assert regles.tp_sl_franchi(None, 110, 105, 95) == (False, False)


# ── PnL ──────────────────────────────────────────────────────────────────────

def test_pnl_dans_les_deux_sens():
    assert regles.pnl_realise("buy", 100, 110, 2) == pytest.approx(20)
    assert regles.pnl_realise("sell", 100, 90, 2) == pytest.approx(20)
    assert regles.pnl_realise("buy", 100, 90, 2) == pytest.approx(-20)


# ── Durée maximale de détention ──────────────────────────────────────────────

def test_detention_expiree_quand_la_duree_est_depassee():
    assert regles.detention_expiree(1000.0, 1000.0 + 7 * 86400, 7 * 86400)
    assert regles.detention_expiree(1000.0, 1000.0 + 8 * 86400, 7 * 86400)


def test_detention_non_expiree_avant_l_echeance():
    assert not regles.detention_expiree(1000.0, 1000.0 + 6 * 86400, 7 * 86400)


def test_detention_sans_open_time_ne_ferme_rien():
    """Position reprise après redémarrage : fermer sur une date inventée serait
    pire que laisser courir."""
    assert not regles.detention_expiree(None, 1e9, 7 * 86400)
    assert not regles.detention_expiree(0, 1e9, 7 * 86400)


def test_plafond_desactive_par_une_duree_nulle():
    assert not regles.detention_expiree(1000.0, 1e9, 0)
    assert not regles.detention_expiree(1000.0, 1e9, None)
