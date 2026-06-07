#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alerte-crue — synthèse du risque de crue pour une commune ou un point en France.

Agrège trois sources publiques sans clé (voir references/api.md) vers le contrat défini
dans contract.py / contract.schema.json :
  - Vigicrues   : couleur de vigilance du tronçon de cours d'eau le plus proche
  - Hub'Eau     : hydrométrie temps réel (hauteur d'eau, débit) des stations proches
  - OpenMeteo   : prévision de précipitations (modèle Météo-France AROME HD)

Localisation OBLIGATOIRE (--commune ou --lat/--lon). Aucun repli par défaut.
Sortie : JSON sur stdout (ensure_ascii=False). Erreurs : JSON sur stderr + code != 0.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Le dossier parent `skills/` doit être sur sys.path pour importer le paquet _common.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import (  # noqa: E402
    SkillError, emit_error, haversine_km, http_get_json, jsonable, resolve_location,
)

import contract as C  # noqa: E402  (module local du skill)

try:
    from shapely.geometry import Point, shape
    from shapely.ops import nearest_points
except ImportError:  # pragma: no cover - dépendance déclarée dans requirements.txt
    Point = shape = nearest_points = None

# --- Endpoints (vérifiés live le 06/06/2026) ---------------------------------
VIGICRUES_GEOJSON = "https://www.vigicrues.gouv.fr/services/InfoVigiCru.geojson"
HUBEAU_STATIONS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/referentiel/stations"
HUBEAU_OBS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr"
OPENMETEO = "https://api.open-meteo.com/v1/forecast"

VIGILANCE_COULEURS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}
SEUIL_PLUIE_MM = 0.5  # mm/h en deçà desquels une heure est "sèche" (bruit, écartée)
# OpenMeteo est interrogé avec timezone=Europe/Paris : ses horodatages horaires sont donc
# en heure locale de Paris (naïfs). On ancre « maintenant » sur CETTE zone, sans dépendre du
# fuseau de la machine (un serveur en UTC décalerait sinon la fenêtre des 24 h).
PARIS_TZ = ZoneInfo("Europe/Paris")


# --- Adaptateur : vigilance Vigicrues ----------------------------------------
def collect_vigilance(loc, radius_km, timeout):
    if shape is None:
        return {"error": "dépendance 'shapely' manquante (pip install -r requirements.txt)"}
    geo = http_get_json(VIGICRUES_GEOJSON, timeout=timeout)
    point = Point(loc.lon, loc.lat)
    best = None
    for feat in geo.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
            # nearest_points travaille en degrés (lon/lat plats) : le « plus proche »
            # est une approximation (le degré de longitude est ~0,72× le degré de latitude
            # à 44°). Négligeable à courte distance ; la distance affichée, elle, est
            # recalculée en km par haversine sur le point le plus proche trouvé.
            p_geom, _ = nearest_points(g, point)
        except Exception:
            continue
        dist_km = haversine_km(loc.lat, loc.lon, p_geom.y, p_geom.x)
        if best is None or dist_km < best.distance_km:
            props = feat.get("properties", {})
            # NivInfViCr peut arriver en string selon les versions : on coerce en int
            # (sinon le champ violerait le contrat ["integer","null"]).
            try:
                niveau = int(props.get("NivInfViCr"))
            except (TypeError, ValueError):
                niveau = None
            best = C.Vigilance(
                couleur=VIGILANCE_COULEURS.get(niveau, "inconnu"),
                distance_km=round(dist_km, 2),
                niveau=niveau,
                troncon=props.get("lbentcru"),
            )
    if best is None:
        return {"error": "aucun tronçon Vigicrues exploitable"}
    if best.distance_km > radius_km:
        return {"error": "aucun tronçon Vigicrues dans un rayon de %s km" % radius_km,
                "troncon_le_plus_proche": jsonable(best)}
    return best


# --- Adaptateur : hydrométrie Hub'Eau ----------------------------------------
def collect_hydro(loc, radius_km, timeout, max_stations=4):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(loc.lat)), 0.1))
    bbox = "%s,%s,%s,%s" % (
        round(loc.lon - dlon, 5), round(loc.lat - dlat, 5),
        round(loc.lon + dlon, 5), round(loc.lat + dlat, 5),
    )
    data = http_get_json(
        HUBEAU_STATIONS,
        params={"bbox": bbox, "en_service": "true", "format": "json", "size": 50,
                "fields": "code_station,libelle_station,latitude_station,longitude_station"},
        timeout=timeout,
    )
    stations = []
    for s in data.get("data", []):
        slat, slon = s.get("latitude_station"), s.get("longitude_station")
        if slat is None or slon is None:
            continue
        d = haversine_km(loc.lat, loc.lon, slat, slon)
        if d <= radius_km:
            stations.append((d, s))
    stations.sort(key=lambda x: x[0])

    results = []
    for dist, s in stations[:max_stations]:
        code = s["code_station"]
        mesures = {}
        dates = {}  # horodatage propre à chaque grandeur (H et Q peuvent différer)
        for grandeur, key in (("H", "hauteur_mm"), ("Q", "debit_ls")):
            try:
                obs = http_get_json(
                    HUBEAU_OBS,
                    params={"code_entite": code, "grandeur_hydro": grandeur,
                            "size": 1, "sort": "desc", "format": "json",
                            "fields": "date_obs,resultat_obs"},
                    timeout=timeout,
                )
                rows = obs.get("data", [])
                if rows:
                    mesures[key] = rows[0].get("resultat_obs")
                    dates[key] = rows[0].get("date_obs")
                else:  # appel OK mais aucune donnée temps réel pour cette grandeur
                    mesures[key] = "indisponible : pas de mesure temps réel récente"
            except SkillError as exc:  # l'appel a échoué : on le dit, sans masquer
                mesures[key] = "erreur : %s" % exc.message
        # On n'inclut la station que si elle porte au moins une vraie mesure numérique.
        if any(isinstance(v, (int, float)) for v in mesures.values()):
            results.append(C.StationHydro(
                station=code,
                distance_km=round(dist, 2),
                nom=s.get("libelle_station"),
                hauteur_mm=mesures.get("hauteur_mm"),
                debit_ls=mesures.get("debit_ls"),
                date_hauteur=dates.get("hauteur_mm"),
                date_debit=dates.get("debit_ls"),
            ))
    if not results:
        return {"error": "aucune station Hub'Eau avec donnée temps réel "
                         "dans un rayon de %s km" % radius_km}
    return results


# --- Adaptateur : prévision pluie OpenMeteo ----------------------------------
def collect_pluie(loc, timeout, detail=False, seuil=SEUIL_PLUIE_MM):
    """Pluie 24 h optimisée pour la décision : on ne garde que les heures pluvieuses
    (>= seuil) + un résumé (cumul, pic, créneau). --detail réexpose la série complète."""
    data = http_get_json(
        OPENMETEO,
        params={"latitude": loc.lat, "longitude": loc.lon,
                "hourly": "precipitation",
                "models": "meteofrance_arome_france_hd",
                "timezone": "Europe/Paris", "forecast_days": 2},
        timeout=timeout,
    )
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    now = datetime.now(PARIS_TZ)
    cur_hour = now.replace(minute=0, second=0, microsecond=0)

    fenetre = []  # (heure_iso, mm) des 24 prochaines heures
    for t, p in zip(times, precip):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt.tzinfo is None:  # OpenMeteo renvoie l'heure locale Paris en naïf -> on l'ancre
            dt = dt.replace(tzinfo=PARIS_TZ)
        if dt < cur_hour:
            continue
        if len(fenetre) >= 24:
            break
        fenetre.append((t, round(p or 0.0, 1)))

    # forecast_days=2 garantit normalement >= 24 h futures ; si l'API en renvoie moins,
    # on ne ment pas sur le nom du champ : cumul devient une chaîne explicative.
    if len(fenetre) >= 24:
        cumul = round(sum(v for _, v in fenetre), 1)
    else:
        cumul = ("indisponible : prévision limitée à %d h (< 24) renvoyée par l'API"
                 % len(fenetre))
    pluvieuses = [C.HeurePluie(heure=t, precipitation_mm=v) for t, v in fenetre if v >= seuil]
    pic_raw = max(fenetre, key=lambda x: x[1], default=None)
    pic = (C.Pic(heure=pic_raw[0], precipitation_mm=pic_raw[1])
           if pic_raw and pic_raw[1] >= seuil else None)

    # Découpe la fenêtre en épisodes pluvieux CONTIGUS : une heure sèche (< seuil) clôt le
    # créneau courant. On expose ainsi chaque épisode réel plutôt qu'un début/fin global qui
    # masquerait les accalmies.
    creneaux, seg = [], None
    for t, v in fenetre:
        if v >= seuil:
            if seg is None:
                seg = {"debut": t, "fin": t, "cumul": v}
            else:
                seg["fin"], seg["cumul"] = t, seg["cumul"] + v
        elif seg is not None:
            creneaux.append(seg)
            seg = None
    if seg is not None:
        creneaux.append(seg)
    creneaux = [C.Creneau(debut=s["debut"], fin=s["fin"], cumul_mm=round(s["cumul"], 1))
                for s in creneaux]

    pluie = C.Pluie(
        cumul_prochaines_24h_mm=cumul,
        seuil_mm=seuil,
        heures_pluvieuses=pluvieuses,
        unite=data.get("hourly_units", {}).get("precipitation", "mm"),
        pic=pic,
        creneaux=creneaux,
    )
    if detail:
        out = jsonable(pluie)
        out["par_heure"] = [{"heure": t, "precipitation_mm": v} for t, v in fenetre]
        return out
    return pluie


# --- Orchestration ------------------------------------------------------------
COLLECTEURS = {
    "vigilance": lambda loc, a: collect_vigilance(loc, a.radius, a.timeout),
    "hydro": lambda loc, a: collect_hydro(loc, a.radius, a.timeout),
    "pluie": lambda loc, a: collect_pluie(loc, a.timeout,
                                          detail=a.detail, seuil=a.seuil_pluie),
}


def run(args):
    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    sources = args.only or list(COLLECTEURS.keys())
    out = {"lieu": jsonable(loc)}
    erreurs = 0
    for name in sources:
        try:
            result = COLLECTEURS[name](loc, args)
            out[name] = jsonable(result)
            if isinstance(out[name], dict) and "error" in out[name]:
                erreurs += 1
        except SkillError as exc:
            out[name] = {"error": exc.message, "detail": exc.detail}
            erreurs += 1
        except Exception as exc:  # robustesse : une source ne doit pas tout casser
            out[name] = {"error": "erreur inattendue : %s" % exc}
            erreurs += 1
    # Code retour != 0 seulement si TOUTES les sources demandées ont échoué.
    return out, (1 if erreurs == len(sources) else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Synthèse du risque de crue (vigilance, hydrométrie, pluie) "
                    "pour une commune ou un point en France.")
    parser.add_argument("--commune", help="Nom ou code INSEE (ex. \"Alès\" ou 30007)")
    parser.add_argument("--lat", type=float, help="Latitude décimale")
    parser.add_argument("--lon", type=float, help="Longitude décimale")
    parser.add_argument("--only", action="extend", nargs="+", default=None,
                        choices=list(COLLECTEURS.keys()),
                        help="Limiter aux sources indiquées (ex. --only vigilance hydro, "
                             "ou répété). Défaut : toutes.")
    parser.add_argument("--radius", type=float, default=15.0,
                        help="Rayon de recherche en km (Vigicrues/Hub'Eau). Défaut 15.")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Timeout HTTP en secondes. Défaut 20.")
    parser.add_argument("--detail", action="store_true",
                        help="Pluie : réexposer la série horaire complète (24 h, heures sèches "
                             "incluses) en plus du résumé.")
    parser.add_argument("--seuil-pluie", dest="seuil_pluie", type=float,
                        default=SEUIL_PLUIE_MM,
                        help="Pluie : seuil mm/h en dessous duquel une heure est ignorée "
                             "(défaut %(default)s).")
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
