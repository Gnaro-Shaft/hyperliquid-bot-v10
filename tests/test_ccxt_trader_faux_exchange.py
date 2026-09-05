"""Passage d'ordre vérifié contre un faux exchange.

Ce code enverra les ordres réels le jour où PAPER_MODE tombe, et rien ne
vérifiait qu'il les compose correctement : ni le symbole, ni le sens, ni les
prix TP/SL, ni le fait qu'aucun ordre ne parte quand le solde est insuffisant.

LIMITE À CONNAÎTRE : ce faux exchange prouve que le code appelle ccxt
conformément à la lecture qu'on fait de son API — pas qu'Hyperliquid accepte
ces ordres. C'est une vérification de cohérence interne, pas d'intégration.
"""

import pytest

from trader.ccxt_trader import HyperliquidTrader


class FauxExchange:
    """Enregistre les appels au lieu de les émettre."""

    def __init__(self, echec_ordre_principal=False):
        self.ordres = []
        self.annulations = []
        self.echec_ordre_principal = echec_ordre_principal
        self._n = 0

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        if self.echec_ordre_principal and not self.ordres:
            raise RuntimeError("exchange indisponible")
        self._n += 1
        self.ordres.append({"symbol": symbol, "type": type, "side": side,
                            "amount": amount, "price": price,
                            "params": params or {}})
        return {"id": f"ord{self._n}"}

    def cancel_order(self, order_id, symbol=None):
        self.annulations.append(order_id)
        return {"id": order_id}

    def fetch_open_orders(self, symbol=None):
        return []

    def price_to_precision(self, symbol, prix):
        return f"{float(prix):.5f}"

    def amount_to_precision(self, symbol, montant):
        return f"{float(montant):.6f}"

    def market(self, symbol):
        return {"precision": {"amount": 6}}


class FauxLogger:
    def __init__(self):
        self.trades = []

    def log_trade(self, trade_info, context=None):
        self.trades.append((trade_info, context))


class FauxNotifier:
    def __init__(self):
        self.erreurs = []

    def error(self, msg):
        self.erreurs.append(msg)

    def trade_opened(self, *a, **k):
        pass


def _trader(pair="BTC/USDC:USDC", taille_base=1.0, echec=False):
    """HyperliquidTrader sans réseau : __init__ ouvrirait une connexion ccxt."""
    t = HyperliquidTrader.__new__(HyperliquidTrader)
    t.exchange = FauxExchange(echec_ordre_principal=echec)
    t.logger = FauxLogger()
    t.notifier = FauxNotifier()
    t.pair = pair
    t.get_position_size = lambda prix: taille_base
    return t


# ── Composition de l'ordre ───────────────────────────────────────────────────

def test_un_achat_envoie_trois_ordres_dans_le_bon_ordre():
    t = _trader()
    res = t.place_order_with_tp_sl("buy", 100.0, tp_pct=0.02, sl_pct=0.01)
    assert res is not None
    assert len(t.exchange.ordres) == 3, "principal + TP + SL attendus"
    principal, tp, sl = t.exchange.ordres
    assert principal["side"] == "buy"
    assert tp["side"] == "sell" and sl["side"] == "sell", "TP et SL ferment la position"


def test_les_prix_tp_sl_encadrent_le_prix_d_entree_en_achat():
    t = _trader()
    res = t.place_order_with_tp_sl("buy", 100.0, tp_pct=0.02, sl_pct=0.01)
    assert res["tp_price"] == pytest.approx(102.0)
    assert res["sl_price"] == pytest.approx(99.0)


def test_une_vente_inverse_tout():
    t = _trader()
    res = t.place_order_with_tp_sl("sell", 100.0, tp_pct=0.02, sl_pct=0.01)
    assert res["tp_price"] == pytest.approx(98.0)
    assert res["sl_price"] == pytest.approx(101.0)
    assert all(o["side"] == "buy" for o in t.exchange.ordres[1:])


def test_le_symbole_kilo_est_converti_pour_l_exchange():
    """kPEPE côté Hyperliquid, KPEPE côté ccxt : se tromper, c'est envoyer
    l'ordre sur un symbole inexistant."""
    t = _trader(pair="kPEPE/USDC:USDC", taille_base=100000.0)
    t.place_order_with_tp_sl("buy", 0.0035, tp_pct=0.02, sl_pct=0.01)
    assert all(o["symbol"] == "KPEPE/USDC:USDC" for o in t.exchange.ordres)


def test_le_slippage_maximal_est_transmis():
    t = _trader()
    t.place_order_with_tp_sl("buy", 100.0)
    assert t.exchange.ordres[0]["params"].get("maxSlippagePcnt") == 0.01


# ── Refus : aucun ordre ne doit partir ───────────────────────────────────────

def test_aucun_ordre_si_le_solde_ne_couvre_pas_le_minimum():
    """Le contrôle le plus important : ne rien envoyer plutôt qu'un ordre
    qui sera rejeté par l'exchange."""
    t = _trader(taille_base=0.005)          # 0.5 USDC de notionnel à 100
    res = t.place_order_with_tp_sl("buy", 100.0)
    assert res is None
    assert t.exchange.ordres == [], "aucun ordre ne devait partir"


def test_aucun_ordre_sans_paire_selectionnee():
    t = _trader()
    t.pair = None
    assert t.place_order_with_tp_sl("buy", 100.0) is None
    assert t.exchange.ordres == []


def test_echec_de_l_ordre_principal_n_envoie_ni_tp_ni_sl():
    """Poser un TP/SL sur une position inexistante laisserait des ordres
    orphelins qui se déclencheraient plus tard."""
    t = _trader(echec=True)
    res = t.place_order_with_tp_sl("buy", 100.0)
    assert res is None
    assert t.exchange.ordres == []
    assert t.notifier.erreurs, "l'échec doit être notifié"


# ── Remplacement des ordres protecteurs (TP / SL) ────────────────────────────

class FauxExchangeAvecPosition(FauxExchange):
    """Faux exchange portant une position ouverte et des ordres protecteurs."""

    def __init__(self, ordres_ouverts=None, side="long"):
        super().__init__()
        self._ouverts = ordres_ouverts or []
        self._side = side

    def fetch_open_orders(self, symbol=None):
        return self._ouverts

    def fetch_positions(self, symbols=None):
        return [{"symbol": "BTC/USDC:USDC", "contracts": 2.0,
                 "side": self._side, "entryPrice": 100.0}]


def _trader_avec_position(ordres_ouverts=None, side="long"):
    t = _trader()
    t.exchange = FauxExchangeAvecPosition(ordres_ouverts, side)
    return t


def test_update_sl_annule_par_id_puis_replace():
    t = _trader_avec_position()
    ordre = t.update_sl(95.0, old_sl_order_id="ancien-sl")
    assert t.exchange.annulations == ["ancien-sl"]
    assert ordre is not None
    envoye = t.exchange.ordres[-1]
    assert envoye["params"].get("stopLossPrice") == pytest.approx(95.0)
    assert envoye["params"].get("reduceOnly") is True
    assert envoye["side"] == "sell", "on ferme un long en vendant"


def test_update_tp_annule_par_id_puis_replace():
    t = _trader_avec_position()
    ordre = t.update_tp(110.0, old_tp_order_id="ancien-tp")
    assert t.exchange.annulations == ["ancien-tp"]
    envoye = t.exchange.ordres[-1]
    assert envoye["type"] == "limit", "un TP est un ordre limite"
    assert "stopLossPrice" not in envoye["params"]
    assert envoye["price"] == pytest.approx(110.0)


def test_le_sens_de_cloture_suit_le_sens_de_la_position():
    t = _trader_avec_position(side="short")
    t.update_sl(105.0, old_sl_order_id="x")
    assert t.exchange.ordres[-1]["side"] == "buy", "on ferme un short en achetant"


def test_sans_id_le_sl_annule_les_ordres_stop_existants():
    """Repli quand l'ID a été perdu : on cible par type, sans toucher au TP."""
    ouverts = [{"id": "o-stop", "type": "stop_market", "reduceOnly": True},
               {"id": "o-tp", "type": "take_profit", "reduceOnly": True}]
    t = _trader_avec_position(ouverts)
    t.update_sl(95.0)
    assert "o-stop" in t.exchange.annulations
    assert "o-tp" not in t.exchange.annulations, "le TP ne doit pas être annulé"


def test_sans_id_le_tp_annule_les_ordres_take_profit_existants():
    ouverts = [{"id": "o-stop", "type": "stop_market", "reduceOnly": True},
               {"id": "o-tp", "type": "take_profit", "reduceOnly": True}]
    t = _trader_avec_position(ouverts)
    t.update_tp(110.0)
    assert "o-tp" in t.exchange.annulations
    assert "o-stop" not in t.exchange.annulations, "le SL ne doit pas être annulé"


def test_un_id_deja_annule_ne_bloque_pas_le_remplacement():
    """L'ancien ordre a pu être exécuté entre-temps : ce n'est pas une erreur."""
    t = _trader_avec_position()

    def refuse(order_id, symbol=None):
        raise RuntimeError("order not found")

    t.exchange.cancel_order = refuse
    assert t.update_sl(95.0, old_sl_order_id="disparu") is not None


def test_pas_de_paire_pas_de_mise_a_jour():
    t = _trader_avec_position()
    t.pair = None
    assert t.update_sl(95.0) is None
    assert t.update_tp(110.0) is None
