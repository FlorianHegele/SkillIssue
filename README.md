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

### Comme plugin Claude Code (recommandé)

Depuis Claude Code, ajouter le dépôt comme marketplace puis installer le plugin :

```text
/plugin marketplace add FlorianHegele/SkillIssue
/plugin install flood-response@flood-response-marketplace
```

Une fois installés, les 5 skills se déclenchent **automatiquement** selon la conversation
(routage par leur `description`) — il suffit de demander à Claude une info de crue pour une
commune française. Aucune clé ni inscription n'est nécessaire.

> **Aucune installation de dépendances à faire à la main.** Au **tout premier lancement** d'un
> skill, si les dépendances Python manquent, le plugin crée automatiquement un environnement
> virtuel **local au plugin** (`.venv/`) et y installe `requirements.txt`, puis s'y ré-exécute —
> **sans jamais toucher au Python système** (compatible avec les distributions « externally
> managed » / PEP 668 comme Arch, Debian, Ubuntu). `uv` est utilisé s'il est présent (plus
> rapide), sinon le module `venv` de la stdlib. Les lancements suivants sont instantanés. Pour
> désactiver ce comportement (environnement déjà gréé), exporter `FLOOD_NO_BOOTSTRAP=1`.

### En local / développement (CLI)

Pour lancer les scripts directement (ou contribuer/tester). Python 3.10+ recommandé.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Cette étape est **facultative pour un simple usage** : lancé directement avec `python3`, un skill
crée et provisionne `.venv/` tout seul au premier appel (voir l'encadré plus haut). La créer à la
main reste utile en développement (contrôle de l'interpréteur, lancement des tests).

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
- Le cache est **borné à 2 millésimes par skill** : au-delà, le plus vieux est supprimé
  automatiquement. Pour le vider à la main, supprimer `data/` (ou les fichiers `data/<prefix>-*`).
- Les millésimes des datasets sont pilotés par un **registre versionné** (`dataset-registry.json`)
  servi depuis GitHub : on publie un nouveau millésime sans réinstaller le skill.

## Structure du dépôt

```
.claude-plugin/plugin.json · .claude-plugin/marketplace.json
CLAUDE.md · README.md · LICENSE · requirements.txt · run_tests.py
skills/_common/              ← infra partagée (http, geo/commune, erreurs, contrat, dataset, overpass)
skills/<skill>/              ← SKILL.md · main.py · contract.py · contract.schema.json · references/ · tests/
```

## Versionnage et mises à jour

Deux mécanismes de version coexistent — ils répondent à des questions différentes :

- **Version du plugin** (`.claude-plugin/plugin.json`, champ `version`) : version unique pour
  l'ensemble du plugin. C'est celle qu'utilise Claude Code pour la distribution et la mise à jour
  via le marketplace (`/plugin update`, qui met à jour tout le plugin d'un coup).
- **Version par skill** (champ `version:` dans chaque `SKILL.md`) : lue à l'exécution par
  `skills/_common/version.py`, qui compare avec le `SKILL.md` distant sur GitHub et signale toute
  mise à jour disponible **directement dans la sortie JSON** (`maj_disponible`, `message`).

À connaître (les deux peuvent diverger, c'est assumé) :

- `plugin.json` fait foi pour la **distribution en tant que plugin** ; les `SKILL.md` servent au
  **contrôle de version au runtime, skill par skill**.
- Le contrôle au runtime est surtout utile **hors Claude Code** (usage en CLI / dépôt cloné), là
  où le mécanisme `/plugin` n'existe pas : c'est le seul signal « ce code est périmé ».
- Selon le mode d'usage, le bon geste de mise à jour diffère : **`/plugin update`** si installé
  comme plugin Claude Code, **re-pull du dépôt GitHub** si utilisé en CLI/cloné.

## Auteurs

Florian Hegele · Mehdi Ben Smail · Lukas Willmart — projet académique IUT NFC.

## Licence

Distribué sous licence **MIT** — voir [`LICENSE`](LICENSE).
