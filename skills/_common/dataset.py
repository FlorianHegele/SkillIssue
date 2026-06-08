# -*- coding: utf-8 -*-
"""Socle registre → cache pour les skills basés sur un gros CSV INSEE zippé (non versionné).

Mutualise le pattern partagé par `vulnerabilite-bpe` et `demographie-iris` (et tout futur skill
du même genre) : un **registre versionné** hébergé sur GitHub recense les millésimes disponibles ;
le skill prend le dernier millésime COMPATIBLE avec sa version, télécharge le(s) fichier(s) à la
demande, et les met en **cache identifié par le hash de l'URL** (re-téléchargement seulement si
l'URL change). Réseau coupé → on se rabat sur le cache (dégradation gracieuse).

Chaque skill ne garde qu'une fine couche de configuration (URL du registre, version, préfixe de
cache, libellé) + sa logique de LECTURE du CSV (colonnes, filtres), qui lui est propre. Les
bizarreries génériques (sélection de version, cache, extraction du zip) vivent ici, une seule fois.

Une `SourceConfig` fige la config d'un skill ; ses méthodes exposent la même signature
`(cache_dir, timeout)` / `(entry, file_entry, …)` que l'ancien code in-skill, pour que les wrappers
côté skill restent triviaux (et que les tests qui les mockent ne changent pas).
"""

import csv
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone

from .errors import SkillError, fail
from .http import http_download, http_get_json


# --- Utilitaires purs ---------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def vtuple(version):
    """'1.2.3' -> (1, 2, 3) pour comparer des versions sémantiques."""
    parts = []
    for p in str(version or "0").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def urlhash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_encoding(path):
    """Détecte l'encodage (les bases INSEE sont en UTF-8 ; latin-1 en dernier recours
    décode n'importe quel octet)."""
    with open(path, "rb") as fh:
        sample = fh.read(65536)
    for enc in ("utf-8", "latin-1"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def load_json_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- Extraction du zip --------------------------------------------------------
def extract_data_csv(zip_path, dest_csv):
    """Extrait le CSV de DONNÉES du zip (ignore un éventuel `meta_*.CSV`, dictionnaire)."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        data = [n for n in members if not os.path.basename(n).lower().startswith("meta_")]
        if not data:
            fail("aucun CSV de données dans le zip téléchargé",
                 detail={"membres": zf.namelist()})
        member = max(data, key=lambda n: zf.getinfo(n).file_size)  # le fichier de données
        tmp = dest_csv + ".part"
        with zf.open(member) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, dest_csv)
    return dest_csv


# --- Sélection des fichiers à interroger --------------------------------------
def select_files(entry, zone_arg, code_insee=None):
    """Liste ordonnée des fichiers à essayer pour cette entrée de registre.

    AUCUNE hypothèse géographique codée en dur : la couverture est portée par le registre
    (donnée, pas code). En `auto` on essaie tous les fichiers déclarés jusqu'à trouver la
    commune. Optimisation OPTIONNELLE pilotée par le registre : un fichier peut déclarer
    `code_prefixes` ; ceux dont un préfixe correspond au code commune sont essayés en PREMIER
    (ordre seulement — on essaie toujours tous les fichiers ensuite, donc un indice absent/faux
    ne change jamais le résultat, juste la rapidité). `--zone <nom>` restreint.
    """
    files = entry.get("files") or []
    if zone_arg and zone_arg != "auto":
        chosen = [f for f in files if f.get("zone") == zone_arg]
        if not chosen:
            fail("zone %r inconnue pour le millésime %s" % (zone_arg, entry.get("millesime")),
                 detail={"zones_disponibles": [f.get("zone") for f in files]})
        return chosen

    code = code_insee or ""

    def _matches(f):  # 0 = le code matche un préfixe déclaré -> prioritaire ; 1 = sinon
        prefixes = f.get("code_prefixes") or []
        return 0 if any(code.startswith(p) for p in prefixes) else 1

    return sorted(files, key=_matches)  # tri stable : conserve l'ordre du registre à match égal


# --- Provenance (bloc dataset de la sortie) -----------------------------------
def dataset_block(entry, file_entry, info):
    """Bloc de provenance (forme stable). Mis à jour ensuite via `apply_meta`."""
    url = file_entry.get("url")
    return {
        "millesime": entry.get("millesime"),
        "geographie": entry.get("geographie"),
        "zone": file_entry.get("zone"),
        "url": url,
        "urlhash": urlhash(url) if url else "",
        "telecharge_le": None,
        "sha256": None,
        "depuis_cache": False,
        "registre_source": info["registre_source"],
        "registry_version": info["registry_version"],
        "maj_skill_disponible": info["maj_skill_disponible"],
        "message": info["message"],
    }


def apply_meta(block, meta):
    """Reflète dans le bloc le fichier réellement chargé (zone/url/cache/hash)."""
    block["zone"] = meta.get("zone", block["zone"])
    block["url"] = meta.get("url", block["url"])
    block["urlhash"] = meta.get("urlhash", block["urlhash"])
    block["telecharge_le"] = meta.get("telecharge_le")
    block["sha256"] = meta.get("sha256")
    block["depuis_cache"] = meta.get("depuis_cache", False)
    if meta.get("message"):
        block["message"] = ("%s | %s" % (block["message"], meta["message"])
                            if block["message"] else meta["message"])


# --- Éviction : borne le cache à MAX_CACHED_DATASETS CSV par skill -------------
MAX_CACHED_DATASETS = 2  # nb max de datasets (CSV) gardés par skill (préfixe) ; au-delà, on
                         # supprime le(s) plus vieux. Évite la croissance non bornée du cache
                         # `data/` au fil des millésimes (un fichier-détail BPE pèse ~1,4 Go).


def _evict_old_datasets(cache_dir, prefix, keep=MAX_CACHED_DATASETS):
    """Garde au plus `keep` datasets `<prefix>-<hash>.csv` dans `cache_dir` ; supprime les plus
    vieux au-delà (avec leur sidecar `.json` et un éventuel `.zip` résiduel). Tri par date de
    téléchargement (mtime du CSV : réécrit à chaque téléchargement, intact sur un cache-hit).
    Per-préfixe : ne touche JAMAIS au cache d'un autre skill. Retourne la liste des fichiers
    supprimés. Appelé après un téléchargement réussi → le fichier qu'on vient d'écrire est le plus
    récent, donc jamais évincé."""
    try:
        csvs = [n for n in os.listdir(cache_dir)
                if n.startswith(prefix + "-") and n.endswith(".csv")]
    except OSError:
        return []
    if len(csvs) <= keep:
        return []

    def _mtime(name):
        try:
            return os.path.getmtime(os.path.join(cache_dir, name))
        except OSError:
            return 0.0

    csvs.sort(key=_mtime, reverse=True)        # plus récent d'abord
    removed = []
    for name in csvs[keep:]:                    # le(s) plus vieux au-delà de `keep`
        stem = name[:-len(".csv")]              # "<prefix>-<hash>"
        for ext in (".csv", ".json", ".zip"):
            path = os.path.join(cache_dir, stem + ext)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError:
                    pass
    return removed


class SourceConfig:
    """Configuration d'une source CSV pilotée par registre, propre à un skill.

    Paramètres :
      registry_url        URL raw GitHub du `dataset-registry.json`.
      local_registry      copie locale embarquée dans le skill (repli ultime hors-ligne).
      skill_version        version du skill (sélection du dernier millésime compatible).
      cache_prefix        préfixe des fichiers de cache (ex. "bpe", "cfm") — DOIT être distinct
                          d'un skill à l'autre, le cache étant partagé (même `data/`).
      label               libellé humain de la base, pour les messages d'erreur (ex. "BPE").
      registry_cache_name nom du registre persisté en cache (distinct par skill, même raison).
    """

    def __init__(self, registry_url, local_registry, skill_version, cache_prefix, label,
                 registry_cache_name):
        self.registry_url = registry_url
        self.local_registry = local_registry
        self.skill_version = skill_version
        self.cache_prefix = cache_prefix
        self.label = label
        self.registry_cache_name = registry_cache_name

    # --- Registre : choix de la source de données -----------------------------
    def resolve_source(self, cache_dir, timeout):
        """Choisit l'entrée de dataset à utiliser (registre GitHub > cache > copie locale).

        Retourne (entry, info). `info` porte la provenance et le drapeau de mise à jour du skill.
        Lève SkillError si aucun registre exploitable ou aucune entrée compatible avec ce skill.
        """
        info = {"registre_source": None, "registry_version": None,
                "maj_skill_disponible": False, "message": None}

        candidates = []  # (label, registre)
        try:
            # GitHub raw sert les .json en text/plain -> require_json=False (parsing JSON validé).
            remote = http_get_json(self.registry_url, timeout=timeout, require_json=False)
            if isinstance(remote, dict) and remote.get("entries"):
                candidates.append(("github", remote))
                try:  # persiste le dernier registre distant connu pour les runs hors-ligne
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(os.path.join(cache_dir, self.registry_cache_name), "w",
                              encoding="utf-8") as fh:
                        json.dump(remote, fh, ensure_ascii=False)
                except OSError:
                    pass
        except SkillError:
            pass  # GitHub injoignable : on se rabat sur le cache / la copie locale

        cached = os.path.join(cache_dir, self.registry_cache_name)
        if os.path.exists(cached):
            try:
                candidates.append(("cache", load_json_file(cached)))
            except (OSError, ValueError):
                pass
        if os.path.exists(self.local_registry):
            try:
                candidates.append(("local", load_json_file(self.local_registry)))
            except (OSError, ValueError):
                pass

        if not candidates:
            fail("registre des datasets introuvable (ni distant, ni cache, ni copie locale)",
                 detail="repo du skill incomplet : dataset-registry.json manquant")

        # On retient le registre de plus haut registry_version (le plus à jour).
        def _regv(reg):
            v = reg.get("registry_version")
            return v if isinstance(v, int) else 0
        label, reg = max(candidates, key=lambda c: _regv(c[1]))
        info["registre_source"] = label                       # toujours un label (jamais None)
        info["registry_version"] = _regv(reg)

        entries = reg.get("entries") or []
        if not entries:
            fail("registre des datasets vide", detail="aucune entrée dans dataset-registry.json")

        skill_v = vtuple(self.skill_version)
        compatible = [e for e in entries
                      if vtuple(e.get("min_skill_version", "0")) <= skill_v]
        if not compatible:
            fail("aucune base compatible avec ce skill (v%s) recensée ; mettre à jour le repo "
                 "du skill" % self.skill_version,
                 detail={"versions_de_skill_requises":
                         sorted({e.get("min_skill_version") for e in entries})})

        entry = max(compatible, key=lambda e: e.get("millesime", 0))

        # Le registre est maintenu à la main : on valide l'entrée retenue avant de s'en servir
        # (erreur contrôlée, pas de trace brute). millesime DOIT être un entier — sinon la sortie
        # porterait millesime:null, ce qui violerait le schéma ("type": "integer").
        if not isinstance(entry.get("millesime"), int):
            fail("entrée de registre invalide : 'millesime' (entier) manquant ou non numérique ; "
                 "corriger dataset-registry.json", detail={"entry": entry})
        files = entry.get("files") or []
        if not files or not all(f.get("zone") and f.get("url") for f in files):
            fail("entrée de registre incomplète pour le millésime %s : 'files' (zone+url) "
                 "manquant ou invalide ; corriger dataset-registry.json"
                 % entry.get("millesime"), detail={"entry": entry})

        # Existe-t-il un millésime plus récent mais hors de portée de cette version du skill ?
        plus_recents = [e for e in entries
                        if e.get("millesime", 0) > entry.get("millesime", 0)
                        and vtuple(e.get("min_skill_version", "0")) > skill_v]
        if plus_recents:
            best = max(plus_recents, key=lambda e: e.get("millesime", 0))
            info["maj_skill_disponible"] = True
            info["message"] = (
                "un millésime plus récent (%s) existe mais nécessite un skill >= %s ; mettez à "
                "jour le skill pour des données plus récentes. Utilisation du millésime %s."
                % (best.get("millesime"), best.get("min_skill_version"),
                   entry.get("millesime")))

        return entry, info

    # --- Téléchargement + cache (identité = hash de l'URL) --------------------
    def dataset_path(self, entry, file_entry, cache_dir, refresh, timeout):
        """Garantit la présence locale du CSV d'un fichier (zone) déclaré. Retourne (path, meta).

        Cache identifié par le hash de l'URL : si le fichier nommé par cet `urlhash` existe déjà,
        le lien a déjà été téléchargé -> aucun re-téléchargement (sauf --refresh). Si l'URL change
        (nouveau millésime via le registre), l'urlhash change donc le téléchargement se relance.
        """
        zone = file_entry.get("zone")
        url = file_entry.get("url")
        uh = urlhash(url)
        os.makedirs(cache_dir, exist_ok=True)
        csv_path = os.path.join(cache_dir, "%s-%s.csv" % (self.cache_prefix, uh))
        zip_path = os.path.join(cache_dir, "%s-%s.zip" % (self.cache_prefix, uh))
        side_path = os.path.join(cache_dir, "%s-%s.json" % (self.cache_prefix, uh))

        def _read_cache():
            meta = load_json_file(side_path)
            meta["depuis_cache"] = True
            return csv_path, meta

        if not refresh and os.path.exists(csv_path) and os.path.exists(side_path):
            try:
                return _read_cache()
            except (OSError, ValueError):
                pass  # sidecar corrompu : on re-télécharge

        try:
            http_download(url, zip_path, timeout=timeout, expect_content_type="zip")
        except SkillError as exc:
            # Réseau KO : si un cache existe pour CE lien, on l'utilise quand même (dégradation).
            if os.path.exists(csv_path) and os.path.exists(side_path):
                try:
                    path, meta = _read_cache()
                    meta["message"] = "réseau indisponible, cache utilisé (%s)" % exc.message
                    return path, meta
                except (OSError, ValueError):
                    pass
            fail("impossible de télécharger la base %s %s (zone %s) et aucun cache disponible ; "
                 "il n'existe pas de base de données à jour recensée pour le skill, mettre à jour "
                 "le repo du skill" % (self.label, entry.get("millesime"), zone),
                 detail={"url": url, "cause": exc.detail or exc.message})

        extract_data_csv(zip_path, csv_path)
        try:
            os.remove(zip_path)  # le zip n'est plus utile, on ne garde que le CSV extrait
        except OSError:
            pass

        meta = {
            "millesime": entry.get("millesime"),
            "geographie": entry.get("geographie"),
            "zone": zone,
            "url": url,
            "urlhash": uh,
            "sha256": sha256_file(csv_path),
            "size": os.path.getsize(csv_path),
            "telecharge_le": now_iso(),
            "depuis_cache": False,
        }
        try:
            with open(side_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
        except OSError:
            pass
        # Cache borné : au plus MAX_CACHED_DATASETS CSV pour ce skill ; le plus vieux est supprimé.
        # (Le CSV qu'on vient d'écrire est le plus récent → jamais évincé.)
        _evict_old_datasets(cache_dir, self.cache_prefix)
        return csv_path, meta
