---
name: vulnerabilite-bpe
version: 1.0.0
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

# Grande commune : limiter chaque liste aux 20 plus proches (ou 0 = tout renvoyer)
python3 ${CLAUDE_SKILL_DIR}/main.py --commune "Montpellier" --top 20
```

Par défaut : **écoles** (maternelle, primaire, collège, lycée général/techno, lycée pro) +
**santé** (urgences, maternité, centre de santé, psychiatrie ambulatoire, médecine préventive,
dialyse, hospitalisation à domicile, maison de santé). Options : `--all-types` (tous les
équipements des domaines C *Enseignement* et D *Santé/action sociale*), `--radius` km (filtrer
autour du point), `--top` N (max d'équipements par liste, les plus proches d'abord ; défaut 50,
`0` = illimité — borne la taille de sortie sur les grandes communes), `--timeout` s (défaut 120).

**⚠ 1er appel** : télécharge le fichier-détail BPE national (~165 Mo zippé) puis le met en cache ;
les appels suivants ne re-téléchargent pas. Le filtrage par commune scanne ensuite le CSV (~quelques
secondes). Le champ `dataset.maj_skill_disponible` signale qu'un millésime plus récent existe mais
exige une version de skill supérieure.

## Sortie

JSON sur stdout : `{ lieu, dataset, vulnerabilite, skill }`. Le bloc `skill` (métadonnée de
version/mise à jour du skill) est toujours présent et sans incidence sur la décision.
- `dataset` : provenance (millésime, zone, url, depuis_cache, drapeau de MAJ).
- `vulnerabilite.commune` : `code`, `nom`, `ecoles_count`, `sante_count` (totaux trouvés dans le
  périmètre, **avant** la limite `--top`).
- `vulnerabilite.ecoles[]` / `vulnerabilite.sante[]` : par équipement `type_code`, `type_libelle`,
  `nom` (NOMRS si renseigné), `lat`, `lon`, `qualite_geoloc`, `distance_km` (trié par distance
  croissante ; au plus `--top` éléments par liste).
- `vulnerabilite.note` : présent **uniquement** si une liste a été tronquée par `--top` — précise
  combien d'équipements existent au total vs combien sont affichés (troncature jamais silencieuse :
  `ecoles_count` / `sante_count` restent les totaux, donc un compteur > longueur de liste la signale
  aussi). Sinon `null`.

> **`distance_km` est mesurée depuis le point résolu.** Avec `--commune`, ce point est le
> **centroïde** de la commune (via `geo.api`) : pour une grande commune ou une crue localisée, le
> tri par distance peut être trompeur. Pour viser un secteur précis (point d'inondation), passer
> `--lat`/`--lon` plutôt qu'un nom de commune.

Reformuler ensuite en langage naturel. Une mesure absente vaut une **chaîne explicative** (ex.
`"indisponible : coordonnées de l'équipement absentes"`) — jamais un `null` ambigu ; vérifier le
type avant tout calcul. Trois cas distincts en sortie :
- commune **introuvable / hors France** (géocodage échoué) → erreur (JSON sur stderr + code ≠ 0) ;
- commune valide **présente dans la BPE mais sans équipement ciblé** → listes vides (réponse
  valide, code 0) ;
- commune valide **mais absente du fichier-détail BPE** → bloc `vulnerabilite.error` dans le JSON
  stdout + code retour ≠ 0 (la sortie reste un JSON exploitable).
