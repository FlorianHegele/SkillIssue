---
name: demographie-iris
version: 1.0.0
description: >
  Trigger when user asks about population, demographics, households, families, or vulnerable
  residents of a French location at neighborhood (IRIS) granularity — how many people or
  households to evacuate/shelter, where single-parent families are concentrated. Mots-clés FR :
  démographie, population, habitants, ménages, familles, familles monoparentales, foyers,
  vulnérabilité, personnes à évacuer, capacité d'évacuation, quartier, IRIS, recensement, INSEE.
  EN keywords: population, inhabitants, households, families, single-parent families, demographics,
  census, neighborhood, evacuation headcount. Donne une synthèse commune + détail par IRIS pour
  une commune (nom ou code INSEE) ou des coordonnées lat/lon en France (métropole, DOM, COM).
allowed-tools: Bash(python3 *)
---

# demographie-iris

Population, ménages et familles par **IRIS** (quartiers infra-communaux de ~2 000 hab.) pour une
commune française, à partir de la base INSEE **« Couples - Familles - Ménages »** par IRIS. Sert à
dimensionner une évacuation / un hébergement et à repérer les foyers vulnérables (familles
monoparentales).

## Quand l'utiliser

L'utilisateur veut savoir combien de personnes / ménages / familles vivent dans une commune et
comment ils se répartissent par quartier, ou où se concentrent les familles monoparentales.

## Comment lancer

Localisation **obligatoire** (aucun lieu par défaut) : `--commune` (nom ou code INSEE) **ou**
`--lat`/`--lon` (géocodage inverse vers la commune).

```bash
# Par commune (nom ou code INSEE)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès"
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007

# Par coordonnées
python3 ${CLAUDE_SKILL_DIR}/main.py --lat 44.125 --lon 4.0905

# Détail par IRIS (couples avec/sans enfants, type d'IRIS) ; collectivités d'outre-mer
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007 --detail
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 97501 --zone com
```

Options : `--zone` (défaut `auto` : essaie tous les fichiers déclarés par le registre jusqu'à
trouver la commune ; ou une zone précise, ex. `metropole`/`com`), `--detail` (ajoute par IRIS :
couples avec/sans enfants, type d'IRIS), `--top N` (limite la liste IRIS aux N plus peuplés,
défaut 20, `--top 0` = liste complète ; les totaux commune restent calculés sur **tous** les IRIS),
`--timeout` s (défaut 60).

Le **1er appel** télécharge le CSV INSEE (~20 Mo zippé pour la métropole) puis le met en cache ; les
appels suivants sont immédiats. La couverture géographique couvre métropole, DOM et COM : une commune
absente du millésime (ex. Mayotte en 2022) renvoie une erreur explicite. Le champ
`dataset.maj_skill_disponible` signale qu'un millésime plus récent existe mais exige une version de
skill supérieure (le skill continue alors avec le dernier compatible).

## Sortie

JSON sur stdout : `{ lieu, dataset, demographie, skill }`. Le bloc `skill` (métadonnée de
version/mise à jour du skill) est toujours présent et sans incidence sur la décision.
- `dataset` : provenance (millésime, zone, url, urlhash, depuis_cache, registre, drapeau de MAJ).
- `demographie.commune` : `population`, `menages_total`, `familles_total`, `monoparentales_total`
  (somme **complète**), `part_monoparentales_pct` (indicateur de vulnérabilité) +
  `part_monoparentales_base` (`{monoparentales, familles}` réellement utilisés pour ce % — seuls
  les IRIS où les deux sont chiffrées, donc `pct = base.monoparentales / base.familles`, ce qui
  peut différer de `monoparentales_total`), `iris_count` (total d'IRIS trouvés).
- `demographie.iris[]` : par quartier `code, libelle, population, menages, familles,
  monoparentales` (trié par population décroissante, limité au top-N via `--top`).
- `demographie.iris_tronque` : `true` si la liste a été limitée (`iris_count` > `len(iris)`) ;
  jamais de troncature silencieuse.

Reformuler ensuite en langage naturel. Une mesure absente vaut une **chaîne explicative** (ex.
`"indisponible : donnée soumise au secret statistique"`) — jamais un `null` ambigu ; vérifier le
type avant tout calcul. Une commune introuvable / hors couverture renvoie une erreur (stderr +
code ≠ 0, ou champ `error` dans `demographie`).
