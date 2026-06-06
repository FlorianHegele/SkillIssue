#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vulnerabilite-bpe — écoles et établissements de santé d'une commune française.

Aide à évacuer/protéger en crue : localise les établissements à gérer spécifiquement d'une commune
(écoles : concentration de mineurs à évacuer de façon encadrée ; établissements de santé :
continuité des soins vitale, patients non transportables). La réquisition de bâtiments comme
centres d'hébergement relève du skill logistique-hebergement. Source = fichier-détail BPE de l'INSEE (Base
Permanente des Équipements), un gros CSV NATIONAL zippé à télécharger (~165 Mo, ~1,4 Go une fois
décompressé ; pas une API JSON), qui porte déjà LATITUDE/LONGITUDE WGS84. Voir references/api.md.

Par défaut : écoles (C107/C108/C201/C301/C302) + santé (D106–D113). --all-types élargit aux
domaines C et D entiers. Filtrage sur la commune (DEPCOM) ; chaque équipement est annoté de sa
distance au point résolu (tri croissant) ; --radius restreint à un rayon en km.

Mise à jour des données SANS réinstaller le skill : un registre versionné hébergé sur GitHub
(dataset-registry.json) pointe vers le dernier millésime ; le skill prend le dernier millésime
COMPATIBLE avec sa version (sinon le signale et continue). Le CSV est mis en cache, identifié par
le hash de son URL (re-téléchargement uniquement si l'URL change).

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    SkillError, emit_error, fail, haversine_km, http_download, http_get_json, jsonable,
    resolve_location, reverse_commune,
)

import contract as C  # noqa: E402  (module local du skill)

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Version & registre -------------------------------------------------------
# Incrémenter SKILL_VERSION en cas de changement cassant des colonnes lues ; le registre
# utilise min_skill_version pour qu'un vieux skill ne tente pas de lire un schéma incompatible.
SKILL_VERSION = "1.0.0"
REGISTRY_URL = ("https://raw.githubusercontent.com/FlorianHegele/SkillIssue/main/"
                "skills/vulnerabilite-bpe/dataset-registry.json")
LOCAL_REGISTRY = os.path.join(SKILL_DIR, "dataset-registry.json")

CSV_SEP = ";"
_REPO_ROOT = os.path.dirname(os.path.dirname(SKILL_DIR))
DEFAULT_CACHE = os.environ.get("FLOOD_CACHE_DIR") or os.path.join(_REPO_ROOT, "data")

# --- Types d'équipements ciblés (BPE) -----------------------------------------
# Codes TYPEQU ciblés par défaut + libellés lisibles. Les codes évoluent par millésime : on ne
# plante JAMAIS sur un code inconnu (libellé de repli par domaine). Voir references/api.md.
ECOLES = {
    "C107": "École maternelle",
    "C108": "École primaire",
    "C201": "Collège",
    "C301": "Lycée d'enseignement général et/ou technologique",
    "C302": "Lycée d'enseignement professionnel",
}
SANTE = {
    "D106": "Urgences",
    "D107": "Maternité",
    "D108": "Centre de santé",
    "D109": "Structure psychiatrique en ambulatoire",
    "D110": "Centre de médecine préventive",
    "D111": "Dialyse",
    "D112": "Hospitalisation à domicile",
    "D113": "Maison de santé pluridisciplinaire",
}
TYPE_LIBELLES = dict(ECOLES)
TYPE_LIBELLES.update(SANTE)

# Qualité de géolocalisation (colonne QUALITE_XY de la BPE).
QUALITE_XY = {"A": "Acceptable", "B": "Bonne", "M": "Mauvaise",
              "_U": "indéterminée", "_Z": "sans objet"}

# Colonnes lues dans le CSV BPE (couche anti-corruption : on isole le reste des ~89 colonnes).
COLS = ("DEPCOM", "TYPEQU", "NOMRS", "LATITUDE", "LONGITUDE", "QUALITE_XY", "TR_DIST_PRECISION")


# --- Utilitaires --------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _vtuple(version):
    """'1.2.3' -> (1, 2, 3) pour comparer des versions sémantiques."""
    parts = []
    for p in str(version or "0").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _urlhash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _csv_encoding(path):
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


def _load_json_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- Registre : choix de la source de données ---------------------------------
def resolve_source(cache_dir, timeout):
    """Choisit l'entrée de dataset à utiliser à partir du registre (GitHub > cache > local).

    Retourne (entry, info). `info` porte la provenance et le drapeau de mise à jour de skill.
    Lève SkillError si aucun registre exploitable ou aucune entrée compatible avec ce skill.
    """
    info = {"registre_source": None, "registry_version": None,
            "maj_skill_disponible": False, "message": None}

    candidates = []  # (label, registre)
    try:
        # GitHub raw sert les .json en text/plain -> require_json=False (parsing JSON validé).
        remote = http_get_json(REGISTRY_URL, timeout=timeout, require_json=False)
        if isinstance(remote, dict) and remote.get("entries"):
            candidates.append(("github", remote))
            try:  # persiste le dernier registre distant connu pour les runs hors-ligne
                os.makedirs(cache_dir, exist_ok=True)
                with open(os.path.join(cache_dir, "registry-bpe.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(remote, fh, ensure_ascii=False)
            except OSError:
                pass
    except SkillError:
        pass  # GitHub injoignable : on se rabat sur le cache / la copie locale

    cached = os.path.join(cache_dir, "registry-bpe.json")
    if os.path.exists(cached):
        try:
            candidates.append(("cache", _load_json_file(cached)))
        except (OSError, ValueError):
            pass
    if os.path.exists(LOCAL_REGISTRY):
        try:
            candidates.append(("local", _load_json_file(LOCAL_REGISTRY)))
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

    skill_v = _vtuple(SKILL_VERSION)
    compatible = [e for e in entries if _vtuple(e.get("min_skill_version", "0")) <= skill_v]
    if not compatible:
        fail("aucune base compatible avec ce skill (v%s) recensée ; mettre à jour le repo du "
             "skill" % SKILL_VERSION,
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
        fail("entrée de registre incomplète pour le millésime %s : 'files' (zone+url) manquant "
             "ou invalide ; corriger dataset-registry.json" % entry.get("millesime"),
             detail={"entry": entry})

    # Existe-t-il un millésime plus récent mais hors de portée de cette version du skill ?
    plus_recents = [e for e in entries
                    if e.get("millesime", 0) > entry.get("millesime", 0)
                    and _vtuple(e.get("min_skill_version", "0")) > skill_v]
    if plus_recents:
        best = max(plus_recents, key=lambda e: e.get("millesime", 0))
        info["maj_skill_disponible"] = True
        info["message"] = (
            "un millésime plus récent (%s) existe mais nécessite un skill >= %s ; "
            "mettez à jour le skill pour des données plus récentes. Utilisation du millésime %s."
            % (best.get("millesime"), best.get("min_skill_version"), entry.get("millesime")))

    return entry, info


# --- Choix du/des fichier(s) à interroger -------------------------------------
# TODO(dette) : le socle registre→cache (resolve_source / select_files / dataset_path) est
# dupliqué à l'identique avec demographie-iris (qui, lui, exploite vraiment le multi-fichiers et
# code_prefixes). Le hisser dans skills/_common/ dès le 4e/5e skill qui réutilise ce pattern,
# pour le mutualiser d'un coup plutôt que de le dupliquer/laisser diverger une 3e fois.
def select_files(entry, zone_arg, code_insee=None):
    """Liste ordonnée des fichiers à essayer pour cette entrée de registre.

    AUCUNE hypothèse géographique codée en dur : la couverture est portée par le registre.
    En `auto` on essaie tous les fichiers déclarés jusqu'à trouver la commune. Un fichier peut
    déclarer `code_prefixes` : ceux dont un préfixe correspond au code commune sont essayés en
    PREMIER (ordre seulement). `--zone <nom>` restreint. (Pour la BPE, une seule zone 'france'.)
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


# --- Téléchargement + cache (identité = hash de l'URL) ------------------------
def _extract_data_csv(zip_path, dest_csv):
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


def dataset_path(entry, file_entry, cache_dir, refresh, timeout):
    """Garantit la présence locale du CSV d'un fichier (zone) déclaré. Retourne (csv_path, meta).

    Cache identifié par le hash de l'URL : si le fichier nommé par cet `urlhash` existe déjà,
    le lien a déjà été téléchargé -> aucun re-téléchargement (sauf --refresh). Si l'URL change
    (nouveau millésime via le registre), l'urlhash change donc le téléchargement se relance.
    """
    zone = file_entry.get("zone")
    url = file_entry.get("url")
    urlhash = _urlhash(url)
    os.makedirs(cache_dir, exist_ok=True)
    csv_path = os.path.join(cache_dir, "bpe-%s.csv" % urlhash)
    zip_path = os.path.join(cache_dir, "bpe-%s.zip" % urlhash)
    side_path = os.path.join(cache_dir, "bpe-%s.json" % urlhash)

    def _read_cache():
        meta = _load_json_file(side_path)
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
        fail("impossible de télécharger la base BPE %s (zone %s) et aucun cache disponible ; "
             "il n'existe pas de base de données à jour recensée pour le skill, mettre à jour "
             "le repo du skill" % (entry.get("millesime"), zone),
             detail={"url": url, "cause": exc.detail or exc.message})

    _extract_data_csv(zip_path, csv_path)
    try:
        os.remove(zip_path)  # le zip n'est plus utile, on ne garde que le CSV extrait
    except OSError:
        pass

    meta = {
        "millesime": entry.get("millesime"),
        "geographie": entry.get("geographie"),
        "zone": zone,
        "url": url,
        "urlhash": urlhash,
        "sha256": _sha256_file(csv_path),
        "size": os.path.getsize(csv_path),
        "telecharge_le": _now_iso(),
        "depuis_cache": False,
    }
    try:
        with open(side_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
    except OSError:
        pass
    return csv_path, meta


# --- Lecture / filtrage du CSV BPE --------------------------------------------
def _keep_type(typequ, all_types):
    """True si le type d'équipement doit être retenu (défaut : ciblés ; --all-types : domaines C/D)."""
    if all_types:
        return typequ[:1] in ("C", "D")
    return typequ in ECOLES or typequ in SANTE


def load_equipements(csv_path, code_commune, all_types):
    """Filtre le CSV BPE sur la commune (DEPCOM) + les types voulus. Retourne (rows, depcom_vu).

    Le fichier-détail pèse ~1,4 Go : on itère en streaming avec csv.reader + indices de colonnes
    (plus léger/rapide que DictReader sur 89 colonnes) et on ne garde en mémoire que les lignes
    retenues. `depcom_vu` distingue « commune absente de la BPE » de « commune présente mais sans
    équipement ciblé » (réponse légitimement vide, pas une erreur).
    """
    enc = _csv_encoding(csv_path)
    # errors="replace" : _csv_encoding ne renifle que les 64 premiers Kio ; en UTF-8 un octet
    # invalide apparu plus loin substituerait un caractère de remplacement plutôt que de lever
    # un UnicodeDecodeError en pleine lecture (les colonnes lues sont ASCII de toute façon).
    with open(csv_path, encoding=enc, errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter=CSV_SEP)
        header = next(reader, [])
        idx = {c: i for i, c in enumerate(header)}
        for col in ("DEPCOM", "TYPEQU", "LATITUDE", "LONGITUDE"):
            if col not in idx:
                fail("colonne %s absente du CSV BPE (format inattendu)" % col,
                     detail={"colonnes_vues": header[:20]})
        di, ti = idx["DEPCOM"], idx["TYPEQU"]
        wanted = {c: idx[c] for c in COLS if c in idx}
        rows, depcom_vu = [], False
        for row in reader:
            if len(row) <= di or row[di].strip() != code_commune:
                continue
            depcom_vu = True
            typ = row[ti].strip()
            if not _keep_type(typ, all_types):
                continue
            rows.append({c: (row[i].strip() if i < len(row) else "")
                         for c, i in wanted.items()})
    return rows, depcom_vu


def _coord(raw):
    """Coordonnée (float) ou chaîne explicative si absente/non numérique (jamais null ambigu)."""
    if raw is None or raw.strip() == "":
        return "indisponible : coordonnée absente du fichier BPE"
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return "indisponible : coordonnée non numérique (%r)" % raw


def _libelle(typequ):
    if typequ in TYPE_LIBELLES:
        return TYPE_LIBELLES[typequ]
    if typequ[:1] == "C":
        return "Équipement d'enseignement (%s)" % typequ
    if typequ[:1] == "D":
        return "Équipement de santé / action sociale (%s)" % typequ
    return typequ


def _qualite(row):
    """Qualité de géolocalisation lisible (QUALITE_XY + précision), ou None si inconnue."""
    qxy = (row.get("QUALITE_XY") or "").strip()
    prec = (row.get("TR_DIST_PRECISION") or "").strip()
    parts = []
    label = QUALITE_XY.get(qxy)
    if label:
        parts.append(label)
    if prec and prec not in ("_U", "_Z"):
        parts.append("%s m" % prec)
    return ", ".join(parts) or None


def build_equipement(loc, row):
    typ = (row.get("TYPEQU") or "").strip()
    lat = _coord(row.get("LATITUDE"))
    lon = _coord(row.get("LONGITUDE"))
    if isinstance(lat, float) and isinstance(lon, float):
        distance = round(haversine_km(loc.lat, loc.lon, lat, lon), 3)
    else:
        distance = "indisponible : coordonnées de l'équipement absentes"
    return C.Equipement(
        type_code=typ,
        type_libelle=_libelle(typ),
        nom=((row.get("NOMRS") or "").strip() or None),
        lat=lat, lon=lon,
        qualite_geoloc=_qualite(row),
        distance_km=distance,
    )


def build_vulnerabilite(loc, args, rows):
    ecoles, sante = [], []
    for row in rows:
        eq = build_equipement(loc, row)
        (ecoles if eq.type_code[:1] == "C" else sante).append(eq)

    # --radius : on retire les équipements trop loin (distance numérique > rayon). Ceux sans
    # coordonnées (distance non numérique) sont conservés : non filtrables, leur absence est
    # honnêtement signalée plutôt que masquée.
    if args.radius is not None:
        def _within(eq):
            return not isinstance(eq.distance_km, (int, float)) or eq.distance_km <= args.radius
        ecoles = [e for e in ecoles if _within(e)]
        sante = [e for e in sante if _within(e)]

    # Tri par distance croissante (équipements sans distance numérique en dernier).
    def _key(eq):
        return eq.distance_km if isinstance(eq.distance_km, (int, float)) else float("inf")
    ecoles.sort(key=_key)
    sante.sort(key=_key)

    commune = C.CommuneEquipements(code=loc.code_insee, nom=loc.commune,
                                   ecoles_count=len(ecoles), sante_count=len(sante))
    return jsonable(C.Vulnerabilite(commune=commune, ecoles=ecoles, sante=sante))


# --- Orchestration ------------------------------------------------------------
def _dataset_block(entry, file_entry, info):
    """Bloc de provenance (forme stable). Mis à jour ensuite via _apply_meta."""
    url = file_entry.get("url")
    return {
        "millesime": entry.get("millesime"),
        "geographie": entry.get("geographie"),
        "zone": file_entry.get("zone"),
        "url": url,
        "urlhash": _urlhash(url) if url else "",
        "telecharge_le": None,
        "sha256": None,
        "depuis_cache": False,
        "registre_source": info["registre_source"],
        "registry_version": info["registry_version"],
        "maj_skill_disponible": info["maj_skill_disponible"],
        "message": info["message"],
    }


def _apply_meta(block, meta):
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


def run(args):
    entry, info = resolve_source(args.cache_dir, args.timeout)

    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    if loc.code_insee is None:                       # entrée par coordonnées -> commune
        loc = reverse_commune(loc.lat, loc.lon, args.timeout)

    files = select_files(entry, args.zone, loc.code_insee)  # ordre piloté par le registre
    dataset_block = _dataset_block(entry, files[0], info)
    out = {"lieu": jsonable(loc), "dataset": dataset_block}

    erreurs = 0
    try:
        # On essaie chaque fichier déclaré jusqu'à trouver la commune (aucune hypothèse codée
        # sur la zone). Le cache par urlhash rend les fichiers déjà vus gratuits.
        found, depcom_vu = None, False
        for f in files:
            csv_path, meta = dataset_path(entry, f, args.cache_dir, args.refresh, args.timeout)
            _apply_meta(dataset_block, meta)         # reflète le dernier fichier chargé
            rows, vu = load_equipements(csv_path, loc.code_insee, args.all_types)
            depcom_vu = depcom_vu or vu
            if rows or vu:               # commune trouvée (avec ou sans équipement ciblé)
                found = rows
                break
        if found is not None or depcom_vu:
            # Commune présente : réponse (éventuellement listes vides = aucun équipement ciblé).
            out["vulnerabilite"] = build_vulnerabilite(loc, args, found or [])
        else:
            out["vulnerabilite"] = {
                "error": "commune %s absente de la BPE %s"
                         % (loc.code_insee, entry.get("millesime")),
                "detail": "aucun équipement recensé pour cette commune dans le fichier-détail "
                          "(zones essayées : %s)" % ", ".join(f.get("zone") for f in files)}
            erreurs += 1
    except SkillError as exc:
        out["vulnerabilite"] = {"error": exc.message, "detail": exc.detail}
        erreurs += 1
    except Exception as exc:  # robustesse : une source ne doit pas tout casser
        # On nomme le type d'exception (et on le trace sur stderr) : sans ça, un bug de parsing
        # ou d'encodage se présenterait comme un opaque « erreur inattendue ».
        sys.stderr.write("vulnerabilite-bpe: exception inattendue (%s) : %s\n"
                         % (type(exc).__name__, exc))
        out["vulnerabilite"] = {"error": "erreur inattendue (%s) : %s"
                                % (type(exc).__name__, exc)}
        erreurs += 1

    return out, (1 if erreurs else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Écoles et établissements de santé d'une commune française "
                    "(fichier-détail BPE de l'INSEE).")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--zone", default="auto",
                        help="Restreindre à une zone déclarée dans le registre. Défaut auto : "
                             "essaie tous les fichiers du millésime (la BPE n'a qu'une zone "
                             "'france').")
    parser.add_argument("--all-types", dest="all_types", action="store_true",
                        help="Élargir aux domaines C (enseignement) et D (santé/action sociale) "
                             "entiers, au lieu des seuls écoles + santé ciblés.")
    parser.add_argument("--radius", type=float, default=None,
                        help="Ne garder que les équipements à moins de RADIUS km du point résolu.")
    parser.add_argument("--cache-dir", dest="cache_dir", default=DEFAULT_CACHE,
                        help="Répertoire de cache des CSV téléchargés (défaut : ./data ou "
                             "$FLOOD_CACHE_DIR).")
    parser.add_argument("--refresh", action="store_true",
                        help="Forcer le re-téléchargement même si le CSV est déjà en cache.")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Timeout HTTP en secondes (téléchargement ~165 Mo). Défaut 120.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        out, code = run(args)
    except SkillError as exc:
        emit_error(exc)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
