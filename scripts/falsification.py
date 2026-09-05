"""Harnais de falsification — test de barriere sur l'archive Parquet.

Utilise pour les tests du 05/09/2026 (voir docs/falsification-2026-09-05.md).
S'emploie sur une copie locale de l'archive : ne jamais le faire tourner sur le
VPS, qui heberge le bot en production.

Principe : pour chaque setup, on suit les bougies 1 min et on regarde quelle
barriere (favorable ou adverse) est touchee en premier dans les 24 h. On
compare le taux observe a deux references :

  1. la marche aleatoire sans derive : p = adverse / (favorable + adverse)
     -> le signal porte-t-il une information directionnelle ?
  2. le seuil de rentabilite frais inclus : p = (adverse + frais) / (fav + adv)
     -> cette information suffit-elle a gagner de l'argent ?
"""
import glob
import numpy as np
import polars as pl

FRAIS_ALLER_RETOUR = 0.001      # 0,05 % par leg, taker Hyperliquid
HORIZON_MS = 24 * 3600 * 1000
TRAILING_TRIGGER_PCT = 0.010

_BOUGIES = {}


def charger_bougies(racine):
    """Charge ohlc_1m en tableaux numpy tries, un par coin."""
    if _BOUGIES:
        return _BOUGIES
    # Les fichiers anciens n'ont pas la colonne `backfilled` et les recents si :
    # on impose le schema plutot que de laisser polars trancher.
    d = (pl.scan_parquet(racine + "/ohlc_1m/**/*.parquet",
                         missing_columns="insert", extra_columns="ignore")
           .select(["coin", "timestamp", "high", "low", "close", "backfilled"])
           .collect()
           .sort("timestamp"))
    n_bf = d["backfilled"].fill_null(False).sum()
    print(f"[bougies] {d.height} minutes chargees, dont {n_bf} rattrapees en REST")
    for coin, g in d.group_by("coin"):
        nom = coin[0] if isinstance(coin, tuple) else coin
        _BOUGIES[nom] = {
            "ts": g["timestamp"].to_numpy(),
            "high": g["high"].to_numpy(),
            "low": g["low"].to_numpy(),
            "close": g["close"].to_numpy(),
        }
    return _BOUGIES


def _premiere_barriere(b, ts0, prix_fav, prix_adv, sens, horizon=HORIZON_MS):
    """Renvoie 'fav', 'adv', 'ambigu' ou None (aucune touchee sous l'horizon)."""
    i = np.searchsorted(b["ts"], ts0, side="right")
    j = np.searchsorted(b["ts"], ts0 + horizon, side="right")
    if i >= j:
        return None
    hauts, bas = b["high"][i:j], b["low"][i:j]
    if sens > 0:
        touche_fav, touche_adv = hauts >= prix_fav, bas <= prix_adv
    else:
        touche_fav, touche_adv = bas <= prix_fav, hauts >= prix_adv
    if not touche_fav.any() and not touche_adv.any():
        return None
    i_fav = int(np.argmax(touche_fav)) if touche_fav.any() else 10**9
    i_adv = int(np.argmax(touche_adv)) if touche_adv.any() else 10**9
    if i_fav < i_adv:
        return "fav"
    if i_adv < i_fav:
        return "adv"
    return "ambigu"      # les deux dans la meme bougie : non tranchable a la minute


def rendement_precedent(b, ts0, minutes=60):
    """Rendement des `minutes` precedant ts0. None si l'historique manque."""
    i = np.searchsorted(b["ts"], ts0, side="right") - 1
    k = np.searchsorted(b["ts"], ts0 - minutes * 60_000, side="right") - 1
    if i < 0 or k < 0 or i == k:
        return None
    return b["close"][i] / b["close"][k] - 1.0


def evaluer(setups, racine, *, sens_col="sens", fav_col="fav", adv_col="adv",
            prix_col="prix_entree", ts_col="timestamp", ambigu="adv"):
    """Deroule le test de barriere. `setups` est un DataFrame polars.

    ambigu : comment trancher quand les deux barrieres tombent dans la meme
             bougie. 'adv' est le choix conservateur.
    """
    bougies = charger_bougies(racine)
    issues, attendus = [], []
    n_ambigu = n_sans_issue = 0
    for r in setups.iter_rows(named=True):
        b = bougies.get(r["coin"])
        if b is None:
            continue
        p0, sens = r[prix_col], r[sens_col]
        fav, adv = r[fav_col], r[adv_col]
        if p0 is None or fav is None or adv is None or fav <= 0 or adv <= 0:
            continue
        prix_fav = p0 * (1 + sens * fav)
        prix_adv = p0 * (1 - sens * adv)
        issue = _premiere_barriere(b, r[ts_col], prix_fav, prix_adv, sens)
        if issue is None:
            n_sans_issue += 1
            continue
        if issue == "ambigu":
            n_ambigu += 1
            issue = ambigu
        issues.append(1 if issue == "fav" else 0)
        attendus.append(adv / (fav + adv))
    return {
        "issues": np.array(issues, dtype=float),
        "attendus": np.array(attendus, dtype=float),
        "n_ambigu": n_ambigu,
        "n_sans_issue": n_sans_issue,
    }


def _p_valeur(k, esperances):
    """P(observer >= k favorables) sous la binomiale-Poisson, approx normale."""
    mu = esperances.sum()
    var = (esperances * (1 - esperances)).sum()
    if var <= 0:
        return float("nan")
    from math import erfc, sqrt
    z = (k - 0.5 - mu) / sqrt(var)          # correction de continuite
    return 0.5 * erfc(z / sqrt(2))


def rapport(nom, res, setups=None, fav_col="fav", adv_col="adv"):
    y, p = res["issues"], res["attendus"]
    n = len(y)
    if n == 0:
        print(f"  {nom:<44} n=0 — rien a mesurer")
        return None
    obs, att = y.mean(), p.mean()
    ecart = (obs - att) * 100
    pv = _p_valeur(y.sum(), p)

    # seuil de rentabilite frais inclus, moyenne sur les setups
    if setups is not None:
        fav = setups[fav_col].to_numpy()
        adv = setups[adv_col].to_numpy()
        seuil = ((adv + FRAIS_ALLER_RETOUR) / (fav + adv)).mean()
    else:
        seuil = float("nan")
    marge = (obs - seuil) * 100

    print(f"  {nom:<44} n={n:>5}  obs {obs*100:5.1f}%  hasard {att*100:5.1f}%  "
          f"ecart {ecart:+5.1f} pts  p={pv:.3f}  vs rentabilite {marge:+5.1f} pts")
    return {"n": n, "obs": obs, "attendu": att, "ecart": ecart, "p": pv,
            "seuil_rentable": seuil, "marge": marge}


def bootstrap_jours(setups, racine, *, tirages=5000, graine=20260905):
    """IC par rechantillonnage de JOURS entiers.

    Les setups d'une meme journee partagent le mouvement du marche : les
    traiter comme independants surestime la significativite. On rechantillonne
    donc des journees completes, pas des setups.
    """
    bougies = charger_bougies(racine)
    lignes = []
    for r in setups.iter_rows(named=True):
        b = bougies.get(r["coin"])
        if b is None:
            continue
        p0, s, fav, adv = r["prix_entree"], r["sens"], r["fav"], r["adv"]
        issue = _premiere_barriere(b, r["timestamp"], p0 * (1 + s * fav),
                                   p0 * (1 - s * adv), s)
        if issue not in ("fav", "adv"):
            continue
        lignes.append((r["jour"], 1.0 if issue == "fav" else 0.0,
                       adv / (fav + adv),
                       (adv + FRAIS_ALLER_RETOUR) / (fav + adv)))
    if not lignes:
        return None
    jours = np.array([l[0] for l in lignes])
    y = np.array([l[1] for l in lignes])
    p = np.array([l[2] for l in lignes])
    seuil = np.array([l[3] for l in lignes])

    uniques = np.unique(jours)
    par_jour = {j: np.where(jours == j)[0] for j in uniques}
    rng = np.random.default_rng(graine)
    ec, ma = np.empty(tirages), np.empty(tirages)
    for t in range(tirages):
        idx = np.concatenate([par_jour[j] for j in rng.choice(uniques, len(uniques), True)])
        ec[t] = (y[idx].mean() - p[idx].mean()) * 100
        ma[t] = (y[idx].mean() - seuil[idx].mean()) * 100
    return {
        "n": len(y), "jours": len(uniques),
        "ecart": (y.mean() - p.mean()) * 100,
        "ecart_ic": (np.percentile(ec, 2.5), np.percentile(ec, 97.5)),
        "marge": (y.mean() - seuil.mean()) * 100,
        "marge_ic": (np.percentile(ma, 2.5), np.percentile(ma, 97.5)),
        "p_pas_edge": float((ec <= 0).mean()),
        "p_perdant": float((ma <= 0).mean()),
    }


def ligne_bootstrap(nom, r):
    if r is None:
        print(f"  {nom:<42} — rien a mesurer")
        return
    print(f"  {nom:<42} n={r['n']:>5}  ecart {r['ecart']:+5.1f} "
          f"[{r['ecart_ic'][0]:+5.1f};{r['ecart_ic'][1]:+5.1f}]  "
          f"marge {r['marge']:+5.1f} [{r['marge_ic'][0]:+5.1f};{r['marge_ic'][1]:+5.1f}]  "
          f"P(perdant)={r['p_perdant']:.2f}")
