---
name: vulnerabilite-bpe
description: >
  Trigger when user asks where the schools and health facilities of a French location are — to
  evacuate or protect them during a flood: schools (a concentration of dependent minors to
  evacuate in an organised way) and hospitals/clinics whose care must keep running
  (non-transportable patients). Mots-clés FR : équipements sensibles, vulnérabilité, écoles,
  école maternelle, primaire, collège, lycée, établissements de santé, hôpital, urgences,
  maternité, dialyse, centre de santé, maison de santé, à évacuer, à protéger, continuité des
  soins, BPE, INSEE. EN keywords: schools, hospitals, health facilities, emergency, maternity,
  dialysis, sensitive facilities, vulnerability, evacuate, continuity of care, BPE. Donne la liste
  géolocalisée (écoles / santé) d'une commune (nom ou code INSEE) ou de coordonnées lat/lon en
  France, triée par distance, avec option rayon.
allowed-tools: Bash(python3 *)
---

# vulnerabilite-bpe

Écoles et établissements de **santé** d'une commune française, géolocalisés, à partir du
**fichier-détail BPE** (Base Permanente des Équipements) de l'INSEE. Sert à repérer les
établissements à **gérer spécifiquement** lors d'une évacuation : la **santé** (continuité des
soins, patients non transportables — dialyse, maternité, urgences) et les **écoles**
(concentration de mineurs à évacuer de façon encadrée).

> La réquisition de bâtiments comme **centres d'hébergement** (capacité d'accueil des sinistrés)
> relève du skill `logistique-hebergement`, pas de celui-ci.

## Quand l'utiliser

L'utilisateur veut savoir **où sont les écoles et les établissements de santé** d'une commune (ou
autour d'un point) : combien, de quel type, à quelle distance, pour décider quoi évacuer/protéger.
Pour la **capacité d'accueil** des sinistrés (hôtels, gymnases, salles réquisitionnables), voir
`logistique-hebergement`.

## Comment lancer

Localisation **obligatoire** (aucun lieu par défaut) : `--commune` (nom ou code INSEE) **ou**
`--lat`/`--lon` (géocodage inverse vers la commune).

```bash
# Par commune (nom ou code INSEE)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Alès"
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007

# Par coordonnées
python3 ${CLAUDE_SKILL_DIR}/main.py --lat 44.125 --lon 4.0905

# Restreindre à un rayon (km) autour du point ; élargir à tous les types des domaines C et D
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007 --radius 2
python3 ${CLAUDE_SKILL_DIR}/main.py --commune 30007 --all-types
```

Par défaut : **écoles** (maternelle, primaire, collège, lycée général/techno, lycée pro) +
**santé** (urgences, maternité, centre de santé, psychiatrie ambulatoire, médecine préventive,
dialyse, hospitalisation à domicile, maison de santé). Options : `--all-types` (tous les
équipements des domaines C *Enseignement* et D *Santé/action sociale*), `--radius` km (filtrer
autour du point), `--cache-dir` (défaut : `data/` à la racine du repo, ou `$FLOOD_CACHE_DIR`),
`--refresh` (force le re-téléchargement), `--timeout` s (défaut 120).

**⚠ 1er appel** : télécharge le fichier-détail BPE national (~165 Mo zippé, **~1,4 Go décompressé
en cache**) puis le met en **cache** (identifié par le hash de l'URL) ; les appels suivants ne
re-téléchargent pas. Le filtrage par commune scanne le CSV (~quelques secondes).

## Mise à jour des données (sans réinstaller le skill)

Le skill lit à chaque exécution un **registre versionné** (`dataset-registry.json`) hébergé sur
GitHub : il prend automatiquement le **dernier millésime compatible** avec sa version. Pour publier
un nouveau millésime (ex. BPE25), ajouter une entrée dans `dataset-registry.json` (le commit sur
GitHub suffit, aucune réinstallation côté utilisateur). Si un millésime plus récent exige une
version de skill supérieure, il le signale via `dataset.maj_skill_disponible` + un `message`.

## Sortie

JSON sur stdout : `{ lieu, dataset, vulnerabilite }`.
- `dataset` : provenance (millésime, zone, url, urlhash, depuis_cache, registre, drapeau de MAJ).
- `vulnerabilite.commune` : `code`, `nom`, `ecoles_count`, `sante_count`.
- `vulnerabilite.ecoles[]` / `vulnerabilite.sante[]` : par équipement `type_code`, `type_libelle`,
  `nom` (NOMRS si renseigné), `lat`, `lon`, `qualite_geoloc`, `distance_km` (trié par distance
  croissante).

Reformuler ensuite en langage naturel. Une mesure absente vaut une **chaîne explicative** (ex.
`"indisponible : coordonnées de l'équipement absentes"`) — jamais un `null` ambigu ; vérifier le
type avant tout calcul. Trois cas distincts en sortie :
- commune **introuvable / hors France** (géocodage échoué) → erreur (JSON sur stderr + code ≠ 0) ;
- commune valide **présente dans la BPE mais sans équipement ciblé** → listes vides (réponse
  valide, code 0) ;
- commune valide **mais absente du fichier-détail BPE** → bloc `vulnerabilite.error` dans le JSON
  stdout + code retour ≠ 0 (la sortie reste un JSON exploitable).

Contrat de sortie (défini en amont) : `contract.py` (dataclasses typées) + `contract.schema.json`
(validé hors-ligne par `tests/test_contract.py`). Infra commune : `skills/_common/`.

Détails des fichiers INSEE, colonnes et codes TYPEQU : voir `references/api.md`.
