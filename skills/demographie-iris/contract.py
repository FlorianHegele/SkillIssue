# -*- coding: utf-8 -*-
"""Contrat de sortie du skill demographie-iris (défini en amont, à partir des besoins).

Question de décision en crue → champs nécessaires :
  - « Combien de personnes / ménages à évacuer ou héberger ici ? » -> CommuneSynthese.population,
        menages_total ; détail par quartier -> IrisItem.population / menages
  - « Quels quartiers concentrent des foyers vulnérables ? »        -> IrisItem.monoparentales,
        CommuneSynthese.part_monoparentales_pct (familles monoparentales = mobilité contrainte)

Granularité = IRIS (≈ quartiers de 2 000 hab. pour les communes découpées ; sinon la commune
entière forme un IRIS). Source = base INSEE « Couples - Familles - Ménages » par IRIS (CSV zippé).
Les adaptateurs (collect_* dans main.py) traduisent ce CSV vers ces structures ; le nom de commune
vient du pivot géo (geo.api), pas du CSV (les fichiers métropole/COM divergent sur ce point).
Le contrat exécutable (validation) est `contract.schema.json`.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Union

# Convention transverse : une mesure absente n'est PAS un null ambigu, mais une chaîne
# explicative (ex. secret statistique INSEE, colonne absente du millésime). Donc une mesure
# = soit sa valeur numérique, soit un str disant pourquoi.
Mesure = Union[float, str]


@dataclass
class CommuneSynthese:
    code: str                          # code commune INSEE (pivot)
    nom: Optional[str]                 # libellé commune (via geo.api, pas le CSV)
    population: Mesure                 # population de la commune (geo.api) OU chaîne explicative
    iris_count: int                    # nombre d'IRIS trouvés pour la commune dans le CSV
    menages_total: Mesure              # somme des ménages sur les IRIS (unités à héberger)
    familles_total: Mesure             # somme des familles
    monoparentales_total: Mesure       # somme des familles monoparentales (TOUS les IRIS chiffrés)
    part_monoparentales_pct: Mesure    # % monoparentales / familles (indicateur de vulnérabilité)
    # Base RÉELLEMENT utilisée pour le pourcentage : uniquement les IRIS où familles ET
    # monoparentales sont chiffrées (sinon on mélangerait des périmètres). Exposée pour lever
    # l'ambiguïté avec monoparentales_total (somme complète) : ici part_monoparentales_pct =
    # base.monoparentales / base.familles. Dict {monoparentales, familles} OU chaîne explicative.
    part_monoparentales_base: Union[dict, str] = "indisponible : base non calculée"


@dataclass
class IrisItem:
    code: str                          # code IRIS
    libelle: Mesure                    # libellé IRIS (LIBIRIS/LIB_IRIS) OU chaîne si absent
    type_iris: Optional[str] = None    # TYP_IRIS : H/A/D/Z (seulement avec --detail)
    population: Mesure = None           # population des ménages de l'IRIS (C__PMEN)
    menages: Mesure = None              # C__MEN
    familles: Mesure = None             # C__FAM
    monoparentales: Mesure = None       # C__MENFAMMONO
    couples_avec_enfants: Mesure = None  # C__MENCOUPAENF (seulement avec --detail)
    couples_sans_enfants: Mesure = None  # C__MENCOUPSENF (seulement avec --detail)


@dataclass
class Demographie:
    commune: CommuneSynthese
    iris: List[IrisItem] = field(default_factory=list)  # trié par population décroissante
    # Liste IRIS limitée au top-N (par population) pour économiser le contexte : si True, des IRIS
    # ont été omis (commune.iris_count = total trouvé > len(iris)). Les totaux commune restent
    # calculés sur TOUS les IRIS. --top 0 renvoie la liste complète. Jamais de troncature silencieuse.
    iris_tronque: bool = False
