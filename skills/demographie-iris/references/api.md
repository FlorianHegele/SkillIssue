# API — skill `demographie-iris`

Détails vérifiés (5 juin 2026). Source retenue : **téléchargement dataset INSEE CSV sans clé**
(plus exhaustif/granulaire que l'API Melodi pour l'IRIS). Granularité = IRIS (infra-communal).

---

## 1. Datasets INSEE par IRIS — téléchargement direct (RETENU)

Sans clé, sans inscription. CSV + XLSX. **Préférer le millésime 2022** (corrections sur 2021).

| Jeu | Millésime 2022 | Millésime 2021 |
| --- | -------------- | -------------- |
| Couples-Familles-Ménages (CFM) | `insee.fr/fr/statistiques/8647008` | `…/8268828` |
| Population | `insee.fr/fr/statistiques/8647014` | `…/8268806` |
| Logement | — | `…/8268838` |

- Miroir : `data.gouv.fr/datasets/bases-de-donnees-et-fichiers-details-du-recensement-de-la-population`.
- Taille (CFM 2021) : CSV ~**21 Mo**, XLSX ~44 Mo (France hors Mayotte) ; ~**15 500 IRIS** (dont ~750 DOM).
- **Colonnes d'identification** : `IRIS` (code IRIS), `COM`, `LIBCOM`, `GRD_QUART`, `TYP_IRIS`.
- **Variables clés** (préfixe millésime, ex. `C22_`) :
  - `C22_MEN` = ménages · `C22_FAM` = familles · `C22_PMEN` = population des ménages
  - `C22_MENCOUPAENF` = couples avec enfant(s) · `C22_MENCOUPSENF` = couples sans enfant
  - `C22_MENFAMMONO` = familles monoparentales
  - Le jeu « Population » donne population par sexe/âge/CSP par IRIS.

## 2. geo.api.gouv.fr — communes (complément, sans clé)

- `https://geo.api.gouv.fr/communes?codeDepartement=30&fields=nom,code,population,centre`
- `https://geo.api.gouv.fr/departements/30/communes`
- Donne population **au niveau commune** (pas IRIS) + centroïde + contours (WGS-84, JSON/GeoJSON).
- Usage : cadrage commune, centroïdes, mapping commune↔IRIS. À combiner avec la Base IRIS.

## 3. API INSEE Melodi — plan B (sans clé, limité)

- `https://api.insee.fr/melodi/catalog/all` répond en JSON sans authentification (**30 appels/min anonyme**).
- Portail : `https://portail-api.insee.fr/` (l'ancien `api.insee.fr` fermé le 10 sept. 2025 ; DDL déprécié → Melodi).
- Surface technique plus lourde (pagination, codes de jeux) et couverture IRIS en consolidation
  → **écarté au profit du CSV** ; à garder seulement pour des requêtes ciblées à jour.

---

## Stratégie d'implémentation

1. Télécharger le CSV CFM 2022 (+ Population si besoin) au 1er appel → **cache local** (non versionné).
2. Filtrer sur `COM` (code commune, ex. Alès `30007`) ou liste d'IRIS.
3. Agréger/retourner par IRIS : population, ménages, familles, monoparentales.
4. Petit échantillon de test (quelques IRIS du Gard) versionné pour le mode hors-ligne.

## Sortie attendue (synthèse JSON)

`{ commune: {code, nom, population}, iris: [{code, libelle, population, menages, familles, monoparentales}] }`
