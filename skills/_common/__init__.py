# -*- coding: utf-8 -*-
"""Infra partagée entre les skills du plugin flood-response.

Import depuis un skill (le dossier parent `skills/` doit être sur sys.path) :

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _common import http_get_json, resolve_location, SkillError, Lieu, jsonable
"""

from . import dataset
from .contract import Lieu, jsonable, validate
from .dataset import SourceConfig, csv_encoding
from .errors import SkillError, emit_error, fail
from .geo import (
    FRANCE_BBOXES,
    FRANCE_TIMEZONES,
    GEO_API,
    haversine_km,
    in_france,
    local_timezone,
    normalize,
    resolve_commune,
    resolve_location,
    reverse_commune,
)
from .http import http_download, http_get_json

__all__ = [
    "dataset", "SourceConfig", "csv_encoding",
    "Lieu", "jsonable", "validate",
    "SkillError", "emit_error", "fail",
    "FRANCE_BBOXES", "FRANCE_TIMEZONES", "GEO_API", "haversine_km", "in_france",
    "local_timezone", "normalize", "resolve_commune", "resolve_location", "reverse_commune",
    "http_get_json", "http_download",
]
