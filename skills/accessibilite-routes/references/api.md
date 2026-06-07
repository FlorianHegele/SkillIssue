# API — skill `accessibilite-routes`

Détails vérifiés en live (5 juin 2026) sur `overpass-api.de`. Sans clé. OSM via Overpass QL.

---

## API Overpass

- Endpoint : `https://overpass-api.de/api/interpreter` (paramètre `data=` contenant le QL).
  Ce skill l'appelle en **GET** (`?data=…`) : le QL reste court (~600 c, la géométrie est dans la
  réponse, pas la requête) donc aucun risque de dépassement de longueur d'URL. Passer en **POST**
  seulement si l'on génère un QL long.
- Statut/slots : `https://overpass-api.de/api/status` (vérifier avant rafale).
- **Sans clé** (identification par IP). **2 requêtes concurrentes max / IP** ; fair-use ~**10 000 req/j**,
  < ~1 Go/j. Timeout défaut 180 s, RAM défaut 512 MiB.
- Erreurs : `429` rate limit · `504` ressources dépassées · `406`/HTML sous charge.
- Mirror de repli : `https://overpass.kumi.systems/api/interpreter`.
- **Pièges** : éviter les noms accentués dans `area[...]` (→ 406) ; préférer **bbox `(s,o,n,e)`** ou
  **`(around:rayon_m,lat,lon)`**. `out count;` correct (pas `.x out count;`). Ne jamais scanner à
  l'échelle nationale sur clés non indexées (`ford`, `flood_prone` → timeout) : toujours scoper.

## Requête de base (testée, renvoie géométrie + tags)

```overpassql
[out:json][timeout:25];
(
  way["highway"](around:1500,44.1280,4.0820);
  node["ford"](around:1500,44.1280,4.0820);
);
out geom;
>;
out skel qt;
```
(rayon 1500 m près d'Alès — testé : ~2 022 ways, dont 27 `bridge`, 15 `tunnel`, 46 `layer`.)

## Requête ciblée « vulnérabilité eau » (testée)

```overpassql
[out:json][timeout:25];
(
  way["highway"](around:800,44.1380,4.0810);
  node["ford"]["ford"!="no"](around:800,44.1380,4.0810);
  way["ford"]["ford"!="no"](around:800,44.1380,4.0810);
  way["bridge"="yes"](around:800,44.1380,4.0810);
);
out geom;
```

## Tags pertinents pour la vulnérabilité à l'inondation

- `highway=*` (réseau) · `bridge=yes` (+ `layer` positif = franchissement) ·
  `tunnel=yes|culvert|flooded` · `layer` négatif = passage inférieur / point bas ·
  `ford=yes|stepping_stones` (gué, **node OU way**) + `intermittent=yes` ·
  `flood_prone=yes` (tag dédié) · `hazard=flooding` · `ele=*` (altitude).
- **Jugement** : points critiques = gués, ponts + accès bas, tunnels/passages inférieurs (`layer` négatif).

## Sortie / extraction

- `[out:json]` → `elements[]`. `out geom;` ajoute à chaque way `geometry` (`[{lat,lon}…]`) + `tags`.
  `out tags center;` = point représentatif + tags (plus léger). `>; out skel qt;` = nodes référencés.

---

## ⚠ Réserve de complétude (importante)

- Réseau/ponts/tunnels/gués : **bien cartographiés en France** (utilisables directement).
- `flood_prone` (~32 600 dans le monde) et `hazard=flooding` (~1 000) : **trop rares pour être source primaire**.
  Leur absence ≠ « non vulnérable ». Pour un vrai jugement d'aléa → **croiser avec Géorisques / data.gouv.fr**
  (« Risque d'inondation », zonages TRI). OSM = réseau + ouvrages ; l'État = l'aléa.

## Sortie réelle du skill (contrat)

Voir `contract.py` / `contract.schema.json`. Forme : `{ lieu, accessibilite }`.

```jsonc
{
  "lieu": { "commune", "code_insee", "lat", "lon" },
  "accessibilite": {
    "rayon_m": 1500,
    "resume": { "ouvrages_total", "gues", "ponts", "tunnels",
                "passages_inferieurs", "zones_inondables" },   // compté sur TOUS les ouvrages
    "ouvrages_a_risque": [                                      // trié par distance ; --limit borne
      { "osm_id": "way/123", "kind": "gué|pont|tunnel|passage_inférieur|zone_inondable",
        "nom", "highway", "lat", "lon", "distance_km", "tags",
        "geometry": [ {lat,lon}… ] }                           // uniquement avec --geometry
    ],
    "note": "OSM ≠ aléa : croiser avec Géorisques (zonages TRI)."
  }
}
```

- Décision = priorité de `kind` : `gué` > `tunnel` > `pont` > `passage_inférieur` (`layer` < 0) >
  `zone_inondable` (flood_prone/hazard).
- **Filtres appliqués côté Overpass** (cf. `main.build_query`) : voies **non carrossables**
  exclues (`footway|steps|path|cycleway|pedestrian|bridleway|corridor` — skill = accès véhicules /
  secours) ; `layer` scopé aux **valeurs négatives** ; `flood_prone`/`hazard` restreints aux
  **voies** (pas de polygones de zone). Les gués (souvent un node sans `highway`) ne sont pas
  filtrés ainsi.
- Mesure absente (way sans position) = **chaîne explicative**, jamais `null` ; rejetée en fin de
  tri. `--radius-m` borné à 5000 m (scoping fair-use). `--limit` >= 0 (0 = résumé seul).
