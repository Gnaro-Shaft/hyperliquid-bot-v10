"""Surveillance de l'expiration des API wallets Hyperliquid.

Un agent expiré ne peut plus signer d'ordre. Le bot continue de collecter,
d'évaluer et de battre ses heartbeats — mais ses entrées sont rejetées par
l'exchange. C'est une panne parfaitement silencieuse pour tout ce que le
watchdog surveillait jusqu'ici, d'où ce contrôle.
"""

import pytest

from scripts.external_watchdog import agents_expirants, check_agents

JOUR_MS = 86_400_000
MAINTENANT = 1_800_000_000_000


def _agent(nom, dans_jours):
    return {"name": nom, "address": "0xabc", "validUntil": MAINTENANT + int(dans_jours * JOUR_MS)}


def test_agent_lointain_ne_declenche_rien():
    assert agents_expirants([_agent("V10", 120)], MAINTENANT, 30) == []


def test_agent_proche_de_l_expiration_alerte():
    p = agents_expirants([_agent("V10", 14)], MAINTENANT, 30)
    assert len(p) == 1
    assert "V10" in p[0] and "14 j" in p[0]


def test_agent_deja_expire_est_signale_comme_tel():
    p = agents_expirants([_agent("HL_API", -5)], MAINTENANT, 30)
    assert "EXPIRÉ" in p[0]


def test_frontiere_du_seuil():
    """Au seuil exact, on alerte : mieux vaut prévenir un jour trop tôt."""
    assert agents_expirants([_agent("X", 30)], MAINTENANT, 30) != []
    assert agents_expirants([_agent("X", 31)], MAINTENANT, 30) == []


def test_plusieurs_agents_sont_tous_evalues():
    p = agents_expirants([_agent("V10", 120), _agent("HL_API", 10)], MAINTENANT, 30)
    assert len(p) == 1 and "HL_API" in p[0]


def test_agent_sans_date_est_ignore():
    """Un champ absent ne doit pas faire échouer le contrôle entier."""
    assert agents_expirants([{"name": "X"}], MAINTENANT, 30) == []


def test_liste_vide_ou_absente():
    assert agents_expirants([], MAINTENANT) == []
    assert agents_expirants(None, MAINTENANT) == []


def test_une_panne_reseau_ne_reveille_personne(monkeypatch):
    """Le watchdog surveille la collecte, pas la disponibilité d'Hyperliquid :
    un endpoint injoignable ne doit produire aucune alerte."""
    import scripts.external_watchdog as w

    def refuse(*a, **k):
        raise RuntimeError("endpoint injoignable")

    monkeypatch.setattr(w.http, "demander", refuse)
    assert check_agents(MAINTENANT) == []


# ── Anti-spam sur le contenu ─────────────────────────────────────────────────

def _nouveaux(problemes, deja_connus):
    """Reproduit la décision d'alerte du watchdog (voir main())."""
    return [p for p in problemes if p not in set(deja_connus)]


def test_probleme_identique_ne_realerte_pas():
    connus = ["ws_candles[BTC] muet depuis 400s"]
    assert _nouveaux(connus, connus) == []


def test_un_probleme_durable_n_etouffe_pas_une_panne_nouvelle():
    """Le cas qui motive ce changement : un agent expirant dans 14 jours
    maintiendrait l'état « unhealthy » pendant deux semaines. Avec un anti-spam
    booléen, une panne de collecte survenant entre-temps serait muette."""
    connus = ["agent Hyperliquid « HL_API » expire dans 14 j"]
    maintenant = connus + ["ws_candles[BTC] muet depuis 400s"]
    nouveaux = _nouveaux(maintenant, connus)
    assert nouveaux == ["ws_candles[BTC] muet depuis 400s"]


def test_premiere_detection_alerte():
    assert _nouveaux(["quelque chose"], []) == ["quelque chose"]


# ── Fraîcheur de la sauvegarde ───────────────────────────────────────────────

from scripts.external_watchdog import sauvegarde_perimee, check_sauvegarde

HEURE = 3600


def test_sauvegarde_recente_ne_declenche_rien():
    assert sauvegarde_perimee(MAINTENANT/1000 - 6*HEURE, MAINTENANT, 48) is None


def test_sauvegarde_trop_ancienne_alerte():
    p = sauvegarde_perimee(MAINTENANT/1000 - 72*HEURE, MAINTENANT, 48)
    assert p is not None and "72 h" in p


def test_frontiere_du_seuil_de_sauvegarde():
    """Deux passages nocturnes manqués : au-delà, quelque chose ne va pas."""
    assert sauvegarde_perimee(MAINTENANT/1000 - 48*HEURE, MAINTENANT, 48) is None
    assert sauvegarde_perimee(MAINTENANT/1000 - 49*HEURE, MAINTENANT, 48) is not None


def test_marqueur_absent_est_ignore():
    """Pas de marqueur : on ne sait pas, donc on n'alarme pas à tort."""
    assert sauvegarde_perimee(None, MAINTENANT, 48) is None


def test_controle_desactive_sans_configuration(monkeypatch):
    import scripts.external_watchdog as w
    monkeypatch.setattr(w, "BACKUP_CHECK_HOST", "")
    assert check_sauvegarde(MAINTENANT) == []


def test_panne_de_liaison_ne_reveille_personne(monkeypatch):
    """Une liaison SSH coupée n'est pas une panne de collecte."""
    import scripts.external_watchdog as w
    monkeypatch.setattr(w, "BACKUP_CHECK_HOST", "hote")
    monkeypatch.setattr(w, "BACKUP_CHECK_KEY", "/cle")
    monkeypatch.setattr(w.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("injoignable")))
    assert check_sauvegarde(MAINTENANT) == []
