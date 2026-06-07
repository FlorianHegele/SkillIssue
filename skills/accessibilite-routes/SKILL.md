---
name: accessibilite-routes
description: >
  Trigger when user asks which roads or crossings around a French location risk being cut off by
  a flood — to plan access and evacuation routes. Identifies flood-vulnerable structures: fords
  (roads crossing watercourses at grade), bridges, tunnels, underpasses / low points (negative
  layer), and the rare OSM flood tags. Mots-clés FR : accessibilité routière, routes coupées,
  axes d'accès, itinéraire d'évacuation, franchissement, gué, pont, tunnel, passage inférieur,
  point bas, voie submersible, secours, OSM. EN keywords: road access, flooded roads, cut-off
  roads, access routes, evacuation route, river crossing, ford, bridge, tunnel, underpass, low
  point, Overpass, OSM. Donne les ouvrages à risque géolocalisés autour d'une commune (nom ou
  code INSEE) ou de coordonnées lat/lon en France, triés par distance, avec rayon réglable.
allowed-tools: Bash(python3 *)
---

# accessibilite-routes

Repère, autour d'un lieu, les **points de franchissement routiers susceptibles d'être coupés par
l'eau** lors d'une crue, à partir d'**OpenStreetMap** via **Overpass** (sans clé) : **gués**
(routes traversant un cours d'eau à niveau), **ponts**, **tunnels**, **passages inférieurs**
(points bas en `layer` négatif) et les rares tags d'aléa OSM (`flood_prone`, `hazard=flooding`).
Sert à anticiper les accès coupés et à planifier les itinéraires d'évacuation / de secours.

> ⚠ **OSM cartographie le réseau et les ouvrages, pas l'aléa.** L'absence de gué/pont vulnérable
> ne veut pas dire « zone sûre ». Pour un vrai jugement d'aléa, croiser avec **Géorisques**
> (zonages TRI, « Risque d'inondation »). Ce rappel est inclus dans le champ `note` de la sortie.

## Quand l'utiliser

L'utilisateur veut savoir **par où on accède** à un secteur et **quels franchissements risquent
d'être coupés** : combien de gués/ponts/tunnels, où, à quelle distance, sur quelle voie. Pour les
**équipements à évacuer** (écoles, santé), voir `vulnerabilite-bpe` ; pour l'**hébergement** des
sinistrés, `logistique-hebergement`.

## Comment lancer

Localisation **obligatoire** (aucun lieu par défaut) : `--commune` (nom ou code INSEE) **ou**
`--lat`/`--lon`.

```bash
# Par commune (nom ou code INSEE)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès"
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007

# Par coordonnées, rayon réduit, liste limitée
python3 ${CLAUDE_SKILL_DIR}/main.py --lat 44.13 --lon 4.08 --radius-m 800 --limit 20

# Avec le tracé complet de chaque ouvrage
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès" --geometry
```

Options : `--radius-m` mètres (défaut 1500, max 5000 — toujours scopé), `--limit` (défaut 100,
borne la liste ; le résumé compte tous les ouvrages trouvés), `--geometry` (ajoute le tracé
complet en plus du point représentatif), `--timeout` s (défaut 25).

## Sortie

JSON sur stdout : `{ lieu, accessibilite }`.
- `accessibilite.rayon_m` : rayon utilisé.
- `accessibilite.resume` : compteurs `ouvrages_total`, `gues`, `ponts`, `tunnels`,
  `passages_inferieurs`, `zones_inondables` (sur **tous** les ouvrages trouvés, même au-delà de
  `--limit`).
- `accessibilite.ouvrages_a_risque[]` : par ouvrage `osm_id`, `kind`
  (gué/pont/tunnel/passage_inférieur/zone_inondable), `nom` (name ou ref de la voie), `highway`,
  `lat`, `lon`, `distance_km` (trié par distance croissante), `tags` (sous-ensemble pertinent) ;
  `geometry` seulement avec `--geometry`.
- `accessibilite.note` : rappel « OSM ≠ aléa ».

Reformuler ensuite en langage naturel. Une mesure absente vaut une **chaîne explicative** (ex.
`"indisponible : position absente de la réponse Overpass"`) — jamais un `null` ambigu ; vérifier
le type avant tout calcul. Une commune introuvable / hors France, ou Overpass indisponible (avec
miroir), renvoie une erreur (stderr + code ≠ 0) ; un secteur sans ouvrage renvoie des compteurs à
0 et une liste vide (réponse valide, code 0).

Contrat de sortie (défini en amont) : `contract.py` (dataclasses typées) + `contract.schema.json`
(validé hors-ligne par `tests/test_contract.py`). Infra commune : `skills/_common/`.

Détails Overpass (endpoint, miroir, limites, requêtes testées, tags) : voir `references/api.md`.
