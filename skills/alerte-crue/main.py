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
    SkillError, emit_error, haversine_km, http_get_json, jsonable, local_timezone,
    resolve_location,
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
# Modèle OpenMeteo par défaut. AROME France HD (~1,5 km) ne couvre QUE la métropole :
# hors emprise, OpenMeteo répond HTTP 400 {"reason": "No data is available for this location"}
# (vérifié live le 07/06/2026). Pour les DOM, passer --modele meteofrance_seamless (modèle
# global, couvre la Réunion/Antilles/etc.). On reflète toujours le modèle DEMANDÉ dans la
# sortie : OpenMeteo n'écho­te pas le modèle réellement servi pour une requête mono-modèle.
AROME_HD = "meteofrance_arome_france_hd"
# TOUS les horodatages de la sortie sont en heure LOCALE du point (champ racine `fuseau`),
# jamais un mélange Paris/UTC. Le fuseau est déduit de la position (local_timezone) : Europe/Paris
# en métropole (DST géré), Indian/Reunion à La Réunion, etc. On le passe à OpenMeteo (timezone=…)
# pour que ses heures soient déjà locales, et on s'en sert pour ancrer « maintenant » sans dépendre
# du fuseau de la machine, ET pour convertir les dates Hub'Eau (rendues en UTC `Z`) vers ce fuseau.


def to_local_iso(iso_utc, tz):
    """Convertit un horodatage Hub'Eau (UTC, suffixe `Z`) en heure locale `tz` (ISO naïf, comme
    les heures de pluie : la sortie déclare le fuseau une seule fois au niveau racine). Best
    effort : renvoie la valeur d'origine si elle n'est pas parsable (on ne casse pas la sortie)."""
    if not isinstance(iso_utc, str):
        return iso_utc
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return iso_utc
    return dt.astimezone(tz).replace(tzinfo=None).isoformat()


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
def collect_hydro(loc, radius_km, timeout, tz, max_stations=4):
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
                val = rows[0].get("resultat_obs") if rows else None
                if isinstance(val, (int, float)):
                    mesures[key] = val
                    # Hub'Eau date en UTC (`Z`) -> heure locale du point, pour ne pas mélanger
                    # les fuseaux dans la sortie (pluie aussi est en local).
                    dates[key] = to_local_iso(rows[0].get("date_obs"), tz)
                else:  # appel OK mais aucune mesure exploitable (aucune ligne, ou resultat_obs
                       # null : capteur muet alors que la ligne existe) -> jamais un null ambigu.
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
    # On expose le nombre total de stations DANS LE RAYON (pas seulement les `max_stations`
    # retournées) : un écart révèle le plafonnement ou les stations écartées faute de mesure,
    # plutôt qu'un tri silencieux. Augmenter --max-stations / --radius pour en voir plus.
    return C.BlocHydro(stations=results, stations_dans_rayon=len(stations))


# --- Adaptateur : prévision pluie OpenMeteo ----------------------------------
def collect_pluie(loc, timeout, tz, fuseau, detail=False, seuil=SEUIL_PLUIE_MM, modele=AROME_HD):
    """Pluie 24 h optimisée pour la décision : on ne garde que les heures pluvieuses
    (>= seuil) + un résumé (cumul, pic, créneau). --detail réexpose la série complète.
    Horodatages en heure locale du point (fuseau `fuseau`, déclaré au niveau racine)."""
    try:
        data = http_get_json(
            OPENMETEO,
            params={"latitude": loc.lat, "longitude": loc.lon,
                    "hourly": "precipitation",
                    "models": modele,
                    # On impose le fuseau LOCAL du point : OpenMeteo renvoie alors ses heures
                    # déjà dans ce fuseau (cohérent avec les dates hydro, converties pareil).
                    "timezone": fuseau, "forecast_days": 2},
            timeout=timeout,
        )
    except SkillError as exc:
        # Hors emprise du modèle, OpenMeteo répond 400 + {"reason": "No data ..."} : on relaie
        # la cause réelle (portée par exc.detail) + une piste d'action, plutôt qu'un "HTTP 400"
        # opaque ou — pire — de faux 0 mm. AROME France HD ne couvre que la métropole.
        return {"error": "prévision pluie indisponible (modèle %s) : %s"
                         % (modele, exc.detail or exc.message),
                "indice": "hors métropole, AROME France HD ne couvre pas le point ; "
                          "réessayer avec --modele meteofrance_seamless"}
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    # Heures en local du point : on ancre « maintenant » sur ce fuseau, sans dépendre de celui
    # de la machine (un serveur en UTC décalerait sinon la fenêtre des 24 h).
    now = datetime.now(tz)
    cur_hour = now.replace(minute=0, second=0, microsecond=0)

    fenetre = []  # (heure_iso, mm | None) ; None = valeur non fournie (trou de couverture)
    for t, p in zip(times, precip):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt.tzinfo is None:  # heure locale en naïf -> on l'ancre sur le fuseau servi
            dt = dt.replace(tzinfo=tz)
        if dt < cur_hour:
            continue
        if len(fenetre) >= 24:
            break
        # Une valeur null (modèle muet sur cette heure) reste None : on ne la maquille PAS en 0.0.
        fenetre.append((t, round(p, 1) if isinstance(p, (int, float)) else None))

    dispo = [(t, v) for t, v in fenetre if v is not None]
    # Réponse 200 mais aucune valeur exploitable -> on le dit (jamais un faux total à 0).
    if fenetre and not dispo:
        return {"error": "prévision pluie indisponible : le modèle %s n'a renvoyé aucune "
                         "valeur exploitable pour ce point" % modele}

    # forecast_days=2 garantit normalement >= 24 h futures ; si l'API en renvoie moins (ou des
    # trous), on ne ment pas sur le nom du champ : cumul devient une chaîne explicative.
    if len(dispo) >= 24:
        cumul = round(sum(v for _, v in dispo), 1)
    else:
        cumul = ("indisponible : prévision exploitable limitée à %d h (< 24) — fenêtre "
                 "tronquée ou trous du modèle %s" % (len(dispo), modele))
    pluvieuses = [C.HeurePluie(heure=t, precipitation_mm=v) for t, v in fenetre
                  if v is not None and v >= seuil]
    pic_raw = max(dispo, key=lambda x: x[1], default=None)
    pic = (C.Pic(heure=pic_raw[0], precipitation_mm=pic_raw[1])
           if pic_raw and pic_raw[1] >= seuil else None)

    # Découpe la fenêtre en épisodes pluvieux CONTIGUS : une heure sèche (< seuil) OU un trou
    # (None) clôt le créneau courant. On expose ainsi chaque épisode réel plutôt qu'un
    # début/fin global qui masquerait les accalmies.
    creneaux, seg = [], None
    for t, v in fenetre:
        if v is not None and v >= seuil:
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
        modele=modele,
        unite=data.get("hourly_units", {}).get("precipitation", "mm"),
        pic=pic,
        creneaux=creneaux,
    )
    if detail:
        out = jsonable(pluie)
        out["par_heure"] = [{"heure": t, "precipitation_mm": v} for t, v in fenetre
                            if v is not None]
        return out
    return pluie


# --- Orchestration ------------------------------------------------------------
COLLECTEURS = {
    "vigilance": lambda loc, a, tz, fus: collect_vigilance(loc, a.radius, a.timeout),
    "hydro": lambda loc, a, tz, fus: collect_hydro(loc, a.radius, a.timeout, tz,
                                                   max_stations=a.max_stations),
    "pluie": lambda loc, a, tz, fus: collect_pluie(loc, a.timeout, tz, fus, detail=a.detail,
                                                   seuil=a.seuil_pluie, modele=a.modele),
}


def run(args):
    loc = resolve_location(args.commune, args.lat, args.lon, args.timeout)
    # Fuseau LOCAL du point : référentiel unique de TOUS les horodatages de la sortie (pluie,
    # dates hydro). Déclaré une seule fois au niveau racine pour lever toute ambiguïté.
    fuseau = local_timezone(loc.lat, loc.lon)
    tz = ZoneInfo(fuseau)
    # Dédup en gardant l'ordre : --only répété ne doit pas relancer une source ni fausser
    # le compte d'erreurs (qui pilote le code retour).
    sources = list(dict.fromkeys(args.only or list(COLLECTEURS.keys())))
    out = {"lieu": jsonable(loc), "fuseau": fuseau}
    erreurs = 0
    for name in sources:
        try:
            result = COLLECTEURS[name](loc, args, tz, fuseau)
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
    parser.add_argument("--max-stations", dest="max_stations", type=int, default=4,
                        help="Hydro : nombre max de stations retournées (les plus proches). "
                             "Défaut 4 ; stations_dans_rayon indique le total trouvé.")
    parser.add_argument("--modele", default=AROME_HD,
                        help="Pluie : modèle OpenMeteo. Défaut %(default)s (métropole "
                             "uniquement) ; hors métropole utiliser meteofrance_seamless.")
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
