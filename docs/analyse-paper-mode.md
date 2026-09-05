# Analyse du paper trading — 05/09/2026

Première évaluation de l'edge, sur les données collectées du **10/07 au 22/08/2026**.
Question posée : le signal d'entrée bat-il le hasard ?

**Réponse courte : non.** Le passage en réel n'est pas justifié en l'état.

> ⚠️ **Corrigé le 05/09/2026.** Ce document affirmait plus bas que l'écart de
> −4,2 points « n'est pas attribuable à la chance » (p = 0,998). C'était
> surconfiant : le calcul supposait les setups indépendants, alors qu'un même
> mouvement de marché en déclenche plusieurs le même jour. Avec un bootstrap
> par journées, l'IC95 de l'écart est [−10,0 ; +2,0] et **enjambe zéro**.
>
> Ce qui reste établi, et plus solidement : la stratégie est **non rentable**,
> marge −10,2 points, IC95 [−16,0 ; −4,1], P(perdant) = 1,00. Voir
> `falsification-2026-09-05.md` §5.

---

## 1. Résultat du paper trading

194 trades fermés, frais inclus (0,05 % par leg, aller-retour 0,1 %, taker
Hyperliquid — modélisés dans `utils/paper_sim.py`).

| | |
|---|---|
| PnL total | **−4,63 USDC** (−0,46 % sur 1 000 simulés) |
| Win rate | 36,6 % |
| Ratio gain/perte | 1,65 |
| Profit factor | 0,95 |
| Espérance | −0,024 USDC/trade |
| Drawdown max | −26,29 USDC (−2,6 %) |
| IC 95 % sur l'espérance | **[−0,19 ; +0,15]** — enjambe zéro |

Le win rate d'équilibre pour un R:R de 1,65 est de **37,7 %**. Observé : 36,6 %.
La stratégie opère au niveau du pile ou face, à 1,1 point près.

### Répartition par sortie

| Sortie | n | PnL | Win rate |
|---|---|---|---|
| Trailing stop | 54 | **+61,53** | 100 % |
| Ordre exchange (TP/SL) | 136 | **−63,59** | 12,5 % |
| Inversion de signal | 4 | −2,57 | 0 % |

Le trailing ne fait que compenser les stops. Net ≈ 0.

---

## 2. Pourquoi 87,5 % des sorties exchange sont des stops

**Ce n'est pas un stop mal réglé.** Le stop se situe à **1,6 à 4,1 ATR(15m)**
selon le coin — une distance normale. C'est le TP qui est hors d'atteinte :
2,48 % fixe, soit **6 à 15 ATR**.

Avec des barrières à +2,48 % / −0,66 % (R:R nominal 3,75:1), une marche
aléatoire *sans dérive* touche le stop en premier dans **78,9 %** des cas —
c'est le simple rapport des distances. Le trailing retire ensuite les gagnants
de ce panier, ce qui porte le chiffre observé à 87,5 %.

Conclusion : ce taux découle du choix de R:R, pas d'un défaut de réglage.
Le déplacer ne créerait pas d'edge, il déplacerait le point d'équilibre.

---

## 3. Test du signal sur l'archive

Les 194 trades sont un petit échantillon, et ils subissent les filtres
(cooldown, corrélation, exposition). Pour mesurer le signal **brut**, on rejoue
l'archive.

### Méthode

- Population : les évaluations à `|signal_level| == 2` avec `gate_passed`.
  15 119 lignes — mais **1 086 bougies de 15 min distinctes** seulement, car une
  évaluation tombe toutes les 15 s. On ne retient que la **première évaluation
  de chaque bougie** : sans ce dédoublonnage, un même setup compterait 13,9 fois
  et gonflerait artificiellement la significativité.
- Pour chaque setup, on suit les bougies `ohlc_1m` sur 24 h et on regarde quelle
  barrière est touchée en premier :
  - **favorable** = `max(atr_pct × 2, TRAILING_TRIGGER_PCT)` — le seuil qui
    déclenche réellement le trailing ;
  - **adverse** = `dynamic_sl` de l'évaluation (recalculé en `max(atr×1.5, SL_PCT)`
    pour les fichiers antérieurs à l'ajout de la colonne).
- Référence : pour une marche aléatoire sans dérive, la probabilité de toucher
  la barrière favorable en premier vaut `adverse / (favorable + adverse)`. Elle
  est calculée **par setup**, les barrières étant hétérogènes.

### Résultat

**1 074 setups tranchés** (12 sans issue sous 24 h).

| | |
|---|---|
| Excursions favorables observées | **34,3 %** |
| Attendu par marche aléatoire | 38,5 % |
| Écart | **−4,2 points** |
| p-valeur (obtenir ce résultat ou mieux par hasard) | 0,998 |

~~Une p-valeur de 0,998 signifie que l'écart dans l'autre sens serait
significatif à p ≈ 0,002. Le signal ne se contente pas d'être neutre.~~

**Corrigé le 05/09/2026** : cette p-valeur suppose l'indépendance des setups,
hypothèse fausse (plusieurs coins se déclenchent sur le même mouvement). Après
bootstrap par journées, l'écart de −4,2 points a un IC95 de [−10,0 ; +2,0] :
le signal **n'est pas démontré pire que le hasard**. Il reste démontré non
rentable. Voir `falsification-2026-09-05.md` §0.2 et §5.

---

## 4. La dérive du marché n'explique pas l'écart — elle l'aggrave

Sur la période, presque tous les coins montent : BTC +24 %, ETH +40 %,
WIF +39 %, kPEPE +59 % (exceptions : INJ −2,5 %, TIA −18 %).

Dans un marché haussier, les signaux d'**achat** devraient battre le hasard.
Ce sont eux qui décrochent le plus :

| Sens | n | Favorable | Attendu | Écart |
|---|---|---|---|---|
| Achat | 492 | 31,7 % | 38,3 % | **−6,6** |
| Vente | 582 | 36,4 % | 38,6 % | −2,2 |

Et par coin, les pires écarts correspondent aux plus fortes hausses :
ETH −15,7, kPEPE −11,2, WIF −10,9, DOGE −10,2.

---

## 5. Le mécanisme : le signal court après le mouvement

Rendement médian de l'heure **précédant** le signal :
**+0,45 % avant un achat**, **−0,52 % avant une vente**. Le signal est donc
momentum, pas contrarian — et c'est là que la perte se concentre.

| Contexte | n | Favorable | Attendu | Écart |
|---|---|---|---|---|
| **Achat après hausse > 0,3 %** | 329 | 29,5 % | 38,5 % | **−9,0** |
| Achat en marché plat | 163 | 36,2 % | 37,8 % | −1,6 |
| Vente après baisse > 0,3 % | 409 | 35,0 % | 38,9 % | −3,9 |
| Vente en marché plat | 168 | 39,3 % | 38,0 % | **+1,3** |

**Plus le signal poursuit un mouvement engagé, plus il perd.** Déclenché dans un
marché calme, il est neutre — voire légèrement positif à la vente.

---

## 6. Ce que ça implique

> **Les deux pistes ci-dessous ont été testées le 05/09/2026 et sont toutes
> deux falsifiées** — de même que l'inversion du signal et le carry de funding.
> Voir `falsification-2026-09-05.md`. Le texte d'origine est conservé tel quel
> pour garder trace du raisonnement.

Le levier n'est ni le stop ni le TP : c'est le **timing d'entrée**. Deux pistes
mesurables sur ces mêmes 1 074 setups, sans risquer un centime :

1. **Refuser l'entrée après une extension** de plus de ~0,3 % sur l'heure
   écoulée. Les données suggèrent que le sous-ensemble « marché plat » se
   comporte tout autrement.
2. **Élargir le pullback.** `PULLBACK_PCT` attend un repli minime ; les chiffres
   invitent à tester des valeurs nettement supérieures.

---

## 7. Réserves

- **Une seule période de six semaines, un seul régime de marché** (haussier).
  Rien ne garantit que ces écarts persistent ailleurs.
- Le prix d'entrée du test est le `close` de l'évaluation, alors que le bot
  entre sur pullback : la réalité est donc un peu meilleure que ce test.
- Couverture des bougies 1 min : **88 %** de la période.
- Les chiffres **par coin** (n = 67 à 172) restent bruités — ne pas en tirer de
  règle par actif.
- **L'activation de la règle 11 le 05/09/2026** (voir `SCHEMA.md`) modifie le
  score : les signaux produits après cette date ne sont pas comparables à ceux
  analysés ici.
- Le slippage n'est pas modélisé, et les fills paper sont simulés sur les mèches
  des bougies : le réel serait plutôt moins bon.
