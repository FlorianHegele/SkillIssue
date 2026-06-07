# API — skill `vulnerabilite-bpe`

Détails vérifiés (5 juin 2026). Source : **fichier-détail BPE en CSV** (pas d'API dédiée).
Le fichier-détail contient déjà `LATITUDE`/`LONGITUDE` WGS84 → pas de reprojection nécessaire.

---

## 1. Téléchargement BPE (sans clé)

| Millésime | Page bases en ligne |
| --------- | ------------------- |
| BPE 2024 (récent) | `insee.fr/fr/metadonnees/source/operation/s2216/bases-donnees-ligne` |
| BPE 2023 | `insee.fr/fr/metadonnees/source/operation/s2155/bases-donnees-ligne` |

- Série : `insee.fr/fr/metadonnees/source/serie/s1161` · Miroir : `data.gouv.fr/datasets/base-permanente-des-equipements-1`.
- **Fichier-détail = uniquement CSV**, actualisé mensuellement. Diffusion **nationale**
  (pas de téléchargement par département) → télécharger puis **filtrer sur `DEPCOM`** + `TYPEQU`.
  Vérifié 6 juin 2026 (BPE 2024) : `BPE24.zip` ≈ **165 Mo zippé / ~1,4 Go décompressé**
  (~2,8 M lignes, 89 colonnes), ~229 types d'équipements. URL directe :
  `insee.fr/fr/statistiques/fichier/8217525/BPE24.zip`. La colonne `NOMRS` (nom/raison sociale)
  est renseignée pour les écoles et la santé → exploitable comme nom d'établissement.

## 2. Structure du fichier-détail

Dictionnaire 2024 : `insee.fr/fr/metadonnees/source/fichier/BPE24_anonymisee_dictionnaire_variables.html`
- `TYPEQU` : type d'équipement · `DEPCOM` : code dépt+commune (ex. Alès `30007`).
- `LATITUDE`/`LONGITUDE` : **degrés décimaux WGS84** (directement exploitables).
- `LAMBERT_X`/`LAMBERT_Y` : Lambert 93 (EPSG 2154) en métropole (alternative).
- Qualité géoloc : `QUALITE_XY`, `QUALITE_GEOLOC`, `TR_DIST_PRECISION` (<100m / 100-500m / ≥500m).

## 3. Codes TYPEQU à filtrer

Liste hiérarchisée : `insee.fr/fr/metadonnees/source/fichier/BPE23_liste_hierarchisee_TYPEQU.html`
- **Écoles (domaine C — Enseignement)** : `C107` maternelle · `C108` élémentaire/primaire ·
  `C201` collège · `C301` lycée général/techno · `C302` lycée professionnel.
  (⚠ pas de code `C101` ; la nomenclature commence à C1xx.)
- **Santé (domaine D, plage D106–D113 ciblée)** : `D106` urgences · `D107` maternité ·
  `D108` centre de santé · `D109` structure psychiatrique en ambulatoire · `D110` centre de
  médecine préventive · `D111` dialyse · `D112` hospitalisation à domicile · `D113` maison de
  santé pluridisciplinaire. (CH/CHU catégorisés via les sous-codes D1xx.)
- ⚠ **Les codes ET libellés TYPEQU évoluent chaque millésime** → récupérer le dictionnaire/liste
  de l'année utilisée avant de figer les filtres. Côté skill, `SANTE`/`ECOLES` (main.py) figent
  les libellés du millésime courant ; un code inconnu retombe sur un libellé de repli par domaine
  (jamais d'erreur), mais un libellé codé en dur pourrait dériver — revérifier à chaque bascule.

## 4. Alternatives / compléments (sans clé)

- **Écoles** : annuaire UAI Éducation Nationale (data.gouv, géocodé).
- **OSM/Overpass** : `amenity=hospital|clinic|school` + `out center` ; extrait hôpitaux OSM :
  `data.gouv.fr/datasets/localisation-des-hopitaux-dans-openstreetmap`. Couverture variable.

---

## Stratégie d'implémentation

1. Télécharger le CSV BPE au 1er appel → cache local (non versionné).
2. Filtrer `DEPCOM` (commune) + `TYPEQU ∈ {écoles, santé}`.
3. Retourner liste avec coordonnées + qualité géoloc, séparée écoles / établissements de santé.
4. Échantillon de test (équipements du Gard) versionné pour le hors-ligne.

## Sortie attendue (synthèse JSON)

`{ commune, ecoles: [{type, nom?, lat, lon, qualite}], sante: [{type, lat, lon, qualite}] }`
