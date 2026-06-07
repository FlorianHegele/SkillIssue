---
name: alerte-crue
description: >
  Trigger when user asks about flood risk, river flooding, or rising water for a French
  location — alert color, real-time water level/discharge, or rainfall forecast. Mots-clés FR :
  crue, inondation, montée des eaux, risque crue, vigilance crue, Vigicrues, couleur d'alerte
  (vert/jaune/orange/rouge), hauteur d'eau, débit, cote, station hydrométrique, prévision pluie,
  cumul de précipitations, Hub'Eau, AROME. EN keywords: flood, flooding, flood warning, river
  level, water level, gauge, discharge, rainfall/precipitation forecast. Donne une synthèse pour
  une commune (nom ou code INSEE) ou des coordonnées lat/lon en France (métropole + DOM).
allowed-tools: Bash(python3 *)
---

# alerte-crue

Synthèse du risque de crue pour un lieu en France, à partir de trois sources publiques sans clé :
vigilance officielle (**Vigicrues**), mesure temps réel (**Hub'Eau**), prévision de pluie
(**OpenMeteo / AROME**).

## Quand l'utiliser

L'utilisateur s'interroge sur une crue / inondation : niveau de vigilance, hauteur d'eau ou débit
en temps réel, ou pluie à venir, pour une commune ou un point précis.

## Comment lancer

Localisation **obligatoire** (aucun lieu par défaut) : `--commune` (nom ou code INSEE) **ou**
`--lat`/`--lon`.

```bash
# Par commune (nom ou code INSEE)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès"
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007

# Par coordonnées
python3 ${CLAUDE_SKILL_DIR}/main.py --lat 44.128 --lon 4.082

# Une seule source (répétable) ; élargir le rayon de recherche
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès" --only pluie
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès" --only vigilance --only hydro --radius 25
```

Options : `--only {vigilance,hydro,pluie}` (défaut : les trois), `--radius` km (défaut 15),
`--timeout` s (défaut 20), `--detail` (pluie : ajoute la série horaire complète 24 h),
`--seuil-pluie` mm/h (pluie : seuil d'une heure « pluvieuse », défaut 0.5).

## Sortie

JSON sur stdout : `{ lieu, vigilance, hydro[], pluie }` (hauteurs en mm, débits en l/s, pluie en
mm). Reformuler ensuite en langage naturel pour l'utilisateur. Une source en échec apparaît avec
un champ `error` sans bloquer les autres ; une localisation manquante/introuvable renvoie une
erreur sur stderr avec un code retour ≠ 0.

**Hydro** : une mesure (`hauteur_mm`, `debit_ls`) vaut soit un nombre, soit une **chaîne
explicative** si elle manque (ex. `"indisponible : pas de mesure temps réel récente"`,
`"erreur : …"`) — jamais `null` ambigu. Une station n'est listée que si elle porte au moins
une vraie mesure.

**Pluie** (optimisée pour la décision) : `cumul_prochaines_24h_mm`, `pic` (heure + intensité la
plus forte), `creneaux[]` (chaque épisode pluvieux contigu : `debut`/`fin`/`cumul_mm` — une
accalmie sépare deux créneaux, pas d'intervalle qui masquerait les trous), et
`heures_pluvieuses[]` — **seules** les heures où la pluie atteint le seuil (les heures sèches
sont écartées ; liste vide = pas de pluie notable).
L'intensité horaire (mm/h) est le facteur déclenchant des crues-éclair cévenoles. La série
horaire intégrale n'apparaît (`par_heure[]`) qu'avec `--detail`.

Contrat de sortie (défini en amont) : `contract.py` (dataclasses typées) +
`contract.schema.json` (validé hors-ligne par `tests/test_contract.py`). Infra commune
(HTTP, résolution commune, erreurs) : `skills/_common/`.

Détails des API, champs et pièges : voir `references/api.md`.
