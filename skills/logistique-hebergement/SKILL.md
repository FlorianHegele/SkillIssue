---
name: logistique-hebergement
version: 1.0.0
description: >
  Trigger when user asks where to shelter or house flood victims around a French location — to plan
  emergency accommodation away from the flooded zone. Lists requisitionable sites (hotels, gyms /
  sports halls, schools, community centres) with an estimated, labelled accommodation capacity
  (number of sleeping places). Mots-clés FR : hébergement d'urgence, héberger les sinistrés,
  capacité d'accueil, mise à l'abri, relogement, réquisition, centre d'accueil, hôtel, gymnase,
  salle des fêtes, salle communale, école, couchages, lits, plan communal de sauvegarde. EN
  keywords: emergency shelter, sheltering, housing displaced people, accommodation capacity, beds,
  evacuation centre, hotel, gym, sports hall, school, community centre, OSM, Overpass. Donne les
  sites géolocalisés autour d'une commune (nom ou code INSEE) ou de coordonnées lat/lon en France,
  triés par capacité décroissante, avec rayon réglable.
allowed-tools: Bash(python3 *)
---

# logistique-hebergement

Recense, autour d'un lieu, les **sites où héberger les sinistrés** lors d'une crue, à partir
d'**OpenStreetMap** via **Overpass** (sans clé) : **hôtels**, **gymnases / salles de sport**,
**écoles**, **salles communales**. Pour chaque site, une **capacité d'accueil** (couchages) :
valeur OSM si un tag explicite existe, sinon **estimée et étiquetée** (hôtel ≈ chambres × 2 ou
défaut par étoiles ; gymnase/école/salle ≈ emprise au sol / 4 m² par couchage).

> ⚠ **Capacités très peu renseignées dans OSM** : l'essentiel est estimé (ordres de grandeur à
> confirmer sur place). Ces lieux sont des **candidats**, pas des abris validés — les centres
> d'hébergement officiels relèvent des Plans Communaux de Sauvegarde, rarement publiés. Ce rappel
> est inclus dans le champ `note` de la sortie.

## Calcul de l'estimation des places

Le nombre de couchages est **rarement une donnée, presque toujours une estimation grossière**. Le
champ `capacite_methode` dit, pour chaque site, comment le chiffre a été obtenu :

- **`capacite_source: "osm"`** — tiré d'un tag explicite de couchages (`beds`, `capacity:beds`,
  `capacity:persons`). Le plus fiable, mais **très rare** dans OSM.
- **`capacite_source: "estimee"`, hôtels** — `rooms × 2`, ou à défaut un **forfait par classe
  d'étoiles** (heuristique : le nombre d'étoiles ne dit rien de la taille réelle de l'hôtel → chiffre
  spéculatif). `capacity` nu est traité comme estimation (ambigu sur OSM : parfois des places de
  parking).
- **`capacite_source: "estimee"`, gymnases/écoles/salles** — `emprise au sol / 4 m²`. Fiable **si**
  l'emprise est un bâtiment (`capacite_methode` = « surface bâtie ») ; **majorant** si c'est une
  **parcelle** (« surface parcelle… ») car le polygone OSM englobe souvent cours, stades et parkings.
  L'emprise ne tient pas compte des étages, des cloisons ni du mobilier.
- **`capacite_source: "indisponible"`** — ni tag ni surface exploitable : aucun chiffre inventé, une
  chaîne explique pourquoi.

⚠ **`capacite_fiable_totale` ne veut pas dire « certain »** : cela signifie seulement « hors majorant
parcelle ». Ce total inclut des estimations hôtelières spéculatives (rooms×2, défaut par étoiles). Les
capacités sur parcelle, potentiellement énormes, sont isolées dans `capacite_majorant_parcelles`. Aucun
de ces chiffres ne remplace une vérification de terrain.

## Quand l'utiliser

L'utilisateur veut savoir **où mettre les sinistrés à l'abri** et **combien de personnes** chaque
lieu peut accueillir, à l'écart de la zone inondée. Prendre un point **hors** de la zone sinistrée
et chercher autour. Pour les **axes d'accès / franchissements** vers ces sites, voir
`accessibilite-routes` ; pour les **équipements à évacuer** (écoles, santé comme enjeux), voir
`vulnerabilite-bpe`.

## Comment lancer

> **Premier lancement** : si les dépendances Python manquent, l'environnement (`.venv` local au
> plugin) est créé et installé automatiquement, sans toucher au Python système (PEP 668-safe).
> Les lancements suivants sont immédiats. Désactivable via `FLOOD_NO_BOOTSTRAP=1`.

Localisation **obligatoire** (aucun lieu par défaut) : `--commune` (nom ou code INSEE) **ou**
`--lat`/`--lon`.

```bash
# Par commune (nom ou code INSEE)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès"
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007

# Par coordonnées, rayon élargi, liste limitée aux 20 plus grandes capacités
python3 ${CLAUDE_SKILL_DIR}/main.py --lat 44.13 --lon 4.08 --radius-m 3000 --limit 20

# Avec le tracé complet de chaque site
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès" --geometry
```

Options : `--radius-m` mètres (défaut 2000, max 5000 — toujours scopé), `--limit` (défaut 100,
borne la liste ; le résumé compte tous les sites trouvés), `--geometry` (ajoute le tracé complet
en plus du point représentatif), `--timeout` s (défaut 25).

## Sortie

JSON sur stdout : `{ lieu, hebergement, skill }`. Le bloc `skill` (métadonnée de version/mise à
jour du skill) est toujours présent et sans incidence sur la décision.
- `hebergement.rayon_m` : rayon utilisé.
- `hebergement.resume` : compteurs `sites_total`, `hotels`, `gymnases`, `ecoles`,
  `salles_communales`, plus deux totaux de couchages distincts — `capacite_fiable_totale` (tags OSM
  + empreintes bâties) et `capacite_majorant_parcelles` (estimations sur parcelle, **majorant** :
  séparées pour ne pas gonfler le total) — avec `sites_capacite_majorant` et `sites_sans_capacite`
  (sur **tous** les sites trouvés, même au-delà de `--limit`). ⚠ Un même lieu peut être cartographié
  à la fois en node et en way dans OSM (dédup uniquement par `osm_id`) : compteurs et totaux peuvent
  surévaluer légèrement.
- `hebergement.sites[]` : par site `osm_id`, `type` (hôtel/gymnase/école/salle_communale), `nom`,
  `lat`, `lon`, `distance_km`, `capacite` (couchages), `capacite_source`
  (`osm`/`estimee`/`indisponible`), `capacite_methode` (traçabilité), `surface_m2`, `tags`
  (sous-ensemble pertinent) ; **trié par capacité décroissante** ; `geometry` seulement avec
  `--geometry`.
- `hebergement.note` : rappel sur la complétude OSM et le statut « candidat ».

Reformuler ensuite en langage naturel. Une mesure absente vaut une **chaîne explicative** (ex.
`capacite = "indisponible : aucune donnée de capacité ni surface exploitable"`) — jamais un `null`
ambigu ; vérifier le type avant tout calcul. Une commune introuvable / hors France, ou Overpass
indisponible (avec miroir), renvoie une erreur (stderr + code ≠ 0) ; un secteur sans site renvoie
des compteurs à 0 et une liste vide (réponse valide, code 0).
