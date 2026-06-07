# -*- coding: utf-8 -*-
"""Contrat de sortie du skill alerte-crue (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Quelle est l'alerte officielle ici ? »        -> Vigilance.couleur
  - « Le cours d'eau, à quel niveau, maintenant ? »  -> StationHydro.hauteur_mm / debit_ls
  - « De la pluie arrive-t-elle, quand, quelle force ? » -> Pluie (cumul, pic, créneau)

Les adaptateurs (collect_* dans main.py) traduisent Vigicrues / Hub'Eau / OpenMeteo vers
ces structures. Le contrat exécutable (validation) est `contract.schema.json`.
"""

from dataclasses import dataclass, field
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
    # Horodatage PROPRE à chaque mesure : H et Q n'ont pas forcément le même pas ni le
    # même instant. On ne fusionne pas en une date unique (qui mentirait sur l'une des deux).
    date_hauteur: Optional[str] = None   # date de l'obs H (ISO 8601) ; None si pas de mesure H
    date_debit: Optional[str] = None     # date de l'obs Q (ISO 8601) ; None si pas de mesure Q


@dataclass
class BlocHydro:
    """Bloc hydrométrie : les stations retenues + le nombre total de stations trouvées dans
    le rayon. `stations_dans_rayon` > len(stations) signale un plafonnement (--max-stations)
    ou des stations écartées faute de mesure temps réel : on ne masque pas le tri silencieux."""
    stations: List[StationHydro]
    stations_dans_rayon: int


@dataclass
class HeurePluie:
    heure: str                       # ISO 8601 local
    precipitation_mm: float


@dataclass
class Pic:
    heure: str
    precipitation_mm: float


@dataclass
class Creneau:
    """Épisode pluvieux contigu (suite d'heures consécutives >= seuil). Une accalmie
    (heure sèche) sépare deux créneaux : on n'affiche donc PAS un unique début/fin qui
    engloberait les trous, mais chaque épisode réel."""
    debut: str
    fin: str
    cumul_mm: float


@dataclass
class Pluie:
    cumul_prochaines_24h_mm: Mesure   # nombre, ou chaîne si < 24 h de prévision dispo
    seuil_mm: float
    heures_pluvieuses: List[HeurePluie]
    modele: str = "meteofrance_arome_france_hd"
    unite: str = "mm"
    pic: Optional[Pic] = None
    creneaux: List[Creneau] = field(default_factory=list)  # épisodes pluvieux contigus
    # par_heure (série complète) n'est ajoutée qu'avec --detail, hors dataclass.
