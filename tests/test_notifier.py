"""Canal Telegram — c'est le filet de sécurité, il doit survivre aux à-coups.

Le 22/08/2026, la collecte est morte sans que personne ne soit prévenu. Le
watchdog a depuis été remis en place ; encore faut-il que son message parte.
Avant ce correctif, un 429 de Telegram ou une coupure d'une seconde faisait
perdre l'alerte définitivement.
"""

import pytest
import requests

from utils import notifier as mod
from tests.test_http import FausseReponse, FausseSession


@pytest.fixture
def notifier(monkeypatch):
    """Notifier armé, sans réseau ni jeton réel."""
    monkeypatch.setattr(mod, "TELEGRAM_TOKEN", "jeton-factice")
    monkeypatch.setattr(mod, "TELEGRAM_CHAT_ID", "42")
    n = mod.Notifier()
    n.enabled = True
    return n


def _brancher(n, issues, monkeypatch):
    """Branche une session factice et neutralise l'attente entre tentatives.

    L'originale est capturée AVANT le remplacement : la capturer après ferait
    s'appeler le remplaçant lui-même.
    """
    originale = mod.http.demander

    def sans_attendre(session, methode, url, **kw):
        kw.setdefault("dormir", lambda _: None)
        return originale(session, methode, url, **kw)

    n._http = FausseSession(issues)
    monkeypatch.setattr(mod.http, "demander", sans_attendre)
    return n


def test_envoi_reussi_retourne_true(notifier, monkeypatch):
    _brancher(notifier, [FausseReponse(200)], monkeypatch)
    assert notifier.send("coucou") is True
    assert notifier._http.appels == 1


def test_reessaie_un_429_de_telegram(notifier, monkeypatch):
    """Telegram limite le débit : abandonner au premier 429 perd l'alerte."""
    _brancher(notifier, [FausseReponse(429), FausseReponse(200)], monkeypatch)
    assert notifier.send("coucou") is True
    assert notifier._http.appels == 2


def test_reessaie_une_coupure_reseau(notifier, monkeypatch):
    _brancher(notifier, [requests.exceptions.ConnectionError("coupure"),
                         FausseReponse(200)], monkeypatch)
    assert notifier.send("coucou") is True


def test_abandonne_sur_400_sans_reessayer(notifier, monkeypatch):
    """Un message mal formé ne passera pas mieux au troisième essai."""
    _brancher(notifier, [FausseReponse(400)], monkeypatch)
    assert notifier.send("<b>html casse") is False
    assert notifier._http.appels == 1


def test_echec_definitif_retourne_false(notifier, monkeypatch):
    _brancher(notifier, [requests.exceptions.ConnectionError("coupure")] * 3,
              monkeypatch)
    assert notifier.send("coucou") is False


def test_notifier_desactive_n_envoie_rien(monkeypatch):
    monkeypatch.setattr(mod, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(mod, "TELEGRAM_CHAT_ID", "")
    n = mod.Notifier()
    assert n.enabled is False
    assert n.send("coucou") is None
