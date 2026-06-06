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
  nwr["tourism"="hotel"](43.78,4.30,43.88,4.40);
  nwr["leisure"="sports_centre"](43.78,4.30,43.88,4.40);
  nwr["amenity"="school"](43.78,4.30,43.88,4.40);
  nwr["amenity"="community_centre"](43.78,4.30,43.88,4.40);
);
out tags center;
```
(bbox sur Nîmes — testé : 223 éléments → 122 écoles, 49 sports_centre, 40 hôtels, 12 community_centre.)
`nwr` = nodes+ways+relations en une passe ; `out tags center;` = tags + point central.

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

## Sortie attendue (synthèse JSON)

`{ zone_sinistree: {lat, lon}, rayon_recherche_m, sites: [{type, nom?, lat, lon, capacite, capacite_source: "osm"|"estimee"}] }`
