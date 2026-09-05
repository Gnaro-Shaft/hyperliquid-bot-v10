# Protocole pré-enregistré — recherche d'edge V10

**Rédigé le 05/09/2026 à 19:30 UTC, sur le commit `e0e958d`.**

Ce document est écrit **avant** de regarder les données de test. Sa date de
commit dans git en fait foi : toute analyse dont le script est committé
*après* la lecture des données qu'il consomme est nulle et non avenue.

---

## 1. Pourquoi ce document existe

Quatre hypothèses ont été testées sur les 41 jours du 10/07 au 19/08
(`falsification-2026-09-05.md`). L'une d'elles — l'inversion du signal —
affichait +4,3 points avec p = 0,002 et s'est révélée être un artefact : deux
coins sur dix portaient l'effet, et la p-valeur était surestimée d'un facteur
40 par une hypothèse d'indépendance fausse.

C'est le mécanisme normal de la fouille de données. Il ne se corrige pas par
plus de rigueur au moment du calcul : il se corrige en **fixant la question
avant de voir la réponse**. D'où ce protocole.

---

## 2. Les trois jeux de données

| Jeu | Période | Statut | Usage autorisé |
|---|---|---|---|
| **A — formation** | 10/07 → 19/08/2026 | contaminé (4 hypothèses testées) | Former des hypothèses. Librement. Aucune décision. |
| **B — transition** | 05/09/2026 → déploiement | règle 11 activée, ancienne géométrie | Aucun. Régime bâtard. |
| **C — test** | déploiement → n atteint | **scellé** | Une seule lecture, par hypothèse enregistrée. |

Le jeu C ne commence qu'au déploiement d'une géométrie décidée. Sa date exacte
est à inscrire au §9 au moment du déploiement, par un commit distinct.

**Tout changement de la géométrie, des paires suivies ou du calcul du score
clôt le jeu C et en ouvre un nouveau.** Le compteur d'observations repart de
zéro. C'est le prix de la validité ; il n'y a pas de contournement.

---

## 3. Ce qui est mesuré

### 3.1 Pas les trades réalisés

Avec `MAX_OPEN_POSITIONS = 2` et une détention médiane de 43 h sous la
géométrie envisagée, le bot produirait environ **1 trade par jour**, soit ~117
en 3,5 mois. C'est trop peu : les 194 trades du paper mode donnaient déjà un
intervalle de confiance qui enjambait zéro.

**La décision repose donc sur le test de barrière contrefactuel**, appliqué à
*tous* les setups émis, indépendamment de ce que le bot a pu prendre. Les
trades réalisés servent au contrôle d'exécution (§7), pas à la décision.

### 3.2 Métrique primaire : la marge de rentabilité

Pour chaque setup : quelle barrière est touchée en premier, favorable ou
adverse, dans l'horizon retenu.

- Seuil de rentabilité, frais inclus : `(adverse + frais) / (favorable + adverse)`
- **Marge** = taux favorable observé − seuil de rentabilité, en points.

La comparaison à la marche aléatoire (`adverse / (favorable + adverse)`) reste
reportée comme métrique **secondaire**, informative mais non décisionnelle :
battre le hasard ne paie pas les frais.

### 3.3 Méthode statistique — fixée

- Un seul setup retenu par (coin, bougie de 15 min) : la première évaluation.
- Intervalle de confiance à 95 % par **bootstrap de journées entières**,
  5 000 tirages, graine `20260905`. Jamais par tirage de setups : ceux d'une
  même journée partagent le mouvement du marché.
- Issue ambiguë (les deux barrières dans la même minute) : comptée **adverse**.
- Harnais : `scripts/falsification.py`, sur copie locale de l'archive.

**Contrôle d'intégrité obligatoire avant toute mesure.** Rejouer le test de
référence sur le jeu A ; il doit rendre exactement :

```
n = 1074   observé 34,3 %   attendu 38,5 %   12 sans issue sous 24 h
```

Si ces quatre nombres ne tombent pas, la mesure est invalide et rien n'en est
tiré.

---

## 4. La règle de décision

Le passage en réel exige **les trois conditions simultanément** :

1. **n ≥ 1 160** setups résolus sur le jeu C ;
2. **borne basse** de l'IC95 de la marge **> 0** ;
3. **marge ponctuelle ≥ +2,0 points**.

La troisième condition est une réserve de glissement. Le glissement n'est pas
modélisé ; sur la géométrie 5 %/3 %, 2 points de marge valent 0,16 % de gain
espéré par trade, soit environ quatre fois le glissement attendu (~0,04 %
aller-retour). Une marge positive mais inférieure à 2 points sera traitée comme
un échec, pas comme un encouragement.

Équivalence utile : avec un handicap de frais de 1,12 point, une marge de
+2,0 points correspond à un écart au hasard d'environ +3,1 points.

**Aucune de ces trois conditions n'est négociable après coup.** Si le résultat
tombe juste à côté, la réponse est non.

---

## 5. Taille d'échantillon et arrêt

n = 1 160 vient de la puissance : détecter 4 points à 95 % de confiance et 80 %
de puissance, autour de p ≈ 0,385, demande `(1,96+0,84)² × p(1−p) / 0,04²`.

À ~22 setups résolus par jour, et en tenant compte du chevauchement des
positions (détention médiane 43 h, soit une indépendance effective de l'ordre
de la moitié), **compter environ 3,5 mois**.

**Aucune analyse intermédiaire.** Une seule lecture du jeu C, quand n est
atteint. Regarder en cours de route puis décider de continuer ou d'arrêter
selon ce qu'on voit détruit la validité du test aussi sûrement que la fouille
de données.

Le seul motif d'arrêt anticipé est **opérationnel** : panne de collecte,
changement de régime imposé, décision d'abandonner le projet. Dans ce cas les
données sont déclarées incomplètes et aucune conclusion n'en est tirée.

---

## 6. Hypothèses enregistrées

### H1 — le signal actuel, sous géométrie corrigée

> Sous une géométrie à handicap de frais réduit (~5 % favorable / ~3 % adverse,
> horizon 7 jours), le signal `|signal_level| == 2` inchangé dégage une marge
> de rentabilité strictement positive.

- **Statut** : enregistrée le 05/09/2026.
- **Prédiction de l'auteur** : *échec*. Rien dans le jeu A ne suggère un edge
  directionnel ; la correction de géométrie retire un handicap, elle ne crée
  pas d'information. H1 est enregistrée comme **témoin** : c'est la valeur de
  référence que toute hypothèse ultérieure devra battre.

### H2, H3 — à enregistrer séparément

Les données non explorées (open interest, clusters de liquidation, flux de
baleines) autorisent la formation d'hypothèses **sur le jeu A uniquement**.
Chacune doit être enregistrée ici, par un commit daté, **avant** d'être
confrontée au jeu C, et doit énoncer :

1. le mécanisme économique attendu — pourquoi cet effet existerait ;
2. la règle de calcul exacte, sans paramètre libre ;
3. la prédiction signée de l'auteur, avant mesure.

Une hypothèse sans mécanisme énoncé est une corrélation en quête de récit :
elle n'est pas recevable.

### Budget de comparaisons

**Trois hypothèses au maximum** sur un même jeu C. Au-delà, seuil ajusté par
Bonferroni (borne basse de l'IC à 98,3 % pour trois tests).

Les variantes d'une même hypothèse — balayage de seuils, sous-ensembles de
coins, découpages temporels — **comptent comme des hypothèses distinctes**.
C'est précisément ce qui a fabriqué le faux positif de l'inversion.

---

## 7. Contrôles annexes (non décisionnels)

Mesurés en continu, ils ne servent qu'à valider que le test porte sur la
réalité :

- **Écart d'exécution** : prix de remplissage réel des trades paper contre prix
  d'entrée théorique du test. Mesure le glissement réel et vérifie la réserve
  de 2 points.
- **Couverture des bougies 1 min** : si elle tombe sous 85 % sur la période,
  la mesure est déclarée dégradée.
- **Continuité de la collecte** : tout trou supérieur à 6 h est consigné. Les
  journées affectées sortent du bootstrap.

---

## 8. Critère d'abandon

Si les trois hypothèses du budget échouent sur un jeu C valide, la conclusion
enregistrée d'avance est :

> Le trading directionnel automatisé sur ces dix paires, à cette échelle de
> temps et avec cette structure de frais, ne dégage pas d'edge exploitable.

La suite alors décidée d'avance : **arrêter de chercher un edge, garder la
collecte.** L'archive Parquet est le seul actif du projet qui s'apprécie ;
elle continue quoi qu'il arrive, et coûte le prix d'un VPS.

Écrire ce critère maintenant a un but précis : empêcher qu'après trois échecs
on invente une quatrième hypothèse pour ne pas conclure.

---

## 9. Journal des enregistrements

| Date | Événement | Commit |
|---|---|---|
| 05/09/2026 19:30 UTC | Protocole rédigé. Jeu A clos, contaminé. | `e0e958d` |
| — | Déploiement de la géométrie corrigée → **ouverture du jeu C** | *à remplir* |
| — | H1 mesurée | *à remplir* |

Toute ligne ajoutée à ce tableau l'est par un commit distinct, à la date de
l'événement. Une ligne rétro-datée invalide le protocole.
