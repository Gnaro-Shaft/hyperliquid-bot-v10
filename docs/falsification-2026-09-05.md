# Tests de falsification — 05/09/2026

Suite de `analyse-paper-mode.md`, qui concluait à l'absence d'edge et proposait
deux pistes. Ce document teste ces pistes, plus deux autres, sur les **mêmes**
données : 41 jours continus (10/07 → 19/08/2026), 1 074 setups indépendants.

**Aucune piste ne survit.** Et au passage, la méthode employée ici invalide une
affirmation du document précédent — voir §5.

---

## 0. Deux corrections de méthode

### 0.1 Le bon étalon n'est pas le hasard, c'est la rentabilité

Le document précédent comparait le taux d'excursions favorables à celui d'une
marche aléatoire (38,5 %). C'est le bon test pour « le signal porte-t-il une
information ? », mais pas pour « faut-il trader ? ».

Avec les barrières employées (favorable ≈ 1,0 %, adverse ≈ 0,6 %) et 0,1 % de
frais aller-retour, le seuil de rentabilité est à **44,5 %**.

> Il ne suffit pas de battre le hasard : il faut le battre de **6 points**.

Les deux mesures sont donc reportées partout : `écart` (vs hasard) et
`marge` (vs rentabilité).

### 0.2 Les setups ne sont pas indépendants

Un mouvement de marché déclenche plusieurs coins la même journée. Traiter les
1 074 setups comme indépendants surestime la significativité. Tous les
intervalles de confiance ci-dessous proviennent d'un **bootstrap par blocs de
journées entières** (5 000 tirages sur 41 jours), pas d'un tirage de setups.

L'effet de cette correction est considérable : sur l'inversion, la p-valeur
passe de **0,002 à 0,087**.

---

## 1. T1 — Filtrer les entrées en extension

Hypothèse 1 du document précédent : refuser l'entrée quand le mouvement de
l'heure écoulée dépasse ~0,3 %.

| Filtre | n | écart vs hasard | marge vs rentabilité |
|---|---|---|---|
| aucun (référence) | 1 074 | −4,2 | −10,2 |
| extension ≤ 1,0 % | 914 | −4,3 | −10,4 |
| extension ≤ 0,6 % | 650 | −2,5 | −8,7 |
| **extension ≤ 0,3 %** | 338 | **−0,1** | **−6,3** |
| extension ≤ 0,1 % | 121 | +4,8 | −1,3 |
| extension ≤ 0,0 % | 50 | +11,4 | +5,4 |

**Verdict : falsifiée.** Le filtre fait exactement ce qu'on pouvait prévoir — il
supprime une fuite, il ne crée pas d'edge. À 0,3 %, on atteint la neutralité
(−0,1) et on reste à 6,3 points sous la rentabilité.

Les deux dernières lignes sont l'extrémité d'un balayage de seuils sur n=121 et
n=50 : elles ne sont pas une découverte, elles sont le bruit qu'un balayage
produit toujours à sa queue.

---

## 2. T2 — Inverser le signal

Puisque le signal fait moins bien que le hasard, le prendre à contre-pied
devrait faire mieux. C'est la piste qui a le mieux résisté, et c'est celle qui
demandait le plus d'efforts pour être écartée.

**Naïvement, elle fonctionne :** +4,3 points, p = 0,002.

Trois attaques la démontent :

| Test | n | écart | IC95 (bootstrap-jours) | P(perdant) |
|---|---|---|---|---|
| inversion, tous coins | 1 071 | +4,3 | [−1,7 ; +10,8] | **0,71** |
| inversion sans ETH ni kPEPE | 889 | +1,1 | [−5,1 ; +7,3] | 0,94 |
| inversion, extension > 0,6 % | 423 | +6,3 | [−0,5 ; +13,3] | 0,43 |

1. **Deux coins portent tout.** ETH (+20,8) et kPEPE (+18,6) contre SOL (−5,9)
   et INJ (−5,5). Retirés, l'effet tombe à +1,1 point, p = 0,263.
2. **Le bootstrap par jours** élargit l'IC jusqu'à enjamber zéro.
3. **Même au mieux, ça ne rapporte pas.** La meilleure variante affiche une
   marge de +0,6 point sur n=423 — l'erreur-type y est de 2,4 points.

**Verdict : falsifiée.** L'inversion récupère le miroir arithmétique d'un écart
négatif ; elle atteint le seuil de rentabilité sans le franchir.

---

## 3. T3 — Élargir le pullback

Hypothèse 2 du document précédent. Entrée sur repli, fenêtre de 30 min.

| Pullback | % touchés | écart | marge |
|---|---|---|---|
| 0,15 % (actuel) | 60 % | −5,3 | −11,2 |
| 0,30 % | 33 % | −6,6 | −12,5 |
| 0,50 % | 16 % | −11,5 | −17,1 |
| 0,80 % | 6 % | −16,9 | −22,5 |

**Verdict : falsifiée, et dans le mauvais sens.** La dégradation est monotone.
L'explication est mécanique : sur un signal momentum, attendre un repli revient
à ne conserver que les cas où le mouvement a échoué. C'est de la sélection
adverse, pas un meilleur prix d'entrée.

---

## 4. T4 — Le funding

### 4.1 Un fait structurel

Le taux de funding est **collé au taux de base** (1,25 × 10⁻⁵/h, soit 11 %/an)
**61,3 %** du temps, et n'est négatif que 16,7 % du temps. Moyenne annualisée
par coin : de −4,8 % (INJ) à +9,9 % (WIF).

Le carry existe donc, mais il **ne se récolte pas en perpétuel seul** : rester
short pour l'encaisser revient à prendre toute la hausse contre soi. Il
faudrait une jambe spot, que le bot n'a pas.

### 4.2 Carry transversal (long funding bas / short funding haut)

C'est la seule forme récoltable sans spot. Fenêtres disjointes, frais 0,045 %
par leg.

| Horizon | k | n fenêtres | brut | net | IC95 net | P(perdant) |
|---|---|---|---|---|---|---|
| 8 h | 3 | 124 | −0,093 % | −0,273 % | [−0,457 ; −0,105] | **1,00** |
| 24 h | 2 | 40 | +0,151 % | −0,029 % | [−0,557 ; +0,497] | 0,54 |

**Verdict : falsifiée.** L'économie est sans appel : la dispersion de funding
entre coins vaut ~2 %/an, soit ~0,02 % sur 8 h, quand un rééquilibrage coûte
0,18 %. Les frais sont dix fois l'effet recherché.

### 4.3 Le funding prédit-il le prix ?

Corrélation funding / rendement à 8 h : **+0,095**, et le profil par quintile
n'est pas monotone. Le signe est de surcroît l'inverse de l'hypothèse de
positionnement encombré.

---

## 5. Correction du document précédent

`analyse-paper-mode.md` affirme que l'écart de −4,2 points « n'est pas
attribuable à la chance » (p = 0,998). **Cette affirmation était surconfiante :**
elle repose sur l'hypothèse d'indépendance des setups, que §0.2 invalide.

Avec le bootstrap par journées :

| | valeur | IC95 | conclusion |
|---|---|---|---|
| écart vs hasard | −4,2 | **[−10,0 ; +2,0]** | enjambe zéro — **non établi** |
| marge vs rentabilité | −10,2 | **[−16,0 ; −4,1]** | n'enjambe pas zéro — **établi** |

La conclusion pratique n'est pas affaiblie, elle est déplacée :

> Le signal n'est **pas** démontré pire que le hasard.
> Il est démontré **non rentable**, avec P(perdant) = 1,00.

C'est plus solide que la version initiale, parce que ça ne dépend plus d'un
écart directionnel fragile mais de la géométrie barrières/frais, qui, elle, ne
bouge pas.

---

## 6. Ce que ça implique

Quatre hypothèses testées, quatre falsifications. Le momentum 15 min sur ces
dix paires ne produit pas d'edge exploitable, ni endroit, ni à l'envers, ni
filtré, ni retardé — et le funding ne comble pas le vide.

**Le budget statistique de ce jeu de données est en grande partie consommé.**
Quatre hypothèses y ont déjà été testées ; en enchaîner d'autres sur les mêmes
41 jours produira tôt ou tard un résultat positif par pur hasard. Les pistes
restantes (open interest, clusters de liquidation, flux de baleines — données
déjà collectées mais non testées) ne devraient être évaluées qu'avec un
protocole **fixé d'avance** : hypothèse, seuil de décision et horizon écrits
avant de regarder les données.

Rappel de la puissance disponible (`analyse-paper-mode.md` §7) : 1 074 setups
permettent de détecter un effet de ~3,5 points. Or il en faut **6** pour être
rentable. Un edge tout juste suffisant serait donc à la limite du mesurable —
raison de plus pour ne chercher que des effets francs.

**La collecte reste le seul actif qui s'apprécie.** Elle continue.

---

## 7. Reproduire

Harnais : `scripts/falsification.py`. Il s'exécute sur une copie **locale** de
l'archive Parquet — jamais sur le VPS, qui héberge le bot en production.

Le test de référence doit retrouver : n = 1 074, 34,3 % observé, 38,5 % attendu,
12 setups sans issue sous 24 h. Si ces quatre nombres ne tombent pas, le reste
du document ne vaut rien.
