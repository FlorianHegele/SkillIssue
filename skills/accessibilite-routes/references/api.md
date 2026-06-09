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
- Erreurs : `429` rate limit · `504` « dispatcher too busy » · `406`/HTML sous charge. Ces statuts
  sont **transitoires** (saturation d'instance publique, indépendante du poids de la requête : même
  `out count;` peut renvoyer 504). Le client HTTP les **retente** avec backoff exponentiel et
  remonte le vrai code HTTP (message « temporairement saturé, réessayer ») ; les 4xx définitifs
  (400/404/406) échouent vite, sans re-essai.
- **Piège `remark`** : un timeout / dépassement mémoire *côté serveur* renvoie **HTTP 200 + JSON
  valide** avec `{"elements": [], "remark": "runtime error: Query timed out…"}`. Le skill détecte
  ce `remark` d'erreur (`main._check_overpass_remark`) puis lève une erreur explicite — sinon une
  requête tronquée serait lue à tort comme « secteur sans aucun ouvrage ».
- **Repli miroir** : aucun par défaut. Les miroirs gratuits sont morts (kumi.systems), injoignables
  (private.coffee), suspendus (maps.mail.ru → 403) ou **régionaux** (overpass.osm.ch = Suisse, renvoie
  0 pour la France = faux secteur vide INTERDIT). Un miroir **global vérifié** peut être fourni via
  l'env `FLOOD_OVERPASS_MIRROR` : il est alors tenté une seule fois, en timeout court. On s'appuie
  sinon sur le retry du primaire (les 504/429 d'Overpass sont transitoires).
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

- `[out:json]` → `elements[]`. Ce skill utilise **`out tags center;`** = point représentatif
  (`center` pour les ways, `lat/lon` pour les nodes) + `tags`, léger. (`out geom;` ajouterait le
  tracé complet `[{lat,lon}…]` ; non utilisé — l'`osm_id` suffit à récupérer le tracé à la demande.)

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
        "nom", "highway", "lat", "lon", "distance_km", "tags" }
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

---

## Piste d'amélioration : extraits Geofabrik hors-ligne (plan B)

Le correctif de robustesse (retry exponentiel 504/429, retrait du miroir mort, remontée du vrai
code HTTP) garde une dépendance à une **instance Overpass publique qui sature par intermittence**.
Si cette fragilité redevient bloquante, la solution de fond — **non implémentée** — est d'abandonner
l'Overpass temps réel au profit d'**extraits OSM départementaux Geofabrik (`.pbf`)** téléchargés à
la demande + cache local, requêtés **hors-ligne** avec `pyosmium` ou `pyrosm`.

- **Pourquoi** : élimine la cause racine (plus de 504/429, plus de dépendance réseau au moment de
  la requête), reproductible pour le correcteur, toujours **sans clé**. La donnée recherchée
  (`ford`/`bridge`/`tunnel`/`layer`/`flood_prone`) n'existe que dans OSM — Google Maps & co. sont
  exclus (clé + CB obligatoires, CGU anti-extraction, et n'exposent pas ces tags).
- **Comment** : réutiliser le pattern déjà en place dans `demographie-iris` / `vulnerabilite-bpe`
  (`_common/dataset.py` : téléchargement à la demande + cache borné `MAX_CACHED_DATASETS`). Extrait
  départemental ≈ 50–200 Mo (ordre de grandeur du CSV IRIS ~21 Mo déjà accepté). Nouvelle dépendance
  `pyosmium`/`pyrosm` dans `requirements.txt`.
- **Coût** : nouveau chemin de code + ~100 Mo au 1er appel par département (puis cache).

Constat sur les instances publiques (9 juin 2026) : `overpass-api.de` seul global fiable mais
saturé par intermittence ; `kumi.systems` mort ; `private.coffee` injoignable ; `maps.mail.ru`
suspendu (403 depuis mars 2026) ; `overpass.osm.ch` régional (Suisse → 0 pour la France) ;
`geofabrik` payant.
