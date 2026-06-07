# -*- coding: utf-8 -*-
"""Contrat de sortie du skill vulnerabilite-bpe (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Où sont les écoles de cette commune ? »            -> Vulnerabilite.ecoles (souvent
        réquisitionnées comme centres d'hébergement, et publics fragiles à évacuer)
  - « Où sont les établissements de santé à protéger ? » -> Vulnerabilite.sante (urgences,
        maternité, dialyse… : continuité de soins vitale, évacuation prioritaire)
  - « Lesquels sont les plus proches du point inondé ? » -> Equipement.distance_km (tri),
        option --radius pour restreindre au secteur menacé

Source = fichier-détail BPE de l'INSEE (CSV national), qui porte déjà `LATITUDE`/`LONGITUDE`
WGS84. Les adaptateurs (load_equipements / build_vulnerabilite dans main.py) traduisent ce CSV
vers ces structures ; le nom de la commune vient du pivot géo (geo.api), le nom de l'établissement
de la colonne NOMRS quand elle est renseignée. Le contrat exécutable est `contract.schema.json`.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Union

# Convention transverse : une mesure absente n'est PAS un null ambigu, mais une chaîne
# explicative (ex. coordonnées de l'équipement absentes du fichier). Donc une mesure
# = soit sa valeur numérique, soit un str disant pourquoi.
Mesure = Union[float, str]


@dataclass
class Equipement:
    type_code: str                    # TYPEQU (ex. C107)
    type_libelle: str                 # libellé lisible (ex. « École maternelle ») ; repli = code
    nom: Optional[str]                # NOMRS si renseigné (ex. « ÉCOLE MATERNELLE PRÉS ST JEAN »)
    lat: Mesure                       # latitude WGS84 OU chaîne explicative
    lon: Mesure                       # longitude WGS84 OU chaîne explicative
    qualite_geoloc: Optional[str]     # qualité de géolocalisation lisible (ou None si inconnue)
    distance_km: Mesure               # distance au point résolu OU chaîne explicative


@dataclass
class CommuneEquipements:
    code: str                         # code commune INSEE (pivot)
    nom: Optional[str]                # libellé commune (via geo.api)
    ecoles_count: int                 # nombre TOTAL d'écoles trouvées (avant limite --top)
    sante_count: int                  # nombre TOTAL d'établissements de santé (avant limite --top)


@dataclass
class Vulnerabilite:
    commune: CommuneEquipements
    ecoles: List[Equipement] = field(default_factory=list)  # plus proches d'abord ; ≤ --top
    sante: List[Equipement] = field(default_factory=list)   # plus proches d'abord ; ≤ --top
    # Renseigné UNIQUEMENT si une liste a été tronquée par --top : dit explicitement combien
    # d'équipements existent vs combien sont affichés (jamais de troncature silencieuse). Les
    # compteurs ci-dessus restent les totaux, donc count > len(liste) signale aussi la coupe.
    note: Optional[str] = None
