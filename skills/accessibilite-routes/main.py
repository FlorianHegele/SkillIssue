#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""accessibilite-routes — ouvrages routiers vulnérables à l'inondation autour d'un lieu.

Interroge OpenStreetMap via Overpass (sans clé, voir references/api.md) et repère les points
de franchissement susceptibles d'être coupés par l'eau, vers le contrat de contract.py /
contract.schema.json :
  - gués (ford)                          : route qui traverse un cours d'eau à niveau
  - ponts (bridge)                        : franchissement, accès parfois bas
  - tunnels (tunnel)                      : point bas pouvant se remplir
  - passages inférieurs (layer négatif)   : points bas du réseau
  - zones inondables (flood_prone/hazard) : tag d'aléa OSM (rare — voir la note de sortie)

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
OSM cartographie le réseau et les ouvrages, PAS l'aléa : voir le champ `note` de la sortie.
"""

import argparse
import json
import os
import sys

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    SkillError, emit_error, fail, haversine_km, http_get_json, jsonable, resolve_location,
    version,
)

import contract as C  # noqa: E402  (module local du skill)
from _common import overpass as _ov  # noqa: E402  (client Overpass mutualisé)

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(SKILL_DIR))
# Répertoire de cache partagé (ici : uniquement la vérification de version du skill, pas de dataset).
DEFAULT_CACHE = os.environ.get("FLOOD_CACHE_DIR") or os.path.join(_REPO_ROOT, "data")

# --- Endpoints Overpass : client mutualisé dans _common/overpass.py -----------
OVERPASS_PRIMARY = _ov.PRIMARY      # conservés comme noms de module (sondes live, lisibilité)
OVERPASS_MIRROR = _ov.MIRROR

DEFAULT_RADIUS_M = 1500
MAX_RADIUS_M = 5000          # garde-fou fair-use : scoper toujours (clés ford/flood non indexées)

NOTE = ("OpenStreetMap cartographie le réseau routier et les ouvrages (ponts, tunnels, gués), "
        "pas l'aléa d'inondation. L'absence de tag flood_prone/hazard ne signifie PAS "
        "« non vulnérable » (ces tags sont très rares). Pour un vrai jugement d'aléa, croiser "
        "avec Géorisques / data.gouv.fr (zonages TRI, « Risque d'inondation »).")

# Tags OSM conservés dans la sortie (les autres sont écartés : sortie lean, cf. CLAUDE.md).
RELEVANT_TAGS = ("highway", "bridge", "tunnel", "ford", "layer", "intermittent",
                 "waterway", "name", "ref", "man_made", "culvert", "flood_prone", "hazard")

# Voies NON carrossables exclues côté Overpass : le skill sert à planifier l'accès des véhicules
# et des secours, une passerelle piétonne ou un escalier en tunnel n'est pas un « ouvrage routier
# à risque » ici. Les gués sont souvent un node sans tag highway -> non filtrés de cette façon.
EXCLUDE_HIGHWAY = "footway|steps|path|cycleway|pedestrian|bridleway|corridor"


# --- Requête Overpass ---------------------------------------------------------
def build_query(lat, lon, radius_m, timeout):
    """Assemble le QL : union scopée par `around:` (jamais à l'échelle nationale).

    `out tags center;` = point représentatif + tags, léger (on n'expose pas le tracé complet :
    l'osm_id suffit à le récupérer à la demande sur openstreetmap.org).
    Filtres : voie carrossable présente et non piétonne ; `layer` scopé aux valeurs négatives
    (points bas) ; flood_prone/hazard restreints aux voies (pas de polygones de zone hors-sujet).
    """
    out_stmt = "out tags center;"
    a = "around:%d,%s,%s" % (int(radius_m), lat, lon)
    road = '["highway"]["highway"!~"^(%s)$"]' % EXCLUDE_HIGHWAY      # carrossable présente
    not_foot = '["highway"!~"^(%s)$"]' % EXCLUDE_HIGHWAY             # exclut piéton, tolère absence
    parts = [
        'way %s["ford"]["ford"!="no"](%s);' % (not_foot, a),
        'node["ford"]["ford"!="no"](%s);' % a,
        'way %s["bridge"]["bridge"!="no"](%s);' % (road, a),
        'way %s["tunnel"]["tunnel"!="no"](%s);' % (road, a),
        'way %s["layer"~"^-"](%s);' % (road, a),
        'way %s["flood_prone"="yes"](%s);' % (road, a),
        'way %s["hazard"="flooding"](%s);' % (road, a),
    ]
    return "[out:json][timeout:%d];\n(\n  %s\n);\n%s" % (
        int(timeout), "\n  ".join(parts), out_stmt)


# La garde anti-« faux secteur vide » (remark de timeout/OOM serveur rendu en 200) et le repli
# primaire→miroir vivent dans _common/overpass.py — partagés à l'identique avec
# logistique-hebergement. On expose des wrappers de même signature qu'avant : les tests patchent
# main.overpass_query / main.http_get_json et appellent main._check_overpass_remark.
_check_overpass_remark = _ov.check_remark


def overpass_query(ql, timeout):
    # get_json = http_get_json (global du module), pour que le patch test de main.http_get_json
    # reste effectif et que le repli miroir + check_remark s'appliquent. Le QL est court (~600 car).
    return _ov.query(ql, timeout, get_json=http_get_json)


# --- Classification d'un élément OSM ------------------------------------------
def _layer_int(raw):
    """`layer` OSM en entier (None si absent/non parsable). Tolère les formes 'a;b'."""
    if raw is None:
        return None
    try:
        return int(str(raw).split(";")[0])
    except (TypeError, ValueError):
        return None


def classify(tags):
    """Type d'ouvrage à risque, par ordre de priorité, ou None si l'élément n'est pas à risque.

    Un `layer` n'est un « passage inférieur » que s'il est NÉGATIF et que l'élément n'est ni
    pont ni tunnel (un layer positif sans bridge = simple empilement, écarté).
    """
    ford = tags.get("ford")
    if ford and ford != "no":
        return "gué"
    tunnel = tags.get("tunnel")
    if tunnel and tunnel != "no":
        return "tunnel"
    bridge = tags.get("bridge")
    if bridge and bridge != "no":
        return "pont"
    layer = _layer_int(tags.get("layer"))
    if layer is not None and layer < 0:
        return "passage_inférieur"
    if tags.get("flood_prone") == "yes" or tags.get("hazard") == "flooding":
        return "zone_inondable"
    return None


# --- Construction d'un Ouvrage ------------------------------------------------
def _point(el):
    """Point représentatif (lat, lon) d'un élément, ou (None, None) si introuvable.

    Node -> lat/lon directs. Way avec `out tags center;` -> el['center'].
    """
    if el.get("type") == "node" and "lat" in el and "lon" in el:
        return el.get("lat"), el.get("lon")
    center = el.get("center")
    if isinstance(center, dict):
        return center.get("lat"), center.get("lon")
    return None, None


def build_ouvrage(loc, el, kind):
    tags = el.get("tags", {}) or {}
    osm_id = "%s/%s" % (el.get("type"), el.get("id"))
    lat, lon = _point(el)
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        lat_out, lon_out = float(lat), float(lon)
        distance = round(haversine_km(loc.lat, loc.lon, lat_out, lon_out), 3)
    else:
        lat_out = lon_out = "indisponible : position absente de la réponse Overpass"
        distance = "indisponible : position absente, distance non calculable"
    return C.Ouvrage(
        osm_id=osm_id,
        kind=kind,
        nom=(tags.get("name") or tags.get("ref") or None),
        highway=tags.get("highway"),
        lat=lat_out,
        lon=lon_out,
        distance_km=distance,
        tags={k: tags[k] for k in RELEVANT_TAGS if k in tags},
    )


# --- Adaptateur : accessibilité via Overpass ----------------------------------
def collect_accessibilite(loc, args):
    ql = build_query(loc.lat, loc.lon, args.radius_m, args.timeout)
    data = overpass_query(ql, args.timeout)

    counts = {"gué": 0, "tunnel": 0, "pont": 0, "passage_inférieur": 0, "zone_inondable": 0}
    retenus, seen = [], set()
    for el in data.get("elements", []):
        osm_id = "%s/%s" % (el.get("type"), el.get("id"))
        if osm_id in seen:
            continue
        kind = classify(el.get("tags", {}) or {})
        if kind is None:
            continue
        seen.add(osm_id)
        counts[kind] += 1
        retenus.append(build_ouvrage(loc, el, kind))

    # Tri par distance croissante ; position absente (distance non numérique) rejetée en fin.
    retenus.sort(key=lambda ouv: (ouv.distance_km
                                  if isinstance(ouv.distance_km, (int, float))
                                  else float("inf")))

    # --limit borne la LISTE ; le résumé compte TOUS les ouvrages trouvés. Pas de champ dédié
    # « tronqué » : la troncature se déduit de len(ouvrages_a_risque) < resume.ouvrages_total
    # (l'IA la détecte ainsi, cf. SKILL.md « Sortie »). Les ouvrages listés sont les plus proches
    # (tri par distance). limit >= 0 garanti par run() ; limit == 0 -> liste vide (résumé seul).
    ouvrages_out = [jsonable(ouv) for ouv in retenus[:args.limit]]

    resume = C.Resume(
        ouvrages_total=sum(counts.values()),
        gues=counts["gué"], ponts=counts["pont"], tunnels=counts["tunnel"],
        passages_inferieurs=counts["passage_inférieur"], zones_inondables=counts["zone_inondable"],
    )
    out = jsonable(C.Accessibilite(rayon_m=int(args.radius_m), resume=resume,
                                   ouvrages_a_risque=[], note=NOTE))
    out["ouvrages_a_risque"] = ouvrages_out
    return out


# --- Orchestration ------------------------------------------------------------
def run(args):
    if args.radius_m <= 0 or args.radius_m > MAX_RADIUS_M:
        fail("rayon hors bornes : %s m (attendu 1..%d)" % (args.radius_m, MAX_RADIUS_M),
             detail="Overpass doit rester scopé (clés ford/flood non indexées). "
                    "Réduire --radius-m, ou lancer plusieurs requêtes ciblées.")
    if args.limit < 0:
        fail("--limit négatif (%d) invalide : attendre un entier >= 0 "
             "(0 = résumé seul, sans liste détaillée)" % args.limit)
    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    out = {"lieu": jsonable(loc)}
    erreurs = 0
    try:
        out["accessibilite"] = collect_accessibilite(loc, args)
    except SkillError as exc:
        out["accessibilite"] = {"error": exc.message, "detail": exc.detail}
        erreurs += 1
    except Exception as exc:  # robustesse : une exception inattendue ne doit pas crasher le skill
        sys.stderr.write("accessibilite-routes: exception inattendue (%s) : %s\n"
                         % (type(exc).__name__, exc))
        out["accessibilite"] = {"error": "erreur inattendue (%s) : %s" % (type(exc).__name__, exc)}
        erreurs += 1
    return out, (1 if erreurs else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Ouvrages routiers vulnérables à l'inondation (gués, ponts, tunnels, "
                    "passages inférieurs) autour d'une commune ou d'un point, via OSM/Overpass.")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--radius-m", dest="radius_m", type=int, default=DEFAULT_RADIUS_M,
                        help="Rayon de recherche en mètres (défaut %(default)s, max "
                             + str(MAX_RADIUS_M) + ").")
    parser.add_argument("--limit", type=int, default=100,
                        help="Nombre max d'ouvrages listés, triés par distance (défaut "
                             "%(default)s). Le résumé compte tous les ouvrages trouvés.")
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="Timeout Overpass en secondes. Défaut 25.")
    parser.add_argument("--cache-dir", dest="cache_dir", default=DEFAULT_CACHE,
                        help="Répertoire de cache (vérification de mise à jour du skill). "
                             "Défaut : ./data ou $FLOOD_CACHE_DIR.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Vérification de mise à jour du skill (best-effort, jamais bloquante) : reportée dans la
    # sortie ET sur stderr en cas d'échec, pour que l'IA propose une MAJ si le skill est périmé.
    skill_block = version.check_update(SKILL_DIR, args.cache_dir, timeout=min(args.timeout, 10))
    try:
        out, code = run(args)
    except SkillError as exc:
        emit_error(exc, skill=skill_block)
        return 2
    out["skill"] = skill_block
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
