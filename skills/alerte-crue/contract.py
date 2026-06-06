# -*- coding: utf-8 -*-
"""Contrat de sortie du skill alerte-crue (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Quelle est l'alerte officielle ici ? »        -> Vigilance.couleur
  - « Le cours d'eau, à quel niveau, maintenant ? »  -> StationHydro.hauteur_mm / debit_ls
  - « De la pluie arrive-t-elle, quand, quelle force ? » -> Pluie (cumul, pic, créneau)

Les adaptateurs (collect_* dans main.py) traduisent Vigicrues / Hub'Eau / OpenMeteo vers
ces structures. Le contrat exécutable (validation) est `contract.schema.json`.
"""

from dataclasses import dataclass
from typing import List, Optional, Union

# Convention transverse : une mesure absente n'est PAS un null ambigu, mais une chaîne
# explicative. Donc une mesure = soit sa valeur numérique, soit un str disant pourquoi.
Mesure = Union[float, str]


@dataclass
class Vigilance:
    couleur: str                     # vert | jaune | orange | rouge | inconnu
    distance_km: float               # distance au tronçon Vigicrues retenu
    niveau: Optional[int] = None     # NivInfViCr (1..4)
    troncon: Optional[str] = None    # libellé du tronçon


@dataclass
class StationHydro:
    station: str                     # code station Hub'Eau
    distance_km: float
    nom: Optional[str] = None
    hauteur_mm: Mesure = None        # H en mm (réf. locale, peut être négatif) OU str d'erreur
    debit_ls: Mesure = None          # Q en l/s OU str d'erreur
    date: Optional[str] = None       # date de l'observation utile (ISO 8601)


@dataclass
class HeurePluie:
    heure: str                       # ISO 8601 local
    precipitation_mm: float


@dataclass
class Pic:
    heure: str
    precipitation_mm: float


@dataclass
class Pluie:
    cumul_prochaines_24h_mm: Mesure   # nombre, ou chaîne si < 24 h de prévision dispo
    seuil_mm: float
    heures_pluvieuses: List[HeurePluie]
    modele: str = "meteofrance_arome_france_hd"
    unite: str = "mm"
    pic: Optional[Pic] = None
    debut_pluie: Optional[str] = None
    fin_pluie: Optional[str] = None
    # par_heure (série complète) n'est ajoutée qu'avec --detail, hors dataclass.
