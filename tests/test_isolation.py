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
    """utils.mongo est désormais le seul point de création d'un client."""
    from utils import mongo
    with pytest.raises(RuntimeError, match="interdite pendant les tests"):
        mongo.MongoClient("mongodb://exemple.invalide:27017")


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


def test_un_seul_point_de_creation_de_client():
    """Hors scripts/ (process courts), seul utils/mongo.py crée un MongoClient.

    Garde anti-régression du correctif de fuite : réintroduire un
    `MongoClient(...)` ailleurs recréerait un pool de connexions par objet.
    """
    import pathlib
    racine = pathlib.Path(__file__).resolve().parent.parent
    fautifs = []
    for chemin in racine.rglob("*.py"):
        rel = chemin.relative_to(racine)
        if rel.parts[0] in {"venv", "tests", "scripts", ".git"}:
            continue
        if rel.as_posix() == "utils/mongo.py":
            continue
        if "MongoClient(" in chemin.read_text(encoding="utf-8"):
            fautifs.append(rel.as_posix())
    assert not fautifs, f"MongoClient créé hors utils/mongo.py : {fautifs}"
