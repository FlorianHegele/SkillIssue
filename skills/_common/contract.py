# -*- coding: utf-8 -*-
"""Briques de contrat partagées entre skills.

Le « contrat » d'un skill = la forme de sortie définie en amont à partir des besoins de
décision (dataclasses typées) + un JSON Schema validé sur fixtures hors-ligne. Les
adaptateurs (collecteurs) traduisent les réponses brutes des API vers ce contrat :
les bizarreries d'API restent confinées, la sortie reste stable.

Pivot inter-skills : `Lieu` (code commune INSEE + lat/lon).
"""

from dataclasses import asdict, dataclass, is_dataclass
from typing import Optional


@dataclass
class Lieu:
    """Localisation résolue, commune à tous les skills."""
    commune: Optional[str]
    code_insee: Optional[str]
    lat: float
    lon: float


def jsonable(obj):
    """Convertit dataclasses / listes en structures JSON-sérialisables."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list):
        return [jsonable(x) for x in obj]
    return obj


def validate(data, schema_path):
    """Valide `data` contre le JSON Schema `schema_path`.

    Lève jsonschema.ValidationError si non conforme. Importé paresseusement pour ne pas
    imposer jsonschema à l'exécution normale du skill (utile surtout en test).
    """
    import json
    import jsonschema

    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=data, schema=schema)
    return True
