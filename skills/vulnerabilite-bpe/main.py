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
distance au point résolu (tri croissant) ; --radius restreint à un rayon en km. Chaque liste est
limitée aux --top équipements les plus proches (défaut 50, 0 = illimité) pour borner la taille de
sortie sur les grandes communes ; la coupe n'est JAMAIS silencieuse (compteurs = totaux + `note`).

Mise à jour des données SANS réinstaller le skill : un registre versionné hébergé sur GitHub
(dataset-registry.json) pointe vers le dernier millésime ; le skill prend le dernier millésime
COMPATIBLE avec sa version (sinon le signale et continue). Le socle registre→cache est mutualisé
dans _common/dataset.py (partagé avec demographie-iris) ; le CSV est mis en cache, identifié par
le hash de son URL (re-téléchargement uniquement si l'URL change).

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
"""

import argparse
import csv
import json
import os
import sys

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    SkillError, SourceConfig, csv_encoding, dataset as ds, emit_error, fail, haversine_km,
    jsonable, resolve_location, reverse_commune,
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

# Socle registre→cache mutualisé (_common/dataset.py). cache_prefix/registry_cache_name DOIVENT
# être distincts de ceux des autres skills : le cache `data/` est partagé.
_SOURCE = SourceConfig(REGISTRY_URL, LOCAL_REGISTRY, SKILL_VERSION,
                       cache_prefix="bpe", label="BPE", registry_cache_name="registry-bpe.json")

# Alias pour le code de lecture du CSV (et les sondes live qui vérifient l'encodage réel).
_csv_encoding = csv_encoding


# Fines couches d'adaptation : même signature qu'avant, pour que run() et les tests qui les
# mockent (resolve_source / select_files / dataset_path) restent inchangés côté skill.
def resolve_source(cache_dir, timeout):
    return _SOURCE.resolve_source(cache_dir, timeout)


def select_files(entry, zone_arg, code_insee=None):
    # NB : `code_insee` ne sert qu'à l'optimisation par `code_prefixes` du socle (essai prioritaire
    # du fichier couvrant le code). La BPE est NATIONALE (zone unique 'france') -> ce paramètre est
    # inerte ici ; on le transmet quand même pour rester générique si un futur millésime se découpe.
    return ds.select_files(entry, zone_arg, code_insee)


def dataset_path(entry, file_entry, cache_dir, refresh, timeout):
    return _SOURCE.dataset_path(entry, file_entry, cache_dir, refresh, timeout)


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
    # TR_DIST_PRECISION est une tranche de distance en mètres (« < 100 », « [100 - 500[ ») ;
    # on la préfixe explicitement pour qu'elle se lise comme une précision, pas une mesure.
    if prec and prec not in ("_U", "_Z"):
        parts.append("précision %s m" % prec)
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

    # Compteurs = totaux retenus (après --radius, AVANT --top) : la borne de sortie ne fausse
    # jamais le décompte. On garde ensuite les `--top` plus proches par liste pour borner la
    # taille de réponse sur les grandes communes (--top 0 = illimité).
    total_ecoles, total_sante = len(ecoles), len(sante)
    note = None
    top = args.top
    if top and top > 0 and (total_ecoles > top or total_sante > top):
        note = ("listes limitées aux %d équipements les plus proches (--top) : %d écoles et %d "
                "établissements de santé au total dans le périmètre ; augmentez --top, ou affinez "
                "avec --radius / --lat / --lon pour un secteur précis."
                % (top, total_ecoles, total_sante))
        sys.stderr.write("vulnerabilite-bpe: %s\n" % note)
        ecoles = ecoles[:top]
        sante = sante[:top]

    commune = C.CommuneEquipements(code=loc.code_insee, nom=loc.commune,
                                   ecoles_count=total_ecoles, sante_count=total_sante)
    return jsonable(C.Vulnerabilite(commune=commune, ecoles=ecoles, sante=sante, note=note))


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
        found, depcom_vu = None, False
        for i, f in enumerate(files):
            csv_path, meta = dataset_path(entry, f, args.cache_dir, args.refresh, args.timeout)
            # Le fichier-détail BPE pèse ~1,4 Go : enchaîner plusieurs téléchargements (registre
            # multi-fichiers) serait très coûteux. La BPE n'a qu'une zone 'france' aujourd'hui ;
            # si un jour ce n'est plus le cas, on le signale plutôt que de le subir en silence.
            if i > 0 and not meta.get("depuis_cache"):
                sys.stderr.write("vulnerabilite-bpe: téléchargement d'un fichier BPE "
                                 "supplémentaire (zone %s) — opération lourde.\n" % meta.get("zone"))
            ds.apply_meta(dataset_block, meta)       # reflète le dernier fichier chargé
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
    parser.add_argument("--top", type=int, default=50,
                        help="Nombre max d'équipements par liste (écoles / santé), les plus "
                             "proches d'abord (défaut 50 ; 0 = illimité). La troncature n'est pas "
                             "silencieuse : les compteurs restent les totaux et un champ `note` "
                             "indique combien d'équipements existent.")
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
