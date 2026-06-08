# -*- coding: utf-8 -*-
"""Versioning des skills + détection best-effort d'une mise à jour disponible sur GitHub.

Chaque skill porte sa version dans le frontmatter YAML de son `SKILL.md` (champ `version:`) —
source unique de vérité, lue ici (pas de constante codée en dur, pas de pyyaml). À l'exécution,
`check_update` compare cette version à celle du `SKILL.md` distant sur GitHub (branche `main`) et
renvoie un bloc stable indiquant si une mise à jour existe.

Principes (cf. CLAUDE.md) :
  - **Best-effort total** : un réseau coupé, un SKILL.md distant illisible ou sans version, voire
    une exception inattendue ne doivent JAMAIS casser le skill ni lever — on dégrade en renvoyant
    une chaîne explicative dans `version_distante` (« valeur typée ou chaîne expliquant le défaut »).
  - **Cache TTL 5 min** : relancer la même commande dans la fenêtre ne refait pas la requête réseau.
    Le cache vit dans le `data/` partagé, nommé par skill (`version-<nom>.json`) pour éviter toute
    collision inter-skills.
"""

import json
import os
import re
from datetime import datetime

from .dataset import now_iso, vtuple
from .errors import SkillError
from .http import http_get_text

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/FlorianHegele/SkillIssue/main"
_VERSION_TTL = 300  # 5 min : fenêtre pendant laquelle on réutilise le résultat caché
_FM_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def parse_frontmatter(text):
    """Parse minimal du frontmatter YAML entre les deux premières lignes `---`. PAS de pyyaml.

    Ne retient que les paires `clé: valeur` SCALAIRES non vides du 1er niveau (pas d'indentation) :
    on lit en pratique `name` et `version`. Les blocs multilignes (`description: >` suivi de lignes
    indentées) sont ignorés — la valeur après `>` est vide, et les lignes indentées ne matchent pas.
    Renvoie {} si le texte n'a pas de bloc frontmatter délimité.
    """
    if not isinstance(text, str):
        return {}
    lines = text.splitlines()
    # Le frontmatter doit ouvrir le fichier (tolère un BOM / lignes vides en tête).
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        start = i if line.strip() == "---" else None
        break
    if start is None:
        return {}
    fm = {}
    for line in lines[start + 1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t"):  # ligne indentée : appartient à un bloc multiligne -> ignorée
            continue
        m = _FM_LINE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            # On ne retient que les scalaires : une valeur vide ou un indicateur de bloc YAML
            # (`>`/`|` et leurs variantes `>-`, `|+`…) introduit un bloc multiligne -> ignoré.
            if val and val[0] not in (">", "|"):
                fm[key] = val.strip("'\"")
    return fm


def read_local_version(skill_dir):
    """(name, version) lus dans le SKILL.md du skill. version=None si absente/illisible (avalé)."""
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as fh:
            fm = parse_frontmatter(fh.read())
    except OSError:
        return None, None
    return fm.get("name"), fm.get("version")


def _cache_path(cache_dir, nom):
    # Nommé par skill : le `data/` est partagé entre les 5 skills.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", nom or "skill")
    return os.path.join(cache_dir, "version-%s.json" % safe)


def _fresh(verifie_le, ttl):
    """True si l'horodatage ISO `verifie_le` est plus récent que `ttl` secondes."""
    try:
        dt = datetime.fromisoformat(verifie_le)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(dt.tzinfo) - dt).total_seconds()
    return 0 <= age < ttl


def _compare(locale, distante):
    """(maj_disponible, message) à partir des deux versions. distante peut être une chaîne
    explicative (réseau/parse KO) : dans ce cas, pas de MAJ affirmée."""
    if not isinstance(distante, str) or not re.match(r"^\d", distante):
        return False, None
    if vtuple(distante) > vtuple(locale or "0"):
        return True, ("mise à jour disponible : %s (version installée : %s) — "
                      "mettez à jour le skill depuis le dépôt GitHub" % (distante, locale))
    return False, None


def check_update(skill_dir, cache_dir, timeout=10, ttl=_VERSION_TTL, raw_base=GITHUB_RAW_BASE):
    """Renvoie TOUJOURS un bloc dict stable (jamais d'exception) :

        {nom, version, version_distante, maj_disponible(bool), message(str|null),
         verifie_le(iso), source: "reseau"|"cache"|"local-seul"}

    `version_distante` est soit une version ("1.1.0"), soit une chaîne expliquant pourquoi elle est
    indisponible (réseau coupé, SKILL.md distant sans version). best-effort : enveloppe finale qui
    garantit qu'aucune erreur ne remonte au skill appelant.
    """
    try:
        name, locale = read_local_version(skill_dir)
        nom = name or os.path.basename(os.path.normpath(skill_dir))
        version = locale or "inconnue : SKILL.md local sans champ version"

        cache_file = _cache_path(cache_dir, nom)
        # 1) Cache TTL : on réutilise la version distante connue, mais on RECOMPARE avec la version
        #    locale actuelle (qui a pu changer après une mise à jour du skill dans la fenêtre).
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cached = json.load(fh)
            if _fresh(cached.get("verifie_le"), ttl):
                distante = cached.get("version_distante")
                maj, message = _compare(locale, distante)
                return {"nom": nom, "version": version, "version_distante": distante,
                        "maj_disponible": maj, "message": message,
                        "verifie_le": cached.get("verifie_le"), "source": "cache"}
        except (OSError, ValueError):
            pass  # pas de cache exploitable -> on interroge le réseau

        # 2) Réseau : fetch du SKILL.md distant + parse de sa version. Chemin GitHub = basename
        #    du dossier (fiable), pas le `name` du frontmatter.
        dossier = os.path.basename(os.path.normpath(skill_dir))
        url = "%s/skills/%s/SKILL.md" % (raw_base, dossier)
        try:
            distante = parse_frontmatter(http_get_text(url, timeout=timeout)).get("version")
            if not distante:
                distante = "indisponible : SKILL.md distant sans champ version"
        except SkillError as exc:
            distante = "indisponible : %s" % (exc.detail or exc.message)

        verifie_le = now_iso()
        maj, message = _compare(locale, distante)
        block = {"nom": nom, "version": version, "version_distante": distante,
                 "maj_disponible": maj, "message": message,
                 "verifie_le": verifie_le, "source": "reseau"}

        # 3) Persiste le cache (best-effort : une écriture impossible ne doit pas casser le check).
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(block, fh, ensure_ascii=False)
        except OSError:
            pass
        return block
    except Exception as exc:  # filet ultime : check_update ne remonte JAMAIS d'erreur
        return {"nom": os.path.basename(os.path.normpath(skill_dir or "")) or "skill",
                "version": "inconnue",
                "version_distante": "indisponible : %s" % exc,
                "maj_disponible": False, "message": None,
                "verifie_le": None, "source": "local-seul"}
