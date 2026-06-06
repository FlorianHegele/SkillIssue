#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demographie-iris — population, ménages et familles par IRIS pour une commune française.

Aide à dimensionner évacuation/hébergement en crue : combien de personnes et de ménages,
et où se concentrent les foyers vulnérables (familles monoparentales), au niveau infra-communal
(IRIS). Source = base INSEE « Couples - Familles - Ménages » par IRIS, un CSV zippé à télécharger
(pas une API JSON). Voir references/api.md.

Mise à jour des données SANS réinstaller le skill : un registre versionné hébergé sur GitHub
(dataset-registry.json) pointe vers le dernier millésime. Le skill prend le dernier millésime
COMPATIBLE avec sa version ; si un millésime plus récent existe mais exige une version de skill
supérieure, il le signale dans sa réponse et continue avec le dernier compatible. Le CSV est mis
en cache, identifié par le hash de son URL (re-téléchargement uniquement si l'URL change).

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    GEO_API, SkillError, emit_error, fail, http_download, http_get_json, jsonable,
    resolve_location, reverse_commune,
)

import contract as C  # noqa: E402  (module local du skill)

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Version & registre -------------------------------------------------------
# Incrémenter SKILL_VERSION en cas de changement cassant de lecture du CSV ; le registre
# utilise min_skill_version pour qu'un vieux skill ne tente pas de lire un schéma incompatible.
SKILL_VERSION = "1.0.0"
REGISTRY_URL = ("https://raw.githubusercontent.com/FlorianHegele/SkillIssue/main/"
                "skills/demographie-iris/dataset-registry.json")
LOCAL_REGISTRY = os.path.join(SKILL_DIR, "dataset-registry.json")

# Départements couverts par le fichier « COM » (collectivités d'outre-mer) ; le reste
# (métropole + DOM 971-974) est dans le fichier « France hors Mayotte ». Mayotte (976) n'est
# dans aucun des deux pour le millésime 2022.
COM_DEPTS = {"975", "977", "978", "986", "987", "988"}

CSV_SEP = ";"
_REPO_ROOT = os.path.dirname(os.path.dirname(SKILL_DIR))
DEFAULT_CACHE = os.environ.get("FLOOD_CACHE_DIR") or os.path.join(_REPO_ROOT, "data")


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
        remote = http_get_json(REGISTRY_URL, timeout=timeout)
        if isinstance(remote, dict) and remote.get("entries"):
            candidates.append(("github", remote))
            try:  # persiste le dernier registre distant connu pour les runs hors-ligne
                os.makedirs(cache_dir, exist_ok=True)
                with open(os.path.join(cache_dir, "registry.json"), "w", encoding="utf-8") as fh:
                    json.dump(remote, fh, ensure_ascii=False)
            except OSError:
                pass
    except SkillError:
        pass  # GitHub injoignable : on se rabat sur le cache / la copie locale

    cached = os.path.join(cache_dir, "registry.json")
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
    label, reg = max(candidates, key=lambda c: c[1].get("registry_version", 0))
    info["registre_source"] = label
    info["registry_version"] = reg.get("registry_version")

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


# --- Choix du fichier (zone) --------------------------------------------------
def choose_zone(zone_arg, code_insee):
    """metropole | com. 'auto' déduit du code département de la commune."""
    if zone_arg in ("metropole", "com"):
        return zone_arg
    code = code_insee or ""
    return "com" if code[:3] in COM_DEPTS else "metropole"


# --- Téléchargement + cache (identité = hash de l'URL) ------------------------
def _extract_data_csv(zip_path, dest_csv):
    """Extrait le CSV de DONNÉES du zip (ignore le `meta_*.CSV`, dictionnaire des variables)."""
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


def dataset_path(entry, zone, cache_dir, refresh, timeout):
    """Garantit la présence locale du CSV de la zone. Retourne (csv_path, meta).

    Cache identifié par le hash de l'URL : si le fichier nommé par cet `urlhash` existe déjà,
    le lien a déjà été téléchargé -> aucun re-téléchargement (sauf --refresh). Si l'URL change
    (nouveau millésime via le registre), l'urlhash change donc le téléchargement se relance.
    """
    url = entry["url_com"] if zone == "com" else entry["url_metropole"]
    urlhash = _urlhash(url)
    os.makedirs(cache_dir, exist_ok=True)
    csv_path = os.path.join(cache_dir, "cfm-%s.csv" % urlhash)
    zip_path = os.path.join(cache_dir, "cfm-%s.zip" % urlhash)
    side_path = os.path.join(cache_dir, "cfm-%s.json" % urlhash)

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
        fail("impossible de télécharger la base CFM %s (zone %s) et aucun cache disponible ; "
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


# --- Lecture / parsing du CSV -------------------------------------------------
def resolve_prefix(header, entry):
    """Préfixe des variables (ex. C22_). Vérifie celui du registre, sinon auto-détecte."""
    pref = entry.get("prefix")
    if pref and (pref + "MEN") in header:
        return pref
    for col in header:
        m = re.match(r"^(C\d{2}_)MEN$", col)
        if m:
            return m.group(1)
    fail("colonnes ménages (<prefix>MEN) introuvables dans le CSV",
         detail={"prefix_attendu": pref, "colonnes_vues": header[:15]})


def _num(row, col):
    """Valeur numérique d'une colonne, ou chaîne explicative (jamais null ambigu)."""
    raw = row.get(col)
    if raw is None:
        return "indisponible : colonne %s absente du fichier" % col
    raw = raw.strip()
    if raw == "":
        return "indisponible : valeur absente"
    if raw.lower() == "s":
        return "indisponible : donnée soumise au secret statistique"
    try:
        return int(round(float(raw.replace(",", "."))))
    except ValueError:
        return "indisponible : valeur non numérique (%r)" % raw


def _pct(part, total):
    try:
        return round(100.0 * part / total, 1)
    except (TypeError, ZeroDivisionError):
        return "indisponible : total nul ou non numérique"


# --- Adaptateur : population commune (geo.api) --------------------------------
def collect_population(loc, timeout):
    if not loc.code_insee:
        return "indisponible : code commune inconnu"
    try:
        data = http_get_json(GEO_API, params={"code": loc.code_insee, "fields": "population",
                                              "format": "json"}, timeout=timeout)
    except SkillError as exc:
        return "indisponible : %s" % exc.message
    if isinstance(data, list) and data and isinstance(data[0].get("population"), (int, float)):
        return data[0]["population"]
    return "indisponible : population commune non renvoyée par geo.api"


# --- Adaptateur : démographie IRIS (CSV INSEE CFM) ----------------------------
def collect_demographie(loc, args, csv_path, entry, zone):
    enc = _csv_encoding(csv_path)
    with open(csv_path, encoding=enc, newline="") as fh:
        reader = csv.DictReader(fh, delimiter=CSV_SEP)
        header = reader.fieldnames or []
        prefix = resolve_prefix(header, entry)
        rows = [r for r in reader if (r.get("COM") or "").strip() == loc.code_insee]

    if not rows:
        return {"error": "aucun IRIS pour la commune %s dans la base CFM %s (zone %s)"
                         % (loc.code_insee, entry.get("millesime"), zone),
                "detail": "commune absente du millésime, ou hors couverture du fichier "
                          "(ex. Mayotte 976 non incluse)"}

    iris_items = []
    tot_men = tot_fam = tot_mono = 0.0
    has_men = has_fam = has_mono = False
    for r in rows:
        men = _num(r, prefix + "MEN")
        fam = _num(r, prefix + "FAM")
        mono = _num(r, prefix + "MENFAMMONO")
        pmen = _num(r, prefix + "PMEN")
        libelle = (r.get("LIBIRIS") or r.get("LIB_IRIS") or "").strip()
        if not libelle:
            libelle = "indisponible : libellé IRIS absent du fichier (zone %s)" % zone
        iris_items.append(C.IrisItem(
            code=(r.get("IRIS") or "").strip(),
            libelle=libelle,
            type_iris=((r.get("TYP_IRIS") or "").strip() or None) if args.detail else None,
            population=pmen, menages=men, familles=fam, monoparentales=mono,
            couples_avec_enfants=_num(r, prefix + "MENCOUPAENF") if args.detail else None,
            couples_sans_enfants=_num(r, prefix + "MENCOUPSENF") if args.detail else None,
        ))
        if isinstance(men, (int, float)):
            tot_men += men; has_men = True
        if isinstance(fam, (int, float)):
            tot_fam += fam; has_fam = True
        if isinstance(mono, (int, float)):
            tot_mono += mono; has_mono = True

    # Tri par population décroissante (les valeurs non numériques en dernier).
    iris_items.sort(
        key=lambda it: it.population if isinstance(it.population, (int, float)) else -1,
        reverse=True)

    commune = C.CommuneSynthese(
        code=loc.code_insee,
        nom=loc.commune,
        population=collect_population(loc, args.timeout),
        iris_count=len(iris_items),
        menages_total=(round(tot_men) if has_men
                       else "indisponible : aucune valeur ménages exploitable"),
        familles_total=(round(tot_fam) if has_fam
                        else "indisponible : aucune valeur familles exploitable"),
        monoparentales_total=(round(tot_mono) if has_mono
                              else "indisponible : aucune valeur monoparentales exploitable"),
        part_monoparentales_pct=(_pct(tot_mono, tot_fam) if (has_fam and has_mono)
                                 else "indisponible : familles/monoparentales non exploitables"),
    )

    out = jsonable(C.Demographie(commune=commune, iris=iris_items))
    if not args.detail:  # champs réservés à --detail : on ne les expose pas par défaut
        for it in out["iris"]:
            for k in ("type_iris", "couples_avec_enfants", "couples_sans_enfants"):
                it.pop(k, None)
    return out


# --- Orchestration ------------------------------------------------------------
def run(args):
    entry, info = resolve_source(args.cache_dir, args.timeout)

    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    if loc.code_insee is None:                       # entrée par coordonnées -> commune
        loc = reverse_commune(loc.lat, loc.lon, args.timeout)

    zone = choose_zone(args.zone, loc.code_insee)
    url = entry["url_com"] if zone == "com" else entry["url_metropole"]
    dataset_block = {
        "millesime": entry.get("millesime"),
        "geographie": entry.get("geographie"),
        "zone": zone,
        "url": url,
        "urlhash": _urlhash(url),
        "telecharge_le": None,
        "sha256": None,
        "depuis_cache": False,
        "registre_source": info["registre_source"],
        "registry_version": info["registry_version"],
        "maj_skill_disponible": info["maj_skill_disponible"],
        "message": info["message"],
    }
    out = {"lieu": jsonable(loc), "dataset": dataset_block}

    erreurs = 0
    try:
        csv_path, meta = dataset_path(entry, zone, args.cache_dir, args.refresh, args.timeout)
        dataset_block["telecharge_le"] = meta.get("telecharge_le")
        dataset_block["sha256"] = meta.get("sha256")
        dataset_block["depuis_cache"] = meta.get("depuis_cache", False)
        if meta.get("message"):
            dataset_block["message"] = (
                "%s | %s" % (dataset_block["message"], meta["message"])
                if dataset_block["message"] else meta["message"])
        result = collect_demographie(loc, args, csv_path, entry, zone)
        out["demographie"] = result
        if isinstance(result, dict) and "error" in result:
            erreurs += 1
    except SkillError as exc:
        out["demographie"] = {"error": exc.message, "detail": exc.detail}
        erreurs += 1
    except Exception as exc:  # robustesse : une source ne doit pas tout casser
        out["demographie"] = {"error": "erreur inattendue : %s" % exc}
        erreurs += 1

    return out, (1 if erreurs else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Population, ménages et familles par IRIS pour une commune française "
                    "(base INSEE Couples-Familles-Ménages).")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--zone", choices=["auto", "metropole", "com"], default="auto",
                        help="Fichier INSEE à utiliser : metropole (+ DOM 971-974), com "
                             "(collectivités d'outre-mer), ou auto (déduit du code commune). "
                             "Défaut auto.")
    parser.add_argument("--cache-dir", dest="cache_dir", default=DEFAULT_CACHE,
                        help="Répertoire de cache des CSV téléchargés (défaut : ./data ou "
                             "$FLOOD_CACHE_DIR).")
    parser.add_argument("--refresh", action="store_true",
                        help="Forcer le re-téléchargement même si le CSV est déjà en cache.")
    parser.add_argument("--detail", action="store_true",
                        help="Ajouter par IRIS : couples avec/sans enfants et type d'IRIS.")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Timeout HTTP en secondes (téléchargements ~20 Mo). Défaut 60.")
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
