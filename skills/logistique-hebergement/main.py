#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logistique-hebergement — lieux d'hébergement des sinistrés autour d'un point, via OSM/Overpass.

Interroge OpenStreetMap via Overpass (sans clé, voir references/api.md) et recense les lieux
réquisitionnables pour héberger les sinistrés à l'écart de la zone inondée, vers le contrat de
contract.py / contract.schema.json :
  - hôtels (tourism=hotel)
  - gymnases / salles de sport (leisure=sports_centre|fitness_centre, building=sports_hall)
  - écoles (amenity=school)
  - salles communales (amenity=community_centre)

Pour chaque lieu, une CAPACITÉ d'accueil (couchages) : valeur OSM si un tag explicite existe,
sinon ESTIMÉE et étiquetée comme telle (hôtel ≈ chambres × 2 ou défaut par étoiles ;
gymnase/école/salle ≈ emprise au sol / 4 m² par couchage — surface calculée via `out geom`).

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
"""

import argparse
import json
import math
import os
import sys

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    SkillError, emit_error, fail, haversine_km, http_get_json, jsonable, resolve_location,
)

import contract as C  # noqa: E402  (module local du skill)

# --- Endpoints (vérifiés live le 05/06/2026, voir references/api.md) ----------
OVERPASS_PRIMARY = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRROR = "https://overpass.kumi.systems/api/interpreter"

DEFAULT_RADIUS_M = 2000
MAX_RADIUS_M = 5000          # garde-fou fair-use : scoper toujours (jamais de scan national)

SURFACE_PAR_COUCHAGE_M2 = 4  # ~4 m² par couchage sur un hébergement collectif (gymnase, salle…)
# Chambres par défaut selon la classe d'étoiles, faute de tag `rooms` (estimation grossière).
CHAMBRES_DEFAUT_PAR_ETOILE = {1: 15, 2: 25, 3: 40, 4: 70, 5: 100}
CHAMBRES_DEFAUT_HOTEL = 30   # hôtel sans rooms ni étoiles exploitables

NOTE = ("Capacités d'accueil très peu renseignées dans OpenStreetMap : sauf tag explicite "
        "(source « osm »), elles sont ESTIMÉES (hôtel ≈ chambres × 2 ou défaut par étoiles ; "
        "gymnase/école/salle ≈ emprise au sol / 4 m² par couchage) — ordres de grandeur à "
        "confirmer sur place. ⚠ Quand l'emprise est une PARCELLE (polygone leisure/amenity sans "
        "tag building, voir capacite_methode), elle englobe souvent des espaces extérieurs "
        "(cours, stades, parkings) : la capacité est alors un MAJORANT, pas la surface utile "
        "intérieure. L'emprise ne tient pas compte des étages ni du mobilier. Les centres "
        "d'hébergement officiels relèvent des Plans Communaux de Sauvegarde, rarement publiés en "
        "open data : cette liste recense des lieux CANDIDATS, pas des abris validés.")

# Tags OSM conservés dans la sortie (les autres sont écartés : sortie lean, cf. CLAUDE.md).
RELEVANT_TAGS = ("name", "tourism", "leisure", "amenity", "building", "rooms", "beds",
                 "capacity", "capacity:beds", "capacity:persons", "stars", "operator")


# --- Requête Overpass ---------------------------------------------------------
def build_query(lat, lon, radius_m, timeout):
    """Assemble le QL : union scopée par `around:` (jamais à l'échelle nationale).

    `nwr` = nodes + ways + relations en une passe. `out geom;` joint le tracé de chaque way/relation
    dans la RÉPONSE (la requête reste courte) : indispensable au calcul de l'emprise au sol, base de
    l'estimation de capacité des gymnases/écoles/salles. Le tracé n'est exposé dans la sortie
    qu'avec --geometry ; sinon il sert au calcul puis est retiré.
    """
    a = "around:%d,%s,%s" % (int(radius_m), lat, lon)
    parts = [
        'nwr["tourism"="hotel"](%s);' % a,
        'nwr["leisure"="sports_centre"](%s);' % a,
        'nwr["leisure"="fitness_centre"](%s);' % a,
        'nwr["building"="sports_hall"](%s);' % a,
        'nwr["amenity"="school"](%s);' % a,
        'nwr["amenity"="community_centre"](%s);' % a,
    ]
    return "[out:json][timeout:%d];\n(\n  %s\n);\nout geom;" % (
        int(timeout), "\n  ".join(parts))


def overpass_query(ql, timeout):
    """GET du QL sur Overpass (le QL passe en query-string `?data=`), avec repli sur le miroir.
    Lève SkillError si les deux échouent.

    `http_get_json` rejette déjà les pages HTML d'erreur (406/429/504 servies en 200) via la garde
    Content-Type, et retente avec backoff. Marge de timeout HTTP au-dessus du `[timeout:]` QL.
    """
    http_timeout = timeout + 15
    try:
        return http_get_json(OVERPASS_PRIMARY, params={"data": ql}, timeout=http_timeout)
    except SkillError as exc_primary:
        try:
            return http_get_json(OVERPASS_MIRROR, params={"data": ql}, timeout=http_timeout)
        except SkillError as exc_mirror:
            fail("Overpass indisponible (serveur principal et miroir)",
                 detail={"principal": exc_primary.message, "miroir": exc_mirror.message})


# --- Classification d'un élément OSM ------------------------------------------
def classify(tags):
    """Type de lieu d'hébergement candidat, ou None si l'élément n'en est pas un.

    Ordre : un tourism=hotel prime ; sinon les variantes de gymnase ; puis école ; puis salle.
    """
    if tags.get("tourism") == "hotel":
        return "hôtel"
    if (tags.get("leisure") in ("sports_centre", "fitness_centre")
            or tags.get("building") == "sports_hall"):
        return "gymnase"
    if tags.get("amenity") == "school":
        return "école"
    if tags.get("amenity") == "community_centre":
        return "salle_communale"
    return None


# --- Capacité (valeur OSM ou estimation étiquetée) ----------------------------
def _num(raw):
    """Nombre strictement positif lu dans une valeur de tag OSM, sinon None. Tolère 'a;b', virgule."""
    if raw is None:
        return None
    try:
        v = float(str(raw).split(";")[0].replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def estimate_capacity(type_, tags, surface_m2):
    """(capacite, source, methode) pour un lieu.

    capacite = nombre de couchages : valeur numérique (lue ou estimée) ou chaîne « indisponible ».
    source = "osm" (tag explicite non ambigu) | "estimee" (calculée) | "indisponible".
    methode = explication courte (traçabilité de l'estimation).
    """
    # 1) Compteurs de couchages explicites et non ambigus, valables pour tout type.
    for key in ("capacity:beds", "beds"):
        n = _num(tags.get(key))
        if n is not None:
            return float(round(n)), "osm", "tag %s=%s" % (key, tags[key])

    if type_ == "hôtel":
        # 2a) hôtel : capacity:persons / capacity = capacité d'accueil directe.
        for key in ("capacity:persons", "capacity"):
            n = _num(tags.get(key))
            if n is not None:
                return float(round(n)), "osm", "tag %s=%s" % (key, tags[key])
        # 2b) rooms -> ~2 couchages par chambre.
        rooms = _num(tags.get("rooms"))
        if rooms is not None:
            return float(round(rooms * 2)), "estimee", "rooms×2 (%d chambres)" % int(rooms)
        # 2c) défaut grossier par classe d'étoiles (chambres par défaut × 2 couchages).
        stars = _num(tags.get("stars"))
        if stars is not None:
            chambres = CHAMBRES_DEFAUT_PAR_ETOILE.get(int(stars), CHAMBRES_DEFAUT_HOTEL)
            return (float(chambres * 2), "estimee",
                    "défaut %d★ : %d chambres × 2" % (int(stars), chambres))
    else:
        # 3) gymnase / école / salle : estimation par emprise au sol.
        #    (on n'utilise PAS un éventuel tag `capacity`, ambigu ici : spectateurs/élèves.)
        #    Distinction clé : un tracé `building` = empreinte d'un BÂTIMENT (estimation
        #    plausible) ; un polygone `leisure`/`amenity` sans building = PARCELLE/terrain qui
        #    englobe souvent des espaces extérieurs (cours, stades, parkings) -> la surface est
        #    un MAJORANT, on l'étiquette comme tel (estimation tolérée si clairement étiquetée).
        if isinstance(surface_m2, (int, float)):
            cap = int(surface_m2 // SURFACE_PAR_COUCHAGE_M2)
            if cap > 0:
                if tags.get("building"):
                    methode = ("surface bâtie %d m² / %d m² par couchage"
                               % (round(surface_m2), SURFACE_PAR_COUCHAGE_M2))
                else:
                    methode = ("surface parcelle %d m² / %d m² par couchage "
                               "(terrain : peut inclure des espaces extérieurs — majorant)"
                               % (round(surface_m2), SURFACE_PAR_COUCHAGE_M2))
                return float(cap), "estimee", methode

    return ("indisponible : aucune donnée de capacité ni surface exploitable",
            "indisponible", "—")


# --- Géométrie ----------------------------------------------------------------
def footprint_m2(geometry):
    """Emprise au sol (m²) d'un anneau de points {lat,lon}, via shoelace en projection
    équirectangulaire locale (math pur, comme haversine_km — pas de shapely/pyproj).

    Chaîne explicative si non calculable (élément ponctuel, géométrie absente ou dégénérée).
    Surface non signée. L'anneau est fermé au besoin.
    """
    if not geometry or not isinstance(geometry, list):
        return "indisponible : pas de géométrie (élément ponctuel ou non surfacique)"
    pts = [(p["lat"], p["lon"]) for p in geometry
           if isinstance(p, dict)
           and isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float))]
    if len(pts) < 3:
        return "indisponible : géométrie insuffisante pour une emprise au sol"
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]                       # fermer l'anneau
    lat0 = sum(p[0] for p in pts) / len(pts)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    xy = [(lon * m_per_deg_lon, lat * m_per_deg_lat) for (lat, lon) in pts]
    cross2 = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        cross2 += x1 * y2 - x2 * y1
    area = abs(cross2) / 2.0
    if area <= 0:
        return "indisponible : emprise au sol nulle (tracé dégénéré)"
    return round(area, 1)


def _point(el):
    """Point représentatif (lat, lon) d'un élément, ou (None, None) si introuvable.

    Node -> lat/lon directs. Way/relation avec `out center` -> el['center']. Relation avec
    `out geom` -> centre de la bbox `bounds`. Way avec `out geom` -> centroïde de la géométrie.
    """
    if el.get("type") == "node" and "lat" in el and "lon" in el:
        return el.get("lat"), el.get("lon")
    center = el.get("center")
    if isinstance(center, dict):
        return center.get("lat"), center.get("lon")
    bounds = el.get("bounds")
    if isinstance(bounds, dict) and all(k in bounds for k in ("minlat", "maxlat", "minlon", "maxlon")):
        return (bounds["minlat"] + bounds["maxlat"]) / 2.0, (bounds["minlon"] + bounds["maxlon"]) / 2.0
    geom = el.get("geometry")
    if geom:
        lats = [p["lat"] for p in geom if isinstance(p, dict) and "lat" in p]
        lons = [p["lon"] for p in geom if isinstance(p, dict) and "lon" in p]
        if lats and lons:
            return sum(lats) / len(lats), sum(lons) / len(lons)
    return None, None


# --- Construction d'un Site ---------------------------------------------------
def build_site(loc, el, type_):
    tags = el.get("tags", {}) or {}
    osm_id = "%s/%s" % (el.get("type"), el.get("id"))
    lat, lon = _point(el)
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        lat_out, lon_out = float(lat), float(lon)
        distance = round(haversine_km(loc.lat, loc.lon, lat_out, lon_out), 3)
    else:
        lat_out = lon_out = "indisponible : position absente de la réponse Overpass"
        distance = "indisponible : position absente, distance non calculable"
    surface = footprint_m2(el.get("geometry"))
    capacite, source, methode = estimate_capacity(type_, tags, surface)
    return C.Site(
        osm_id=osm_id,
        type=type_,
        nom=(tags.get("name") or None),
        lat=lat_out,
        lon=lon_out,
        distance_km=distance,
        capacite=capacite,
        capacite_source=source,
        capacite_methode=methode,
        surface_m2=surface,
        tags={k: tags[k] for k in RELEVANT_TAGS if k in tags},
    )


# --- Adaptateur : hébergement via Overpass ------------------------------------
def collect_hebergement(loc, args):
    ql = build_query(loc.lat, loc.lon, args.radius_m, args.timeout)
    data = overpass_query(ql, args.timeout)

    counts = {"hôtel": 0, "gymnase": 0, "école": 0, "salle_communale": 0}
    retenus, seen = [], set()
    for el in data.get("elements", []):
        osm_id = "%s/%s" % (el.get("type"), el.get("id"))
        if osm_id in seen:
            continue
        type_ = classify(el.get("tags", {}) or {})
        if type_ is None:
            continue
        seen.add(osm_id)
        counts[type_] += 1
        retenus.append((build_site(loc, el, type_), el))

    # Tri par capacité DÉCROISSANTE (plus grands abris d'abord) ; capacité non numérique
    # (indisponible) rejetée en fin via -inf + reverse.
    retenus.sort(key=lambda pair: (pair[0].capacite
                                   if isinstance(pair[0].capacite, (int, float))
                                   else float("-inf")),
                 reverse=True)

    # Agrégats sur TOUS les sites trouvés (le résumé ne dépend pas de --limit).
    capacite_totale = sum(int(s.capacite) for s, _ in retenus
                          if isinstance(s.capacite, (int, float)))
    sans_capacite = sum(1 for s, _ in retenus if not isinstance(s.capacite, (int, float)))

    # --limit borne la LISTE ; limit >= 0 garanti par run() ; limit == 0 -> liste vide.
    sites_out = []
    for site, el in retenus[:args.limit]:
        d = jsonable(site)
        if args.geometry:                       # tracé complet hors-contrat (cf. accessibilite-routes)
            d["geometry"] = el.get("geometry") or None
        sites_out.append(d)

    resume = C.Resume(
        sites_total=sum(counts.values()),
        hotels=counts["hôtel"], gymnases=counts["gymnase"],
        ecoles=counts["école"], salles_communales=counts["salle_communale"],
        capacite_estimee_totale=capacite_totale,
        sites_sans_capacite=sans_capacite,
    )
    out = jsonable(C.Hebergement(rayon_m=int(args.radius_m), resume=resume, sites=[], note=NOTE))
    out["sites"] = sites_out
    return out


# --- Orchestration ------------------------------------------------------------
def run(args):
    if args.radius_m <= 0 or args.radius_m > MAX_RADIUS_M:
        fail("rayon hors bornes : %s m (attendu 1..%d)" % (args.radius_m, MAX_RADIUS_M),
             detail="Overpass doit rester scopé (fair-use). Réduire --radius-m, ou lancer "
                    "plusieurs requêtes ciblées.")
    if args.limit < 0:
        fail("--limit négatif (%d) invalide : attendre un entier >= 0 "
             "(0 = résumé seul, sans liste détaillée)" % args.limit)
    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    out = {"lieu": jsonable(loc)}
    erreurs = 0
    try:
        out["hebergement"] = collect_hebergement(loc, args)
    except SkillError as exc:
        out["hebergement"] = {"error": exc.message, "detail": exc.detail}
        erreurs += 1
    except Exception as exc:  # robustesse : une exception inattendue ne doit pas crasher le skill
        sys.stderr.write("logistique-hebergement: exception inattendue (%s) : %s\n"
                         % (type(exc).__name__, exc))
        out["hebergement"] = {"error": "erreur inattendue (%s) : %s" % (type(exc).__name__, exc)}
        erreurs += 1
    return out, (1 if erreurs else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Lieux d'hébergement des sinistrés (hôtels, gymnases, écoles, salles "
                    "communales) avec capacité estimée, autour d'une commune ou d'un point, "
                    "via OSM/Overpass.")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--radius-m", dest="radius_m", type=int, default=DEFAULT_RADIUS_M,
                        help="Rayon de recherche en mètres (défaut %(default)s, max "
                             + str(MAX_RADIUS_M) + ").")
    parser.add_argument("--limit", type=int, default=100,
                        help="Nombre max de sites listés, triés par capacité décroissante (défaut "
                             "%(default)s). Le résumé compte tous les sites trouvés.")
    parser.add_argument("--geometry", action="store_true",
                        help="Ajouter le tracé complet (geometry) de chaque site en plus du point "
                             "représentatif (sortie plus volumineuse).")
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="Timeout Overpass en secondes. Défaut 25.")
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
