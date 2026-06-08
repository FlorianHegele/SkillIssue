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
import json
import os
import re
import sys
import traceback

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    GEO_API, SkillError, SourceConfig, csv_encoding, dataset as ds, emit_error, fail,
    http_get_json, jsonable, resolve_location, reverse_commune,
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

CSV_SEP = ";"
_REPO_ROOT = os.path.dirname(os.path.dirname(SKILL_DIR))
DEFAULT_CACHE = os.environ.get("FLOOD_CACHE_DIR") or os.path.join(_REPO_ROOT, "data")


# Socle registre→cache mutualisé (_common/dataset.py). cache_prefix/registry_cache_name DOIVENT
# être distincts de ceux des autres skills : le cache `data/` est partagé.
_SOURCE = SourceConfig(REGISTRY_URL, LOCAL_REGISTRY, SKILL_VERSION,
                       cache_prefix="cfm", label="CFM", registry_cache_name="registry-cfm.json")

# Alias pour le code de lecture du CSV (et les sondes live qui vérifient l'encodage réel).
_csv_encoding = csv_encoding


# Fines couches d'adaptation : même signature qu'avant, pour que run() et les tests qui les
# mockent (resolve_source / select_files / dataset_path) restent inchangés côté skill.
def resolve_source(cache_dir, timeout):
    return _SOURCE.resolve_source(cache_dir, timeout)


def select_files(entry, zone_arg, code_insee=None):
    return ds.select_files(entry, zone_arg, code_insee)


def dataset_path(entry, file_entry, cache_dir, refresh, timeout):
    return _SOURCE.dataset_path(entry, file_entry, cache_dir, refresh, timeout)


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
def load_rows_and_prefix(csv_path, code_commune, entry):
    """Ouvre le CSV, détermine le préfixe des variables et renvoie (rows, prefix) pour la commune."""
    enc = _csv_encoding(csv_path)
    with open(csv_path, encoding=enc, newline="") as fh:
        reader = csv.DictReader(fh, delimiter=CSV_SEP)
        prefix = resolve_prefix(reader.fieldnames or [], entry)
        rows = [r for r in reader if (r.get("COM") or "").strip() == code_commune]
    return rows, prefix


def build_demographie(loc, args, rows, prefix, zone):
    iris_items = []
    tot_men = tot_fam = tot_mono = 0.0
    has_men = has_fam = has_mono = False
    # Pour le ratio monoparentales/familles : on n'accumule que les IRIS où LES DEUX valeurs
    # sont numériques, sinon le pourcentage mélangerait des périmètres d'IRIS différents
    # (numérateur d'un IRIS, dénominateur d'un autre) — trompeur en cas de secret statistique.
    pair_fam = pair_mono = 0.0
    has_pair = False
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
        fam_num = isinstance(fam, (int, float))
        mono_num = isinstance(mono, (int, float))
        if isinstance(men, (int, float)):
            tot_men += men; has_men = True
        if fam_num:
            tot_fam += fam; has_fam = True
        if mono_num:
            tot_mono += mono; has_mono = True
        if fam_num and mono_num:
            pair_fam += fam; pair_mono += mono; has_pair = True

    # Tri par population décroissante (les valeurs non numériques en dernier).
    iris_items.sort(
        key=lambda it: it.population if isinstance(it.population, (int, float)) else -1,
        reverse=True)

    # Troncature top-N (économie de contexte sur les grandes communes : Paris ≈ 1 000 IRIS).
    # Les totaux ci-dessus sont calculés sur TOUS les IRIS, indépendamment de la troncature.
    total_iris = len(iris_items)
    top = getattr(args, "top", 0) or 0
    shown = iris_items[:top] if top > 0 else iris_items
    tronque = len(shown) < total_iris

    # Base réellement utilisée pour le pourcentage (IRIS où familles ET monoparentales chiffrées),
    # exposée pour lever l'ambiguïté avec monoparentales_total (somme complète, tous IRIS).
    base = ({"monoparentales": round(pair_mono), "familles": round(pair_fam)} if has_pair
            else "indisponible : familles/monoparentales non exploitables")

    commune = C.CommuneSynthese(
        code=loc.code_insee,
        nom=loc.commune,
        population=collect_population(loc, args.timeout),
        iris_count=total_iris,
        menages_total=(round(tot_men) if has_men
                       else "indisponible : aucune valeur ménages exploitable"),
        familles_total=(round(tot_fam) if has_fam
                        else "indisponible : aucune valeur familles exploitable"),
        monoparentales_total=(round(tot_mono) if has_mono
                              else "indisponible : aucune valeur monoparentales exploitable"),
        part_monoparentales_pct=(_pct(pair_mono, pair_fam) if has_pair
                                 else "indisponible : familles/monoparentales non exploitables"),
        part_monoparentales_base=base,
    )

    out = jsonable(C.Demographie(commune=commune, iris=shown, iris_tronque=tronque))
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

    files = select_files(entry, args.zone, loc.code_insee)  # ordre piloté par le registre
    dataset_block = ds.dataset_block(entry, files[0], info)
    out = {"lieu": jsonable(loc), "dataset": dataset_block}

    erreurs = 0
    try:
        # On essaie chaque fichier déclaré jusqu'à trouver la commune (aucune hypothèse codée
        # sur la zone). Le cache par urlhash rend les fichiers déjà vus gratuits.
        found = None
        for f in files:
            csv_path, meta = dataset_path(entry, f, args.cache_dir, args.refresh, args.timeout)
            ds.apply_meta(dataset_block, meta)         # reflète le dernier fichier chargé
            rows, prefix = load_rows_and_prefix(csv_path, loc.code_insee, entry)
            if rows:
                found = (rows, prefix, meta.get("zone"))
                break
        if found:
            rows, prefix, zone = found
            out["demographie"] = build_demographie(loc, args, rows, prefix, zone)
        else:
            out["demographie"] = {
                "error": "aucun IRIS pour la commune %s dans le millésime %s"
                         % (loc.code_insee, entry.get("millesime")),
                "detail": "commune absente de ce millésime ou non couverte par les fichiers "
                          "INSEE disponibles (zones essayées : %s)"
                          % ", ".join(f.get("zone") for f in files)}
            erreurs += 1
    except SkillError as exc:
        out["demographie"] = {"error": exc.message, "detail": exc.detail}
        erreurs += 1
    except Exception as exc:  # robustesse : une source ne doit pas tout casser
        # Bug réel (≠ source en panne) : on dégrade quand même la sortie, mais on trace sur
        # stderr (traceback compris) pour le débogage — sinon l'erreur resterait invisible.
        traceback.print_exc(file=sys.stderr)
        out["demographie"] = {"error": "erreur inattendue : %s: %s"
                                       % (type(exc).__name__, exc)}
        erreurs += 1

    return out, (1 if erreurs else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Population, ménages et familles par IRIS pour une commune française "
                    "(base INSEE Couples-Familles-Ménages).")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--zone", default="auto",
                        help="Restreindre à une zone déclarée dans le registre (ex. metropole, "
                             "com). Défaut auto : essaie tous les fichiers du millésime jusqu'à "
                             "trouver la commune (aucune zone codée en dur).")
    parser.add_argument("--cache-dir", dest="cache_dir", default=DEFAULT_CACHE,
                        help="Répertoire de cache des CSV téléchargés (défaut : ./data ou "
                             "$FLOOD_CACHE_DIR).")
    parser.add_argument("--refresh", action="store_true",
                        help="Forcer le re-téléchargement même si le CSV est déjà en cache.")
    parser.add_argument("--detail", action="store_true",
                        help="Ajouter par IRIS : couples avec/sans enfants et type d'IRIS.")
    parser.add_argument("--top", type=int, default=20,
                        help="Limiter la liste IRIS aux N plus peuplés (économie de contexte sur "
                             "les grandes communes). Les totaux commune restent calculés sur tous "
                             "les IRIS. Défaut 20 ; --top 0 renvoie la liste complète.")
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
