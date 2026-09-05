"""Décisions de passage d'ordre — première couverture.

C'est le code qui enverra les ordres réels le jour où PAPER_MODE tombe. Il
n'était vérifié par rien : ni la taille, ni les prix TP/SL, ni la conversion de
symbole des contrats kilo.
"""

import pytest

from trader import order_rules as regles


# ── Symbole ──────────────────────────────────────────────────────────────────

def test_symbole_inchange_pour_un_contrat_normal():
    assert regles.symbole_ccxt("BTC/USDC:USDC") == "BTC/USDC:USDC"


def test_contrat_kilo_passe_en_majuscules():
    """kPEPE côté Hyperliquid, KPEPE côté marchés ccxt. Se tromper ici, c'est
    envoyer un ordre sur un symbole inexistant."""
    assert regles.symbole_ccxt("kPEPE/USDC:USDC") == "KPEPE/USDC:USDC"


def test_seul_le_prefixe_k_declenche_la_conversion():
    for pair in ("BTC/USDC:USDC", "SOL/USDC:USDC", "HYPE/USDC:USDC"):
        assert regles.symbole_ccxt(pair) == pair


# ── Taille ───────────────────────────────────────────────────────────────────

def test_taille_nominale_conservee():
    taille, note = regles.taille_ordre(base_size=1.0, prix=1000.0,
                                       size_factor=1.0, min_collateral=10)
    assert taille == 1.0 and note is None


def test_facteur_borne_entre_0_3_et_1():
    """Un facteur aberrant ne doit ni annuler l'ordre ni le démultiplier."""
    haut, _ = regles.taille_ordre(1.0, 1000.0, 5.0, 10)
    bas, _ = regles.taille_ordre(1.0, 1000.0, 0.01, 10)
    assert haut == 1.0
    assert bas == pytest.approx(0.3)


def test_solde_nul_refuse_l_ordre():
    taille, note = regles.taille_ordre(0.0, 1000.0, 1.0, 10)
    assert taille is None and "pas assez de solde" in note


def test_taille_remontee_au_minimum_si_le_solde_le_permet():
    """Cas fréquent : un facteur de réduction fait passer sous le minimum
    Hyperliquid alors que le solde couvre largement le minimum."""
    # base 0.02 @ 1000 = 20 USDC ; facteur 0.3 -> 6 USDC, sous les 10 requis
    taille, note = regles.taille_ordre(0.02, 1000.0, 0.3, 10)
    assert taille is not None
    assert taille * 1000.0 >= 10
    assert "remontée au minimum" in note


def test_refus_si_meme_la_taille_pleine_est_sous_le_minimum():
    """Mieux vaut renoncer que d'envoyer un ordre que l'exchange rejettera."""
    taille, note = regles.taille_ordre(0.005, 1000.0, 1.0, 10)   # 5 USDC max
    assert taille is None
    assert "solde insuffisant pour le minimum" in note


def test_la_marge_de_20_pourcent_est_appliquee():
    """min_target_size ajoute 20 % au minimum : un ordre pile au seuil serait
    rejeté au moindre mouvement de prix entre le calcul et l'exécution."""
    taille, _ = regles.taille_ordre(0.02, 1000.0, 0.3, 10)
    assert taille * 1000.0 == pytest.approx(12.0)


# ── Prix TP / SL ─────────────────────────────────────────────────────────────

def test_tp_au_dessus_et_sl_en_dessous_pour_un_achat():
    tp, sl, cloture = regles.prix_tp_sl("buy", 100.0, 0.02, 0.01, arrondir=lambda x: x)
    assert tp == pytest.approx(102.0)
    assert sl == pytest.approx(99.0)
    assert cloture == "sell"


def test_sens_inverse_pour_une_vente():
    tp, sl, cloture = regles.prix_tp_sl("sell", 100.0, 0.02, 0.01, arrondir=lambda x: x)
    assert tp == pytest.approx(98.0)
    assert sl == pytest.approx(101.0)
    assert cloture == "buy"


def test_le_tp_est_toujours_du_bon_cote_du_prix():
    """Inverser TP et SL transformerait chaque entrée en perte immédiate."""
    for side, sens in (("buy", 1), ("sell", -1)):
        tp, sl, _ = regles.prix_tp_sl(side, 100.0, 0.02, 0.01, arrondir=lambda x: x)
        assert (tp - 100.0) * sens > 0, f"TP du mauvais côté en {side}"
        assert (sl - 100.0) * sens < 0, f"SL du mauvais côté en {side}"


def test_l_arrondi_injecte_est_bien_utilise():
    tp, sl, _ = regles.prix_tp_sl("buy", 100.0, 0.02, 0.01, arrondir=lambda x: round(x))
    assert tp == 102 and sl == 99


def test_arrondi_par_defaut_sur_un_prix_minuscule():
    """kPEPE cote autour de 0.0035 : un arrondi à 2 décimales donnerait 0."""
    tp, sl, _ = regles.prix_tp_sl("buy", 0.0034567, 0.02, 0.01)
    assert tp > 0.0034567 > sl > 0


# ── Identification des ordres protecteurs ────────────────────────────────────

@pytest.mark.parametrize("ordre,stop,tp", [
    ({"type": "stop_market", "reduceOnly": True}, True, False),
    ({"type": "take_profit", "reduceOnly": True}, False, True),
    ({"type": "limit", "reduceOnly": True}, True, True),   # ambigu : les deux heuristiques matchent
    ({"type": "limit", "reduceOnly": False}, False, False),
    ({}, False, False),
])
def test_classification_des_ordres_ouverts(ordre, stop, tp):
    """Repli quand l'ID de l'ordre a été perdu. Le cas ambigu est documenté :
    un ordre reduceOnly sans type explicite matche les deux critères — le code
    n'annule alors que celui qu'il cherche, jamais les deux à la fois."""
    assert regles.est_ordre_stop(ordre) is stop
    assert regles.est_ordre_take_profit(ordre) is tp


# ── Montant arrondi à la précision de l'exchange ─────────────────────────────

class FauxExchangePrecision:
    def __init__(self, decimales=2, precision=2):
        self.decimales, self.precision = decimales, precision

    def amount_to_precision(self, symbole, montant):
        return f"{float(montant):.{self.decimales}f}"

    def market(self, symbole):
        return {"precision": {"amount": self.precision}}


def test_montant_arrondi_a_la_precision():
    ex = FauxExchangePrecision(decimales=2)
    assert regles.montant_sur_exchange(ex, "BTC/USDC:USDC", 1.23456, 100.0, 10) == pytest.approx(1.23)


def test_remontee_d_un_cran_si_l_arrondi_passe_sous_le_minimum():
    """L'arrondi vers le bas peut faire chuter le notionnel sous le minimum
    exigé : on remonte d'un cran plutôt que d'envoyer un ordre rejeté."""
    ex = FauxExchangePrecision(decimales=2, precision=2)
    # 0.109 -> 0.10 à 100 USDC = 10.0... juste au minimum de 11 : doit remonter
    montant = regles.montant_sur_exchange(ex, "BTC/USDC:USDC", 0.109, 100.0, 11)
    assert montant * 100.0 >= 11


def test_repli_sur_six_decimales_si_l_exchange_ne_repond_pas():
    class Muet:
        def amount_to_precision(self, *a):
            raise RuntimeError("marché non chargé")

    assert regles.montant_sur_exchange(Muet(), "X", 1.23456789, 100.0, 10) == pytest.approx(1.234568)
