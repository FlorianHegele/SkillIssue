# CLAUDE.md — Plugin Claude Code « flood-response »

Plugin de **skills** (jamais de MCP) pour l'aide à la décision lors d'une crue majeure.
Projet académique IUT NFC. Rendu : repo GitHub public → `guyeux@gmail.com`, **avant le 12 juin 2026**.

## Règles d'architecture d'un skill

Un skill = dossier `skills/<nom>/` avec `SKILL.md` + `main.py` + `contract.py` + `contract.schema.json` + `references/` (chargé à la demande) + `tests/`. Infra transverse partagée dans `skills/_common/` (importée en ajoutant le dossier parent `skills/` au `sys.path`).

**Contrat d'interface (défini en amont)** : pour chaque skill, on décide *d'abord* la forme de sortie à partir des besoins de décision, puis les API sont traduites vers ce contrat par des **adaptateurs** (les fonctions `collect_*`) — couche anti-corruption : les bizarreries d'API (redirections, pagination, renommages) restent confinées, la sortie reste stable. Le contrat = dataclasses typées (`contract.py`) + JSON Schema (`contract.schema.json`) validé hors-ligne sur fixtures (`tests/`). Pivot commun = `_common.Lieu` (code INSEE + lat/lon).

**Donnée manquante = jamais un `null` ambigu.** Convention : un champ de mesure vaut **soit sa valeur typée, soit une chaîne explicative** disant *pourquoi* elle manque (`"indisponible : pas de mesure temps réel récente"`, `"erreur : <message>"`). Schema : `"type": ["number", "string"]`. On distingue ainsi « capteur muet » vs « API en panne » sans alourdir le cas nominal (le consommateur final est l'IA, qui reformule ; vérifier le type avant tout calcul). Même esprit pour les valeurs calculées/estimées à venir (tendance hydro, capacité d'hébergement) : valeur honnête ou chaîne expliquant le défaut, plutôt qu'un faux remplissage.

**`SKILL.md`** : frontmatter (`name`, `description`, `allowed-tools: Bash(python3 *)`) + body court (quand l'utiliser, comment lancer via `python3 ${CLAUDE_SKILL_DIR}/main.py …`).

**`description`** (seul levier de routage, <200 mots) : commence par « Trigger when user asks… », mots-clés métier FR+EN + abréviations (BPE, IRIS, INSEE), ne répète pas `name`.

**`main.py`** : Python ordinaire, testable seul (argparse, pas de serveur), **sortie JSON** (`ensure_ascii=False`), erreurs sur stderr + code retour ≠ 0. Deps dans `requirements.txt`. **Pas de fallback silencieux sur une donnée par défaut** : voir le principe « Erreurs contrôlées, pas de fallback ».

**Réduction tokens** : description courte ; tout détail (doc API, exemples, erreurs) dans `references/` ; gros contenus (CSV/Parquet, configs longues) lus par le script, **jamais inline**.

**Optimisation de la sortie (après chaque skill)** : une fois le skill fonctionnel, repasser sur le JSON renvoyé pour l'**optimiser avant de le retourner** — ne pas relayer la réponse brute de l'API. Ne garder que les champs utiles à la décision, renommer/structurer clairement (clés parlantes, unités explicites), agréger/résumer ce qui peut l'être (ex. cumul plutôt que série complète si la série n'apporte rien), trier/limiter (top-N pertinents). But : réduire le contexte consommé **et** rendre la réponse plus lisible et exploitable (par l'IA comme par l'utilisateur). Tout ce qui est volumineux mais rarement utile va dans une option dédiée ou `references/`, pas dans la sortie par défaut.

## Décisions arrêtées

- **100 % API sans clé** (vérifié live 5 juin 2026 ; qualité ≥ API à clé, et testable par le correcteur sans inscription ; pas de fallback). Météo-France→OpenMeteo, INSEE Melodi→datasets CSV.
- **Zone démo / donnée de test : Alès / Gard (30)** (INSEE 30007, ≈ lat 44.13 / lon 4.08). Sert d'exemple dans la doc et d'« source de vérité » pour les tests — **jamais** comme valeur de repli à l'exécution. Le plugin doit fonctionner pour toute la France.
- **Erreurs contrôlées, pas de fallback** : ne jamais retomber silencieusement sur une donnée par défaut (ex. Alès) quand une entrée manque ou échoue. Émettre une **erreur explicite** (JSON sur stderr + code retour ≠ 0) qui dit *ce* qui a échoué et *pourquoi*, avec un détail exploitable (valeur fournie, suggestions, candidats homonymes…). Objectif : permettre soit à l'IA de se corriger, soit d'informer l'utilisateur que **son** entrée est en cause (ex. commune inexistante en France) et qu'il doit la corriger. Fallback toléré uniquement s'il est réellement nécessaire **et** clairement étiqueté (ex. estimation de capacité d'hébergement). Dégradation gracieuse multi-sources : une source en panne porte un champ `error` sans bloquer les autres ; code retour ≠ 0 seulement si tout échoue.
- **Datasets non versionnés** (CSV IRIS ~21 Mo, BPE) : téléchargement à la demande + cache local + échantillon de test hors-ligne.
- Cohérence inter-skills : pivot = code commune INSEE + lat/lon.

## Les 5 skills + endpoints vérifiés

| Skill | Endpoint(s) |
| ----- | ----------- |
| `alerte-crue` | Vigicrues `…/services/InfoVigiCru.geojson` (redirige depuis `/1/` ; `NivInfViCr` 1=vert 2=jaune 3=orange 4=rouge) · Hub'Eau `hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr` (H mm, Q l/s) · OpenMeteo `api.open-meteo.com/v1/forecast` (`models=meteofrance_arome_france_hd`) |
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
skills/_common/                ← infra partagée (http, geo/commune, errors, contract de base)
skills/<skill>/                ← SKILL.md · main.py · contract.py · contract.schema.json · references/ · tests/
  skills/{alerte-crue,demographie-iris,vulnerabilite-bpe,accessibilite-routes,logistique-hebergement}/
```

Libs Python : `requests`, `shapely`, `pyproj` (géo) ; `pypdf`/`pdfplumber` (PDF) au besoin.
