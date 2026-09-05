"""Gates du moteur : ce qui bloque une entrée, et à quel seuil.

Ces barrières décident si le bot a le droit d'entrer. Aucune n'était testée.
La normalisation est en plus rejouée sur des évaluations réelles archivées :
l'archive contient raw_score, threshold_used et signal_level, donc l'oracle
est la production elle-même.
"""

import glob

import pytest

from strategy import gates


# ── Normalisation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("score,seuil,attendu", [
    (10, 10, 2),      # au seuil exact : ±2 accordé
    (9, 10, 1),       # sous le seuil mais >= 4
    (4, 10, 1),       # frontière basse du ±1
    (3, 10, 0),       # juste en dessous : neutre
    (0, 10, 0),
    (-3, 10, 0),
    (-4, 10, -1),
    (-10, 10, -2),
    (-11, 10, -2),
])
def test_niveau_depuis_score(score, seuil, attendu):
    assert gates.niveau_depuis_score(score, seuil) == attendu


def test_le_seuil_du_regime_deplace_la_frontiere():
    """Un régime WEAK ajoute +1 au seuil : le même score ne suffit plus."""
    assert gates.niveau_depuis_score(10, 10) == 2
    assert gates.niveau_depuis_score(10, 11) == 1


# ── Gate 1h ──────────────────────────────────────────────────────────────────

def test_gate_1h_bloque_le_contre_courant():
    assert gates.gate_1h(2, "bear")[0] is True
    assert gates.gate_1h(-2, "bull")[0] is True


def test_gate_1h_laisse_passer_le_sens_de_la_tendance():
    assert gates.gate_1h(2, "bull")[0] is False
    assert gates.gate_1h(-2, "bear")[0] is False
    assert gates.gate_1h(2, "neutral")[0] is False


def test_gate_1h_ignore_les_signaux_faibles():
    """Un ±1 ne déclenche aucune entrée : le bloquer n'aurait pas de sens."""
    for level in (-1, 0, 1):
        assert gates.gate_1h(level, "bear")[0] is False
        assert gates.gate_1h(level, "bull")[0] is False


# ── Gate ML ──────────────────────────────────────────────────────────────────

def test_verdict_ml_aux_trois_paliers():
    assert gates.verdict_ml(0.10)[0] == "blocked"
    assert gates.verdict_ml(0.42)[0] == "penalty"
    assert gates.verdict_ml(0.90)[0] == "ok"


def test_verdict_ml_aux_frontieres_exactes():
    """Les comparaisons sont strictes : au seuil, on ne bloque pas."""
    assert gates.verdict_ml(gates.ML_BLOCK_THRESHOLD)[0] == "penalty"
    assert gates.verdict_ml(gates.ML_PENALTY_THRESHOLD)[0] == "ok"


# ── Régime et filtre anti-chop ───────────────────────────────────────────────

@pytest.fixture
def legacy(monkeypatch):
    """Force le comportement v8.4 (presets neutres), déterministe."""
    monkeypatch.setattr(gates, "REGIME_ADAPTIVE", False)


def test_regime_legacy_range_bloque(legacy):
    r = gates.evaluer_regime(adx_val=15.0, bb_w=0.02, atr_pct=0.01)
    assert r["regime"] == "RANGE" and r["blocked"] is True
    assert "BLOCKED" in r["debug"]["gate"]


def test_regime_legacy_weak_durcit_le_seuil(legacy):
    r = gates.evaluer_regime(adx_val=27.0, bb_w=0.02, atr_pct=0.01)
    assert r["regime"] == "WEAK"
    assert r["threshold_adj"] == 1        # il faut un point de plus pour un ±2
    assert r["blocked"] is False


def test_regime_legacy_strong_passe_sans_penalite(legacy):
    r = gates.evaluer_regime(adx_val=35.0, bb_w=0.02, atr_pct=0.01)
    assert r["regime"] == "STRONG" and r["threshold_adj"] == 0
    assert "PASSED" in r["debug"]["gate"]


def test_squeeze_bloque_meme_avec_une_tendance_forte(legacy):
    """Bandes resserrées : le prix ne va nulle part, quel que soit l'ADX."""
    r = gates.evaluer_regime(adx_val=40.0, bb_w=0.001, atr_pct=0.01)
    assert r["is_squeeze"] is True and r["blocked"] is True
    assert "SQUEEZE" in r["debug"]["bb_width_filter"]


def test_les_multiplicateurs_sont_neutres_en_legacy(legacy):
    r = gates.evaluer_regime(adx_val=35.0, bb_w=0.02, atr_pct=0.01)
    assert (r["tp_mult"], r["sl_mult"], r["size_mult"]) == (1.0, 1.0, 1.0)


# ── Golden master de la normalisation ────────────────────────────────────────

ARCHIVE = "/Users/dgnaro/V10-archive/parquet/signal_evaluations"


def test_normalisation_rejouee_sur_la_production():
    """niveau_depuis_score(raw_score, threshold_used) doit rendre signal_level."""
    import polars as pl
    fichiers = sorted(glob.glob(f"{ARCHIVE}/coin=*/date=*.parquet"))
    if not fichiers:
        pytest.skip("archive absente")
    compares = ecarts = 0
    for f in fichiers[::9]:
        df = pl.read_parquet(f)
        for l in df.head(500).to_dicts():
            if not l.get("gate_passed") or l.get("threshold_used") is None:
                continue
            if l.get("raw_score") is None or l.get("signal_level") is None:
                continue
            compares += 1
            if gates.niveau_depuis_score(l["raw_score"], l["threshold_used"]) != l["signal_level"]:
                ecarts += 1
    if compares < 100:
        pytest.skip(f"trop peu de lignes exploitables ({compares})")
    print(f"\n  normalisation conforme sur {compares - ecarts}/{compares} évaluations réelles")
    assert ecarts == 0
