"""La garde d'isolation Mongo est-elle bien active ?

Sans ce test, la fixture de conftest.py pourrait être désactivée ou contournée
sans que personne ne s'en aperçoive avant le prochain incident en production.
"""

import pytest


def test_mongoclient_direct_est_interdit():
    """Un import frais de pymongo ne doit pas permettre de se connecter."""
    import pymongo
    with pytest.raises(RuntimeError, match="interdite pendant les tests"):
        pymongo.MongoClient("mongodb://exemple.invalide:27017")


def test_mongoclient_est_interdit_dans_les_modules_du_projet():
    """Le module fautif du 05/09/2026 : TradeLogger ouvrait son propre client."""
    import datalog.trade_logger as tl
    with pytest.raises(RuntimeError, match="interdite pendant les tests"):
        tl.MongoClient("mongodb://exemple.invalide:27017")


def test_trade_logger_degrade_proprement_sans_mongo():
    """Sans base, TradeLogger doit rester inerte plutôt que d'échouer."""
    from datalog.trade_logger import TradeLogger
    logger = TradeLogger(collection="paper_trades")
    assert logger.ready is False
    assert logger.db is None


def test_module_importe_tardivement_est_couvert():
    """Un module chargé PENDANT la session, donc après la fixture, reste couvert.

    C'est la propriété non évidente : elle tient parce que le remplacement porte
    aussi sur `pymongo` lui-même, dont hérite tout `from pymongo import ...`
    exécuté ensuite. Retirer ce volet casserait la garde en silence.
    """
    import scripts.backfill_ohlc as tardif
    assert tardif.MongoClient.__name__ == "MongoClientInterdit"
