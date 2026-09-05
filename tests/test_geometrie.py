"""Invariants de la géométrie SL/TP/trailing.

Les 271 tests existants ont tous continué à passer quand la géométrie a été
multipliée par 5 le 05/09/2026 : ils importent les constantes au lieu de coder
les valeurs en dur. C'est un bon réflexe, mais il a une conséquence — aucun
test n'aurait attrapé une erreur d'échelle.

Ce fichier comble ce trou. Il ne vérifie pas des valeurs, il vérifie les
RAISONS de ces valeurs : le handicap de frais, le risque par trade, et le
rapport qui préserve l'adaptation à la volatilité. Une échelle changée à moitié
— les distances mises à jour mais pas le multiplicateur ATR, ou pas la taille
de position — casse ici.

Références : docs/falsification-2026-09-05.md, docs/protocole-pre-enregistre.md
"""

import pytest

import config
from utils.sizing import dynamic_sl_tp


FRAIS_ALLER_RETOUR = 0.0009      # 0,045 % par leg, taker Hyperliquid


def _barriere_adverse():
    """Stop effectif : le plancher, qui sature pour ~73 % des setups."""
    return config.SL_PCT * 0.5


def _barriere_favorable():
    """Le trailing s'arme ici — c'est la barrière favorable du test."""
    return config.TRAILING_TRIGGER_PCT


# ── Le handicap de frais : la raison d'être de l'échelle ──────────────────────

def test_le_handicap_de_frais_reste_sous_la_resolution_de_mesure():
    """Combien de points au-dessus du hasard il faut, juste pour les frais.

    L'ancienne géométrie imposait 5,42 points alors que 1 074 setups n'en
    détectent que ~3,5 : le handicap dépassait la résolution de la mesure, donc
    aucun edge ne pouvait être ni démontré ni encaissé. Le seuil de 1,5 point
    laisse la marge nécessaire.
    """
    fav, adv = _barriere_favorable(), _barriere_adverse()
    handicap = (FRAIS_ALLER_RETOUR / (fav + adv)) * 100
    assert handicap <= 1.5, (
        f"handicap de frais {handicap:.2f} pts — barrières trop serrées pour "
        f"qu'un edge mesurable soit rentable")


def test_le_seuil_de_rentabilite_depasse_le_hasard_du_handicap():
    """Cohérence interne des deux étalons du protocole."""
    fav, adv = _barriere_favorable(), _barriere_adverse()
    hasard = adv / (fav + adv)
    rentable = (adv + FRAIS_ALLER_RETOUR) / (fav + adv)
    assert rentable > hasard
    assert (rentable - hasard) * 100 <= 1.5


# ── Le risque par trade : ce que l'échelle ne doit PAS changer ────────────────

def test_le_risque_par_trade_reste_celui_d_avant_l_echelle():
    """`vol_factor` vaut SL_PCT / dynamic_sl : c'est un RAPPORT.

    Multiplier les deux termes par 5 le laisse inchangé — la taille de position
    ne s'ajuste donc pas toute seule. Sans compensation explicite de
    POSITION_SIZE_PCT, le risque par trade serait passé de 0,144 % à 0,72 % du
    solde en silence. C'est exactement ce que ce test empêche.
    """
    notionnel = (1 - config.RESERVE_BALANCE_PCT) * config.POSITION_SIZE_PCT
    risque = notionnel * _barriere_adverse()
    assert risque == pytest.approx(0.00144), (
        f"risque par trade {risque*100:.4f} % du solde, attendu 0,1440 % — "
        f"POSITION_SIZE_PCT n'a pas suivi l'échelle des distances")


# ── L'adaptation à la volatilité : préservée, pas supprimée ───────────────────

def test_le_multiplicateur_atr_suit_l_echelle_du_plancher():
    """Le plancher sature dès que atr_pct < SL_PCT×0,5 / ATR_SL_MULT.

    Ce rapport valait 0,004 avant l'échelle (0,006 / 1,5) et doit le rester :
    c'est lui qui fixe la proportion de setups où le stop suit réellement la
    volatilité — 73,4 % de saturation, mesurés sur l'archive. Mettre les
    distances à l'échelle sans le multiplicateur ferait saturer 100 % des cas,
    et le stop cesserait d'être adaptatif.
    """
    seuil = (config.SL_PCT * 0.5) / config.ATR_SL_MULT
    assert seuil == pytest.approx(0.004), (
        f"seuil de saturation {seuil:.5f}, attendu 0,004 — ATR_SL_MULT n'a pas "
        f"suivi l'échelle de SL_PCT")


def test_le_stop_dynamique_vaut_le_plancher_sur_une_volatilite_mediane():
    """ATR médian observé sur l'archive : 0,297 % du prix."""
    sl, _ = dynamic_sl_tp(0.00297 * 100, 100, config.SL_PCT, config.TP_PCT,
                          config.MIN_TP_PCT, config.ATR_SL_MULT)
    assert sl == _barriere_adverse()


def test_le_stop_dynamique_suit_la_volatilite_au_dessus_du_seuil():
    """Au-delà de 0,4 % d'ATR, le terme ATR reprend la main."""
    sl, _ = dynamic_sl_tp(0.008 * 100, 100, config.SL_PCT, config.TP_PCT,
                          config.MIN_TP_PCT, config.ATR_SL_MULT)
    assert sl > _barriere_adverse()


# ── Ordonnancement des niveaux ───────────────────────────────────────────────

def test_le_take_profit_est_au_dela_du_declenchement_du_trailing():
    """Sinon le TP fermerait la position avant que le trailing puisse s'armer."""
    _, tp = dynamic_sl_tp(0.00297 * 100, 100, config.SL_PCT, config.TP_PCT,
                          config.MIN_TP_PCT, config.ATR_SL_MULT)
    assert tp > config.TRAILING_TRIGGER_PCT


def test_la_barriere_favorable_est_plus_lointaine_que_l_adverse():
    assert _barriere_favorable() > _barriere_adverse()


def test_le_pas_du_trailing_reste_petit_devant_sa_distance():
    """Un pas proche de la distance ferait bouger le stop par à-coups."""
    assert config.TRAILING_STEP_PCT < config.TRAIL_PCT


# ── Alignement sur le protocole pré-enregistré ───────────────────────────────

def test_la_detention_maximale_vaut_l_horizon_du_test_de_barriere():
    """7 jours : sans ce plafond, le bot et la mesure ne portent pas sur le
    même objet, et 17 % des positions ne se résoudraient jamais."""
    assert config.MAX_HOLD_SEC == 7 * 24 * 3600
