"""Le contrôle de la sauvegarde de l'archive, éprouvé pour le casser.

Le 16/09/2026, homeserv02 — qui portait l'unique copie de l'archive Parquet —
est mort. Le watchdog a écrit « contrôle de la sauvegarde indisponible —
ignoré » 2 444 fois en neuf jours : un contrôle qui ne pouvait pas conclure
concluait que tout allait bien. Ces tests exigent l'inverse.
"""

import os

import pytest

import scripts.external_watchdog as w
from scripts.external_watchdog import check_sauvegarde, sauvegarde_perimee

MAINTENANT = 1_800_000_000_000
HEURE = 3600


def _marqueur(tmp_path, contenu):
    chemin = tmp_path / ".derniere_sauvegarde"
    chemin.write_text(contenu)
    return str(chemin)


# ── Le chemin nominal, pour que le reste ait un sens ─────────────────────────

def test_marqueur_recent_ne_declenche_rien(tmp_path):
    m = _marqueur(tmp_path, f"{MAINTENANT // 1000 - 6 * HEURE}\n")
    assert check_sauvegarde(MAINTENANT, m) == []


def test_marqueur_trop_ancien_alerte(tmp_path):
    m = _marqueur(tmp_path, str(MAINTENANT // 1000 - 72 * HEURE))
    p = check_sauvegarde(MAINTENANT, m)
    assert len(p) == 1 and "72 h" in p[0]


def test_frontiere_du_seuil():
    """Deux passages nocturnes manqués : au-delà, quelque chose ne va pas."""
    assert sauvegarde_perimee(MAINTENANT / 1000 - 48 * HEURE, MAINTENANT, 48) is None
    assert sauvegarde_perimee(MAINTENANT / 1000 - 49 * HEURE, MAINTENANT, 48) is not None


# ── Ce qui se taisait ────────────────────────────────────────────────────────

def test_marqueur_absent_alerte(tmp_path):
    """Le cas du 16/09 : la machine de sauvegarde n'écrit plus rien."""
    p = check_sauvegarde(MAINTENANT, str(tmp_path / "jamais-ecrit"))
    assert len(p) == 1 and "incontrôlable" in p[0]


@pytest.mark.parametrize("contenu", ["", "\n", "   ", "hier", "1.8e9", "12 34",
                                     "\x00\x00", "１７９０００００００"])
def test_marqueur_malforme_alerte(tmp_path, contenu):
    """Vide (écriture coupée par un disque plein), texte, flottant, octets
    nuls, chiffres pleine chasse : aucun ne doit passer pour une date."""
    p = check_sauvegarde(MAINTENANT, _marqueur(tmp_path, contenu))
    assert len(p) == 1 and "incontrôlable" in p[0], contenu


def test_marqueur_illisible_par_droits_alerte(tmp_path):
    """Le watchdog tourne sous v10watch : un marqueur réservé à root se lit
    comme une absence, et doit alerter pareil."""
    if os.geteuid() == 0:
        pytest.skip("root lit tout, l'épreuve n'a pas de sens")
    m = _marqueur(tmp_path, str(MAINTENANT // 1000))
    os.chmod(m, 0)
    try:
        assert "incontrôlable" in check_sauvegarde(MAINTENANT, m)[0]
    finally:
        os.chmod(m, 0o644)


def test_marqueur_qui_est_un_repertoire_alerte(tmp_path):
    assert "incontrôlable" in check_sauvegarde(MAINTENANT, str(tmp_path))[0]


def test_marqueur_dans_le_futur_alerte(tmp_path):
    """Une horloge fausse à la source rendrait le marqueur éternellement frais."""
    m = _marqueur(tmp_path, str(MAINTENANT // 1000 + 2 * HEURE))
    p = check_sauvegarde(MAINTENANT, m)
    assert len(p) == 1 and "futur" in p[0]


def test_petite_avance_d_horloge_toleree(tmp_path):
    """Deux horloges ne sont jamais exactement d'accord : une minute d'avance
    n'est pas une panne."""
    m = _marqueur(tmp_path, str(MAINTENANT // 1000 + 60))
    assert check_sauvegarde(MAINTENANT, m) == []


def test_marqueur_negatif_ou_nul_alerte(tmp_path):
    """Epoch 0 : un `date +%s` sur une horloge non réglée au démarrage."""
    for valeur in ("0", "-5"):
        p = check_sauvegarde(MAINTENANT, _marqueur(tmp_path, valeur))
        assert len(p) == 1, valeur


# ── L'anti-spam ne doit ni se taire ni hurler ────────────────────────────────

def test_le_message_d_echec_est_stable(tmp_path):
    """L'anti-spam compare les textes : un message qui changerait à chaque
    passage réalerterait toutes les cinq minutes."""
    absent = str(tmp_path / "absent")
    assert check_sauvegarde(MAINTENANT, absent) == check_sauvegarde(MAINTENANT + 300_000, absent)


def test_le_message_ne_porte_pas_le_chemin(tmp_path):
    """Le message part sur Telegram : il n'a pas à y promener l'arborescence."""
    p = check_sauvegarde(MAINTENANT, str(tmp_path / "absent"))
    assert str(tmp_path) not in p[0]


# ── Désactivation explicite ──────────────────────────────────────────────────

def test_sans_configuration_le_controle_est_desactive_et_le_dit(monkeypatch, capsys):
    monkeypatch.setattr(w, "BACKUP_MARKER", "")
    assert check_sauvegarde(MAINTENANT) == []
    assert "désactivé" in capsys.readouterr().out


def test_la_configuration_est_lue(monkeypatch, tmp_path):
    """Sans chemin explicite, c'est BACKUP_MARKER qui compte — et un marqueur
    absent y alerte comme ailleurs."""
    monkeypatch.setattr(w, "BACKUP_MARKER", str(tmp_path / "absent"))
    assert "incontrôlable" in check_sauvegarde(MAINTENANT)[0]
