# API — skill `logistique-hebergement`

Détails vérifiés en live (5 juin 2026) sur `overpass-api.de`. Sans clé. OSM via Overpass QL.
OSM = seule source nationale homogène de lieux candidats (PCS communaux rarement en open data).

Voir `../../accessibilite-routes/references/api.md` pour les règles générales Overpass
(endpoint, limites 2 req concurrentes / fair-use, erreurs 429/504/406, mirror, validation content-type).

---

## Requête de base (testée)

```overpassql
[out:json][timeout:25];
(
  nwr["tourism"="hotel"](around:2000,44.13,4.08);
  nwr["leisure"="sports_centre"](around:2000,44.13,4.08);
  nwr["leisure"="fitness_centre"](around:2000,44.13,4.08);
  nwr["building"="sports_hall"](around:2000,44.13,4.08);
  nwr["amenity"="school"](around:2000,44.13,4.08);
  nwr["amenity"="community_centre"](around:2000,44.13,4.08);
);
out geom;
```
`nwr` = nodes+ways+relations en une passe. **`out geom;`** (et non `out tags center;`) : on a besoin
du tracé pour calculer l'emprise au sol (base de l'estimation de capacité des gymnases/écoles/salles) ;
le tracé sert au calcul puis n'est exposé dans la sortie qu'avec `--geometry`. `around:` sur **chaque**
sous-requête (jamais de scan national). Test initial en bbox sur Nîmes : 223 éléments → 122 écoles,
49 sports_centre, 40 hôtels, 12 community_centre (avant ajout de fitness_centre / building=sports_hall).

## Lieux candidats (tags)

- Hôtels : `tourism=hotel`.
- Gymnases / salles de sport : `leisure=sports_centre`, `building=sports_hall`, `leisure=fitness_centre`.
- Écoles : `amenity=school`. Salles communales : `amenity=community_centre`.
- Utiliser `around:`/bbox **hors zone sinistrée** (le skill prend la zone inondée en entrée et cherche autour, à l'écart).

## ⚠ Capacité d'accueil — très mauvaise complétude (mesurée)

Sur 40 hôtels de Nîmes (live) : `rooms` 7/40 · `stars` 13/40 · `capacity:rooms` 0/40 · `beds`/`capacity:beds` 0–1/40.
(`capacity:rooms` n'existe que ~72 fois dans le monde entier.)

→ **Ne pas se fier aux tags de capacité.** Stratégie :
- utiliser `rooms`/`beds`/`capacity` **quand présents** ;
- sinon **estimer** : hôtel ≈ `rooms`×2 (ou défaut par classe d'étoiles) ; gymnase ≈ surface au sol / ~4 m²
  par couchage (surface via `out geom` sur le way) ;
- **toujours étiqueter** la capacité comme « valeur OSM » vs « estimation ».

## Sources officielles FR (complément, fragmenté)

- `data.gouv.fr` : « Sites d'hébergement d'urgence » (ex. Montpellier), PCS communaux — **périmètre municipal**,
  hétérogène, pas de couverture nationale. API sans clé : `https://www.data.gouv.fr/api/1/datasets/?q=...`.
- Les centres d'hébergement d'urgence relèvent des **Plans Communaux de Sauvegarde** (rarement publiés).

---

## Sortie

Contrat réel = `../contract.py` + `../contract.schema.json` (fait foi). En résumé :
`{ lieu, hebergement: { rayon_m, resume, sites: [{type, nom, lat, lon, distance_km, capacite,
capacite_source: "osm"|"estimee"|"indisponible", capacite_methode, surface_m2, tags}], note } }`,
sites triés par capacité décroissante. Le `resume` sépare `capacite_fiable_totale` (tags OSM +
empreintes bâties) de `capacite_majorant_parcelles` (estimations sur parcelle = majorant), pour ne
pas additionner des couchages fantômes (cf. piège des polygones de parcelle ci-dessus).
