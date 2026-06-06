# CLAUDE.md — Plugin Claude Code « flood-response »

Plugin de **skills** (jamais de MCP) pour l'aide à la décision lors d'une crue majeure.
Projet académique IUT NFC. Rendu : repo GitHub public → `guyeux@gmail.com`, **avant le 12 juin 2026**.

## Règles d'architecture d'un skill

Un skill = dossier `skills/<nom>/` avec `SKILL.md` + `main.py` + `references/` (chargé à la demande).

**`SKILL.md`** : frontmatter (`name`, `description`, `allowed-tools: Bash(python3 *)`) + body court (quand l'utiliser, comment lancer via `python3 ${CLAUDE_SKILL_DIR}/main.py …`).

**`description`** (seul levier de routage, <200 mots) : commence par « Trigger when user asks… », mots-clés métier FR+EN + abréviations (BPE, IRIS, INSEE), ne répète pas `name`.

**`main.py`** : Python ordinaire, testable seul (argparse, pas de serveur), **sortie JSON** (`ensure_ascii=False`), erreurs sur stderr + code retour ≠ 0. Deps dans `requirements.txt`.

**Réduction tokens** : description courte ; tout détail (doc API, exemples, erreurs) dans `references/` ; gros contenus (CSV/Parquet, configs longues) lus par le script, **jamais inline**.

## Décisions arrêtées

- **100 % API sans clé** (vérifié live 5 juin 2026 ; qualité ≥ API à clé, et testable par le correcteur sans inscription ; pas de fallback). Météo-France→OpenMeteo, INSEE Melodi→datasets CSV.
- **Zone démo : Alès / Gard (30)**.
- **Datasets non versionnés** (CSV IRIS ~21 Mo, BPE) : téléchargement à la demande + cache local + échantillon de test hors-ligne.
- Cohérence inter-skills : pivot = code commune INSEE + lat/lon.

## Les 5 skills + endpoints vérifiés

| Skill | Endpoint(s) |
| ----- | ----------- |
| `alerte-crue` | Vigicrues `…/services/1/InfoVigiCru.geojson` (`NivInfViCr` 1=vert 2=jaune 3=orange 4=rouge) · Hub'Eau `hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr` (H mm, Q l/s) · OpenMeteo `api.open-meteo.com/v1/forecast` (`models=meteofrance_arome_france_hd`) |
| `demographie-iris` | INSEE CFM 2022 `insee.fr/fr/statistiques/8647008` · Population `…/8647014` (vars `C22_MEN`, `C22_FAM`, `C22_MENFAMMONO`, `C22_PMEN` par IRIS) · `geo.api.gouv.fr/communes` |
| `vulnerabilite-bpe` | BPE CSV `insee.fr/fr/metadonnees/source/operation/s2216/bases-donnees-ligne` (`TYPEQU`, `DEPCOM`, `LATITUDE`/`LONGITUDE`). Écoles : C107/C108/C201/C301/C302. Santé : D106–D113. Complément : FINESS |
| `accessibilite-routes` | Overpass `overpass-api.de/api/interpreter` — `[out:json]; way["highway"](around:R,lat,lon); node["ford"](…); out geom;` |
| `logistique-hebergement` | Overpass — `nwr["tourism"="hotel"]`, `leisure=sports_centre`, `amenity=school/community_centre` ; capacité = estimation étiquetée |

## Robustesse

- **Overpass** : valider le content-type (HTML 406/429/504 sous charge), backoff/retry, scoper par `around:`/bbox, ~2 req concurrentes (fair-use ~10k/j).
- **OSM ≠ aléa** : `flood_prone` trop rare → s'appuyer sur ponts/tunnels/gués ; aléa via Géorisques en option.
- **Capacités hébergement OSM** quasi absentes → estimer (rooms×2, surface/4m²), étiqueter « estimation ».

## Repo

```
.claude-plugin/plugin.json · CLAUDE.md · README.md · requirements.txt
skills/{alerte-crue,demographie-iris,vulnerabilite-bpe,accessibilite-routes,logistique-hebergement}/
```

Libs Python : `requests`, `shapely`, `pyproj` (géo) ; `pypdf`/`pdfplumber` (PDF) au besoin.
