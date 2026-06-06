# -*- coding: utf-8 -*-
"""Résolution géographique partagée : commune (nom/INSEE) ou lat/lon -> Lieu.

Localisation OBLIGATOIRE, aucun repli par défaut (cf. CLAUDE.md). Toute entrée
manquante/introuvable lève une SkillError explicite, avec un detail exploitable
(suggestions, candidats homonymes) pour permettre une correction.
"""

import math
import unicodedata

from .contract import Lieu
from .errors import fail
from .http import http_get_json

GEO_API = "https://geo.api.gouv.fr/communes"

# Boîtes englobantes France (métropole + DOM) — rejette les coordonnées manifestement
# hors zone couverte. Approche volontairement grossière (les bbox chevauchent les
# voisins) : la résolution par commune, elle, est strictement française via geo.api.
FRANCE_BBOXES = [
    (41.0, 51.6, -5.5, 9.8),     # métropole + Corse
    (2.0, 6.0, -55.0, -51.0),    # Guyane
    (14.0, 16.6, -61.9, -60.7),  # Guadeloupe / Martinique
    (-21.5, -20.7, 55.1, 55.9),  # La Réunion
    (-13.1, -12.5, 44.9, 45.4),  # Mayotte
]


def normalize(text):
    """minuscule + sans accents + trim, pour comparer des noms de communes."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.strip().lower()


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance orthodromique en km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_france(lat, lon):
    return any(
        latmin <= lat <= latmax and lonmin <= lon <= lonmax
        for latmin, latmax, lonmin, lonmax in FRANCE_BBOXES
    )


def resolve_commune(value, timeout=20):
    """Résout un nom OU un code INSEE en `Lieu`. Lève SkillError si introuvable/ambigu."""
    raw = value.strip()
    is_code = raw.replace("2A", "").replace("2B", "").isdigit() and len(raw) == 5
    params = {"fields": "centre,code,nom,codeDepartement", "format": "json"}
    if is_code:
        params["code"] = raw
    else:
        params["nom"] = raw
        params["boost"] = "population"
        params["limit"] = 10

    data = http_get_json(GEO_API, params=params, timeout=timeout)
    if not isinstance(data, list) or not data:
        fail("commune introuvable : %r" % raw,
             detail="aucun résultat sur geo.api.gouv.fr")

    if is_code:
        c = data[0]
    else:
        exact = [c for c in data if normalize(c.get("nom")) == normalize(raw)]
        if len(exact) == 1:
            c = exact[0]
        elif len(exact) > 1:
            cands = [
                {"nom": c["nom"], "code_insee": c["code"],
                 "departement": c.get("codeDepartement")}
                for c in exact
            ]
            fail("plusieurs communes nommées %r : préciser le code INSEE" % raw,
                 detail={"candidats": cands})
        else:
            sugg = [
                {"nom": c["nom"], "code_insee": c["code"],
                 "departement": c.get("codeDepartement")}
                for c in data[:5]
            ]
            fail("commune introuvable : %r" % raw, detail={"suggestions": sugg})

    centre = (c.get("centre") or {}).get("coordinates")
    if not centre:
        fail("coordonnées indisponibles pour la commune %r" % c.get("nom"))
    return Lieu(commune=c.get("nom"), code_insee=c.get("code"),
                lat=centre[1], lon=centre[0])


def resolve_location(commune=None, lat=None, lon=None, timeout=20):
    """Point d'entrée : --commune OU --lat/--lon. Aucun repli. Lève SkillError sinon."""
    has_lat = lat is not None
    has_lon = lon is not None
    if has_lat ^ has_lon:
        fail("coordonnées incomplètes : --lat ET --lon sont requis ensemble")
    if has_lat and has_lon:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            fail("coordonnées hors bornes : lat=%s lon=%s" % (lat, lon))
        if not in_france(lat, lon):
            fail("coordonnées hors zone couverte (France métropole/DOM) : "
                 "lat=%s lon=%s" % (lat, lon))
        return Lieu(commune=None, code_insee=None, lat=lat, lon=lon)
    if commune:
        return resolve_commune(commune, timeout)
    fail("localisation requise : fournir --commune <nom|code INSEE> ou --lat <..> --lon <..>")
