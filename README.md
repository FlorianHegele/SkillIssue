# flood-response — plugin Claude Code d'aide à la décision en crue

Plugin de **skills** Claude Code pour appuyer la décision lors d'une **crue majeure en France**
(métropole + DOM/COM) : vigilance et mesures temps réel, prévision de pluie, démographie à
évacuer/héberger, équipements sensibles, accès routiers et hébergement d'urgence.

> **100 % API publiques sans clé** : aucun compte ni inscription nécessaire pour exécuter ou
> tester le plugin. Météo via OpenMeteo (modèle Météo-France AROME), INSEE via datasets CSV,
> OpenStreetMap via Overpass, géocodage via `geo.api.gouv.fr`.

Projet académique IUT NFC. Détails d'architecture et décisions : voir [`CLAUDE.md`](CLAUDE.md).

## Les 5 skills

| Skill | Question de décision | Sources |
| ----- | -------------------- | ------- |
| **alerte-crue** | Quelle alerte ici ? Quel niveau d'eau, quelle pluie arrive ? | Vigicrues, Hub'Eau, OpenMeteo/AROME |
| **demographie-iris** | Combien de personnes/ménages à évacuer ou héberger, où sont les foyers vulnérables ? | INSEE Couples-Familles-Ménages (IRIS) |
| **vulnerabilite-bpe** | Où sont les écoles et établissements de santé à protéger/évacuer ? | Fichier-détail BPE (INSEE) |
| **accessibilite-routes** | Quels franchissements risquent d'être coupés (gués, ponts, tunnels, points bas) ? | OpenStreetMap / Overpass |
| **logistique-hebergement** | Où héberger les sinistrés, pour combien de personnes ? | OpenStreetMap / Overpass |

Chaque skill prend une **localisation obligatoire** (`--commune <nom\|code INSEE>` **ou**
`--lat`/`--lon`), n'utilise **aucun lieu par défaut**, et renvoie du **JSON** sur stdout (les
erreurs partent sur stderr avec un code retour ≠ 0). Voir le `SKILL.md` de chaque skill.

## Installation

Python 3.10+ recommandé.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Dépendances : `requests` (HTTP, requis), `shapely` (géométrie Vigicrues, utilisé par
`alerte-crue`), `jsonschema` (validation des contrats, surtout en test).

## Utilisation (en ligne de commande)

```bash
# Synthèse crue pour une commune (nom ou code INSEE)
.venv/bin/python skills/alerte-crue/main.py --commune "Alès"
.venv/bin/python skills/alerte-crue/main.py --commune 30007

# Par coordonnées
.venv/bin/python skills/accessibilite-routes/main.py --lat 44.13 --lon 4.08 --radius-m 800

# Démographie détaillée par quartier (IRIS)
.venv/bin/python skills/demographie-iris/main.py --commune "Alès" --detail
```

Les options de chaque skill sont décrites dans son `SKILL.md` (et `--help`). Installé comme plugin
Claude Code, chaque skill s'invoque via `python3 ${CLAUDE_SKILL_DIR}/main.py …`.

## Tests

Suite **hors-ligne et déterministe** (rejoue des réponses API enregistrées + valide la sortie
contre le JSON Schema de chaque skill — aucun réseau) :

```bash
.venv/bin/python run_tests.py
```

> `pytest` lancé à la racine **ne fonctionne pas** : les 5 `tests/test_contract.py` partagent leur
> basename et importent chacun un module `main`/`contract` homonyme — une collecte unique les
> télescope. `run_tests.py` exécute chaque suite dans un **sous-process isolé** (équivalent à les
> lancer un par un). On peut aussi lancer une suite seule :
> `.venv/bin/python skills/alerte-crue/tests/test_contract.py`.

Sondes **live** (opt-in, réseau — vérifient que les vraies API parlent encore la forme attendue ;
à lancer avant le rendu) :

```bash
.venv/bin/python run_tests.py --live     # ou : RUN_LIVE=1 .venv/bin/python skills/<skill>/tests/test_live.py
```

## Données et cache

- **Zone de démonstration / vérité de test : Alès, Gard (INSEE 30007).** Sert d'exemple dans la
  doc et de référence pour les fixtures — **jamais** de valeur de repli à l'exécution : le plugin
  fonctionne pour toute la France.
- Les skills `demographie-iris` et `vulnerabilite-bpe` téléchargent à la demande de gros CSV INSEE
  (non versionnés) et les mettent en **cache local** dans `data/` (configurable via `--cache-dir`
  ou `$FLOOD_CACHE_DIR`). `data/` est ignoré par git.
- Les millésimes des datasets sont pilotés par un **registre versionné** (`dataset-registry.json`)
  servi depuis GitHub : on publie un nouveau millésime sans réinstaller le skill.

## Structure du dépôt

```
.claude-plugin/plugin.json · CLAUDE.md · README.md · requirements.txt · run_tests.py
skills/_common/              ← infra partagée (http, geo/commune, erreurs, contrat, dataset, overpass)
skills/<skill>/              ← SKILL.md · main.py · contract.py · contract.schema.json · references/ · tests/
```
