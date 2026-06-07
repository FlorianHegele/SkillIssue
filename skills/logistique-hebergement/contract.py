# -*- coding: utf-8 -*-
"""Contrat de sortie du skill logistique-hebergement (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Où héberger les sinistrés à l'écart de la zone inondée ? »
        -> Site.type (hôtel / gymnase / école / salle_communale), nom, lat/lon, distance_km
  - « Combien de personnes chaque lieu peut-il accueillir ? »
        -> Site.capacite (couchages), capacite_source (osm / estimee / indisponible),
           capacite_methode (comment l'estimation a été obtenue), surface_m2
  - « Quelle capacité totale mobilisable, combien de lieux ? »
        -> Resume (compteurs par type + capacité agrégée)

Source = OpenStreetMap via Overpass. La complétude des tags de capacité est très mauvaise
(mesurée : `rooms` 7/40 hôtels, `capacity:rooms` ~72 fois dans le monde entier) → l'essentiel
des capacités est ESTIMÉ et étiqueté comme tel. L'adaptateur (collect_hebergement dans main.py)
traduit les `elements` Overpass vers ces structures. Le contrat exécutable est contract.schema.json.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Union

# Convention transverse : une mesure absente n'est PAS un null ambigu, mais une chaîne
# explicative. Donc une mesure = soit sa valeur numérique, soit un str disant pourquoi.
Mesure = Union[float, str]


@dataclass
class Site:
    osm_id: str                      # "way/123456" | "node/789" | "relation/42" (traçable sur OSM)
    type: str                        # hôtel | gymnase | école | salle_communale
    nom: Optional[str]               # name de l'élément, sinon None
    lat: Mesure                      # centre (way/relation) ou position (node), ou str si absent
    lon: Mesure
    distance_km: Mesure              # haversine depuis le lieu, ou str si coordonnée absente
    capacite: Mesure                 # nb de couchages (valeur OSM ou estimée), ou str si indispo
    capacite_source: str             # "osm" (tag explicite) | "estimee" (calculée) | "indisponible"
    capacite_methode: str            # transparence : "tag capacity", "rooms×2", "surface 1200 m² / 4 m²"…
    surface_m2: Mesure               # emprise au sol calculée (way fermé), ou str si non calculable
    tags: dict = field(default_factory=dict)   # sous-ensemble pertinent des tags OSM
    # La géométrie complète ([{lat,lon}…]) n'est PAS un champ du dataclass : elle sert au calcul
    # de surface puis est ajoutée au dict de sortie hors-contrat uniquement avec --geometry
    # (cf. main.collect_hebergement). Défaut = point représentatif seul.


@dataclass
class Resume:
    sites_total: int
    hotels: int
    gymnases: int
    ecoles: int
    salles_communales: int
    capacite_estimee_totale: int     # somme des capacités NUMÉRIQUES (couchages mobilisables)
    sites_sans_capacite: int         # nb de sites dont la capacité n'a pu être ni lue ni estimée


@dataclass
class Hebergement:
    rayon_m: int
    resume: Resume
    sites: List[Site]
    note: str                        # réserve de complétude : tags capacité rares, estimations grossières
