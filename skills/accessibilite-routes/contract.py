# -*- coding: utf-8 -*-
"""Contrat de sortie du skill accessibilite-routes (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Quels franchissements risquent d'être coupés par l'eau autour d'ici ? »
        -> Ouvrage.kind (gué / pont / tunnel / passage_inférieur / zone_inondable)
  - « Où, à quelle distance, sur quelle voie ? »
        -> Ouvrage.lat/lon, distance_km, highway, nom
  - « Combien, de quels types ? »            -> Resume (compteurs)

Source = OpenStreetMap via Overpass (réseau + ouvrages, PAS l'aléa). L'adaptateur
(collect_accessibilite dans main.py) traduit les `elements` Overpass vers ces structures.
Le contrat exécutable (validation) est `contract.schema.json`.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Union

# Convention transverse : une mesure absente n'est PAS un null ambigu, mais une chaîne
# explicative. Donc une mesure = soit sa valeur numérique, soit un str disant pourquoi.
Mesure = Union[float, str]


@dataclass
class Ouvrage:
    osm_id: str                      # "way/123456" | "node/789" (traçable sur openstreetmap.org)
    kind: str                        # gué | pont | tunnel | passage_inférieur | zone_inondable
    nom: Optional[str]               # name, sinon ref de la voie (ex. "D981"), sinon None
    highway: Optional[str]           # classe de voie portée (residential, primary…) si dispo
    lat: Mesure                      # centre (way) ou position (node), ou str si absent
    lon: Mesure
    distance_km: Mesure              # haversine depuis le lieu, ou str si coordonnée absente
    tags: dict = field(default_factory=dict)   # sous-ensemble pertinent des tags OSM
    # Le tracé complet de l'ouvrage n'est PAS exposé : il n'apporte rien à une décision
    # reformulée en langage naturel et reste récupérable via osm_id (openstreetmap.org).


@dataclass
class Resume:
    ouvrages_total: int
    gues: int
    ponts: int
    tunnels: int
    passages_inferieurs: int
    zones_inondables: int


@dataclass
class Accessibilite:
    rayon_m: int
    resume: Resume
    ouvrages_a_risque: List[Ouvrage]
    note: str                        # réserve de complétude : OSM = réseau/ouvrages, pas l'aléa
