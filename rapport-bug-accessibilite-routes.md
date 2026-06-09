# Rapport de bug — skill `flood-response:accessibilite-routes`

**Date :** 2026-06-09
**Plugin :** flood-response 1.0.2 — skill `accessibilite-routes` v1.0.0
**Contexte :** exercice crue Savoureuse / Belfort. Les 3 autres skills
(`alerte-crue`, `demographie-iris`, `vulnerabilite-bpe`, `logistique-hebergement`)
ont fonctionné. Seul `accessibilite-routes` échoue **systématiquement**.

---

## 1. TL;DR

Le skill échoue **non pas à cause de son code de requête** (la requête Overpass est
valide et s'exécute en < 2 s quand un slot est libre), mais à cause de **deux
défaillances côté infrastructure Overpass** que le skill **gère mal** :

1. **Serveur primaire `overpass-api.de`** : renvoie de façon **intermittente** des
   `HTTP 429` (rate-limit) et `HTTP 504` (« dispatcher too busy »). Normal pour
   l'instance publique gratuite sous charge.
2. **Miroir `overpass.kumi.systems`** : **hors service** — la connexion TCP s'établit
   (~0,05 s) mais le serveur **ne répond jamais** (timeout systématique).

Conséquence : dès que le primaire renvoie un 429/504, le skill bascule sur le miroir
mort et **attend le timeout HTTP complet (`timeout` QL + 15 s) × 3 tentatives** avant
d'abandonner → la commande paraît « bloquée » pendant plusieurs minutes, puis renvoie
`"Overpass indisponible (serveur principal et miroir)"`.

`logistique-hebergement` (même client Overpass) a réussi car sa requête, plus légère
et sur des clés indexées (`amenity`/`tourism`/`leisure`), obtient un slot plus
facilement et n'a pas tapé le miroir au mauvais moment.

---

## 2. Symptômes observés

- Lancements répétés du skill (rayons 1200–2000 m, timeouts 25→90 s) :
  - soit échec immédiat `Overpass indisponible (serveur principal et miroir)` ;
  - soit **blocage de plusieurs minutes** sans sortie, puis échec (exit code 1).
- Sortie type :
  ```json
  "accessibilite": {
    "error": "Overpass indisponible (serveur principal et miroir)",
    "detail": {
      "principal": "échec de l'appel à https://overpass-api.de/api/interpreter",
      "miroir": "échec de l'appel à https://overpass.kumi.systems/api/interpreter"
    }
  }
  ```
  → le message **masque la cause réelle** (429/504/miroir mort) : « principal » et
  « miroir » ne portent que `"échec de l'appel à <url>"`, le code HTTP est perdu.

---

## 3. Reproduction (curl direct, en dehors du skill)

Requête QL exacte générée par le skill (point 47.6458, 6.841, rayon 1200 m) :

```
[out:json][timeout:90];
(
  way ["highway"!~"^(footway|steps|path|cycleway|pedestrian|bridleway|corridor)$"]["ford"]["ford"!="no"](around:1200,47.6458,6.841);
  node["ford"]["ford"!="no"](around:1200,47.6458,6.841);
  way ["highway"]["highway"!~"^(...)$"]["bridge"]["bridge"!="no"](around:1200,...);
  way [...]["tunnel"]["tunnel"!="no"](...);
  way [...]["layer"~"^-"](...);
  way [...]["flood_prone"="yes"](...);
  way [...]["hazard"="flooding"](...);
);
out tags center;
```

### Résultats mesurés

| Test | Endpoint | En-têtes | Résultat |
|---|---|---|---|
| count trivial | primaire | `Accept: */*` | **406** en 0,3 s |
| count trivial | primaire | `Accept: application/json` + UA | **200** en 7,8 s |
| count trivial (POST) | primaire | — | **504** en 5,2 s |
| **requête routes complète** | primaire | `Accept: application/json` + UA | **504** « dispatcher too busy » en 8 s |
| requête routes (re-essai 1) | primaire | idem | **200** en 2,1 s ✅ |
| requête routes (re-essai 2) | primaire | idem | **200** en 0,76 s ✅ |
| requête routes (re-essai 3) | primaire | idem | **429** en 7 s |
| gués seuls (allégée) | primaire | idem | **429** en 7 s |
| count trivial | **miroir kumi** | `Accept: application/json` | **pas de réponse**, timeout 15 s (connect OK 0,05 s) ×2 |

Corps du 504 du primaire :
```
Error: runtime error: open64: 0 Success /osm3s_osm_base
Dispatcher_Client::request_read_and_idx::timeout.
The server is probably too busy to handle your request.
```

**Conclusion reproduction :** la requête est **bonne** (réussit en < 2 s quand un slot
est libre). Les échecs sont du **rate-limiting / surcharge intermittente du primaire**
+ un **miroir mort**.

---

## 4. Analyse — problèmes côté SKILL (actionnables par l'équipe)

> Les pannes Overpass sont externes, mais le skill peut beaucoup mieux les encaisser.

### P1 — Le miroir mort domine la latence (impact le plus visible)
`_common/overpass.py` (`query`, l.34-52) : sur échec du primaire, on appelle le miroir
avec `http_timeout = timeout + 15` (l.44), et `_common/http.py:http_get_json` (l.21)
fait **`retries=3`**. Le miroir `kumi.systems` accepte la connexion TCP mais ne répond
jamais → chaque tentative attend le **read-timeout complet**. Avec `--timeout 60` :
75 s × 3 = **jusqu'à 225 s de blocage sur un serveur mort**, après avoir déjà épuisé le
primaire. C'est ce qui fait « planter » l'exercice.

**Pistes :**
- Timeout `(connect, read)` distinct et **court** pour la sonde miroir (ex. `(5, 20)`),
  au lieu d'un scalaire = `timeout+15`.
- Réduire `retries` sur le **fallback** (1 tentative suffit pour un miroir).
- Retirer / remplacer `overpass.kumi.systems` : il est durablement non-répondant.
  Alternatives publiques : `https://overpass.private.coffee/api/interpreter`,
  `https://maps.mail.ru/osm/tools/overpass/api/interpreter`,
  `https://overpass.osm.ch/api/interpreter`. Idéalement **liste de N miroirs**
  essayés dans l'ordre, pas un seul.

### P2 — Aucun traitement spécifique du HTTP 429 (rate-limit)
`_common/http.py:21` traite `429` comme n'importe quel non-2xx : `last_err = "HTTP 429"`,
backoff **linéaire 0,8 s puis 1,6 s**, **3 tentatives**, et **`Retry-After` ignoré**.
Pour une instance Overpass publique, 429 est l'état nominal sous charge et veut dire
« attends » — 0,8 s est très insuffisant.

**Pistes :**
- Détecter `429` explicitement → backoff **exponentiel** plus large (ex. 2 s, 5 s, 15 s),
  honorer l'en-tête `Retry-After` s'il est présent.
- Augmenter `retries` (ou les rendre configurables) pour 429/504, qui sont
  **transitoires** — nos re-essais passaient en 200 quelques secondes plus tard.

### P3 — Le message d'erreur masque la cause réelle
La sortie ne dit que `"échec de l'appel à <url>"` : impossible pour l'utilisateur (ou
l'IA) de savoir si c'est **rate-limit transitoire** (→ « réessaie dans 1 min »),
**serveur surchargé**, ou **réellement down**. Le code HTTP (429/504/406) devrait
remonter dans `detail`, avec un message distinguant « temporairement indisponible,
réessayer » de « indisponible ».

### P4 — Sensibilité au 406 (content-negotiation Apache)
`overpass-api.de` (Apache, négociation de contenu stricte) renvoie **406** quand
l'`Accept` ne lui convient pas : `Accept: */*` → 406, `Accept: application/json` + UA →
200, et l'endpoint `/api/status` → 406 avec `Accept: application/json`. Le skill envoie
bien `Accept: application/json` (l.34 de `http.py`), donc il est surtout exposé quand la
combinaison en-têtes varie. À fiabiliser : figer un couple `Accept` + `User-Agent`
connu-bon, et **ne pas** rejeter brutalement un 406 sans le signaler comme tel.

### P5 — Poids de la requête (secondaire mais aggravant)
`accessibilite-routes/main.py:build_query` (l.69) empile 7 clauses avec regex négatives
sur `highway` et des clés peu/non indexées en zone (`bridge`, `tunnel`, `layer~"^-"`).
Ça reste rapide (< 2 s) **slot libre**, mais c'est plus « cher » donc plus susceptible
de déclencher 429/504 sous charge que la requête de `logistique-hebergement`.

**Pistes :** filtrer l'exclusion piétonne **côté client** plutôt qu'en regex serveur,
ou scinder en requêtes plus petites ; envisager un `[timeout:]` QL plus bas pour
échouer-vite côté serveur plutôt que de tenir un slot.

---

## 5. Recommandations priorisées

| Prio | Action | Fichier |
|---|---|---|
| 🔴 P0 | Remplacer/retirer le miroir `kumi.systems` (mort) + liste multi-miroirs | `_common/overpass.py:20-21,34-52` |
| 🔴 P0 | Timeout court + 1 seule tentative sur le fallback miroir | `_common/overpass.py:44`, `_common/http.py:21` |
| 🟠 P1 | Gestion dédiée 429/504 : backoff exponentiel, honorer `Retry-After` | `_common/http.py:30-60` |
| 🟠 P1 | Remonter le code HTTP réel dans `detail` + message « réessayer » | `_common/overpass.py:51`, `main.py:231` |
| 🟡 P2 | Figer `Accept`+`UA` connus-bons ; gérer 406 proprement | `_common/http.py:32-35` |
| 🟡 P2 | Alléger la requête (exclusion piéton côté client / split) | `accessibilite-routes/main.py:69-91` |

---

## 6. Vérification rapide pour l'équipe (copier-coller)

```bash
# Le miroir est-il toujours mort ? (doit répondre ~instantanément si réparé)
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 15 \
  -H "Accept: application/json" \
  -G --data-urlencode 'data=[out:json][timeout:25];out count;' \
  https://overpass.kumi.systems/api/interpreter

# Le primaire répond-il à la vraie requête ? (réessayer si 429/504)
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 60 \
  -H "Accept: application/json" -H "User-Agent: flood-response/0.1 (academic project)" \
  -G --data-urlencode 'data=[out:json][timeout:25];(node["ford"](around:1200,47.6458,6.841););out;' \
  https://overpass-api.de/api/interpreter
```

---

*Diagnostic réalisé sur l'environnement réel du plugin (venv local 1.0.2), code lu :
`skills/accessibilite-routes/main.py`, `skills/_common/overpass.py`,
`skills/_common/http.py`.*
