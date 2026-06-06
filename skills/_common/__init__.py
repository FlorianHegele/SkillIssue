# -*- coding: utf-8 -*-
"""Infra partagée entre les skills du plugin flood-response.

Import depuis un skill (le dossier parent `skills/` doit être sur sys.path) :

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _common import http_get_json, resolve_location, SkillError, Lieu, jsonable
"""

from .contract import Lieu, jsonable, validate
from .errors import SkillError, emit_error, fail
from .geo import (
    FRANCE_BBOXES,
    GEO_API,
    haversine_km,
    in_france,
    normalize,
    resolve_commune,
    resolve_location,
)
from .http import http_get_json

__all__ = [
    "Lieu", "jsonable", "validate",
    "SkillError", "emit_error", "fail",
    "FRANCE_BBOXES", "GEO_API", "haversine_km", "in_france", "normalize",
    "resolve_commune", "resolve_location",
    "http_get_json",
]
