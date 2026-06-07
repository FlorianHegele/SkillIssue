# API — skill `alerte-crue`

Détails d'API **vérifiés en live le 5 juin 2026**. Toutes sans clé. Trois temps de l'alerte :
vigilance officielle (Vigicrues) · observation temps réel (Hub'Eau) · prévision pluie (OpenMeteo).
Re-vérifier avant de figer le code : ces API évoluent (la v1 Hub'Eau a été coupée fin mai 2025).

---

## 1. Vigicrues — niveau de vigilance par tronçon (couleur)

- Base : `https://www.vigicrues.gouv.fr/services/` (API documentée v1.1 : `.../services/v1.1`).
- **Sans clé.**
- Endpoint couleur (renvoie GeoJSON) :
  `https://www.vigicrues.gouv.fr/services/InfoVigiCru.geojson`
  (⚠ l'ancienne forme `…/services/1/InfoVigiCru.geojson` renvoie une **redirection 302** vers
  celle-ci — suivre les redirections, vérifié le 06/06/2026).
  → `FeatureCollection` ; chaque tronçon a une géométrie **MultiLineString** (lignes, pas
  polygones → matcher le tronçon le **plus proche**, pas point-dans-polygone) + properties :
  ```json
  {"CdTCC":"TCC22","id":"764","lbentcru":"Golo aval","stentcru":"Validé","NivInfViCr":1}
  ```
- **Mapping `NivInfViCr` (convention Sandre)** : `1=vert  2=jaune  3=orange  4=rouge`.
  Champ numérique → convertir en couleur côté script. Validité d'une couleur : 24 h.
- Autres endpoints utiles : `TerEntVigiCru.json` (territoires), `TronEntVigiCru.json`
  (tronçons + stations, ex. `?CdEntVigiCru=8&TypEntVigiCru=5`), `observations.json`, `prevision.json`.
- Open data miroir : data.gouv.fr « Tronçons de cours d'eau Vigicrues simplifiés avec niveau de vigilance ».
- Note : Vigicrues = la **couleur d'alerte** ; Hub'Eau = la **mesure**. Complémentaires.

## 2. Hub'Eau — hydrométrie temps réel (hauteur / débit)

- Base : `https://hubeau.eaufrance.fr/api/v2/hydrometrie/` — **utiliser `/api/v2/`** (v1 coupée).
- **Sans clé.** Doc Swagger : `.../hydrometrie/api-docs`.
- Endpoints :
  - `/referentiel/stations` et `/referentiel/sites` — métadonnées + coordonnées des stations.
  - `/observations_tr` — temps réel : `grandeur_hydro=H` (hauteur, **mm**) ou `Q` (débit, **l/s**),
    pas 5–60 min, historique glissant 1 mois, maj toutes les 5 min.
  - `/obs_elab` — débits moyens journaliers/mensuels, historique long.
- Exemple testé (JSON réel) :
  ```
  https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr?code_entite=Y251002001&size=3&grandeur_hydro=H&fields=code_station,date_obs,resultat_obs
  ```
  Réponse : `{"api_version":"2.0.1", count, next, "data":[{"code_station":"Y251002001","date_obs":"2026-06-05T15:45:00Z","resultat_obs":-360.0}]}`.
- Formats : `format=json|geojson|csv`. Pagination max 20 000 ; URL max 2083 car.
  ⚠ Hub'Eau renvoie **HTTP 206** (Partial Content) en pagination : c'est une réponse JSON
  **valide**, à accepter au même titre que 200 (vérifié le 06/06/2026).
- Workflow type pour Alès : chercher les stations par bbox/commune via `/referentiel/stations`,
  puis interroger `/observations_tr` sur les `code_station` trouvés.

## 3. OpenMeteo — prévision de précipitations (donnée AROME)

- Endpoint : `https://api.open-meteo.com/v1/forecast` — **sans clé** (CC BY 4.0, non commercial).
- Pour la France : modèle Météo-France AROME ; `models=meteofrance_arome_france_hd` (~0.01° ≈ 1,5 km).
  Pas 15 min possible via `&minutely_15=precipitation`.
- Variables pluie : `precipitation` (pluie+averses+neige), `rain`, `showers` ; daily `precipitation_sum`. Unités **mm**.
- Exemple testé (JSON réel) :
  ```
  https://api.open-meteo.com/v1/forecast?latitude=44.13&longitude=4.08&hourly=precipitation,rain&daily=precipitation_sum&models=meteofrance_arome_france_hd&timezone=Europe/Paris
  ```
  (coordonnées ≈ Alès). Réponse : `hourly_units.precipitation:"mm"` + tableaux horaires + `daily.precipitation_sum`.
- IDs modèles : `meteofrance_seamless`, `meteofrance_arpege_world/europe`, `meteofrance_arome_france`, `meteofrance_arome_france_hd`.

---

## Robustesse / pièges

- Toujours **valider le content-type JSON** avant de parser (Vigicrues/Hub'Eau peuvent renvoyer du HTML d'erreur).
- Gérer timeouts + retry/backoff ; coder un message clair si une station n'a pas de donnée récente.
- Hub'Eau : `resultat_obs` peut être négatif (référence locale du capteur) → ne pas interpréter brut sans le contexte station.
- Météo-France `portail-api` (token OAuth2) **écarté** : OpenMeteo sert déjà la donnée AROME sans clé.

## Sortie attendue du skill (synthèse JSON)

Pour une commune/lat-lon (localisation **obligatoire**, aucun repli ; Alès n'est qu'un *exemple* de doc, jamais un défaut applicatif) : `{ lieu, vigilance: {couleur, troncon, …}, hydro: [{station, hauteur_mm, debit_ls, date_hauteur, date_debit}], pluie: {cumul_prochaines_24h_mm, pic, creneaux[], heures_pluvieuses[]} }`. Forme exacte = `contract.schema.json`.
Claude reformule ensuite en phrase naturelle.
