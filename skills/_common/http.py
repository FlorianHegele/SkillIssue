# -*- coding: utf-8 -*-
"""Client HTTP robuste partagé (couche d'accès aux API externes).

Confine ici les pièges communs : timeouts, retry/backoff, et surtout la vérification
du Content-Type (Vigicrues/Hub'Eau peuvent renvoyer du HTML d'erreur sous un code 200).
"""

import time

import requests

from .errors import SkillError

# Hub'Eau renvoie 206 (Partial Content) en pagination : réponse JSON valide.
_OK_STATUS = (200, 206)
_USER_AGENT = "flood-response/0.1 (academic project)"


def http_get_json(url, params=None, timeout=20, retries=3):
    """GET JSON avec retry/backoff. Lève SkillError si tout échoue."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code not in _OK_STATUS:
                last_err = "HTTP %s" % resp.status_code
            elif "json" not in ctype.lower():
                last_err = "réponse non-JSON (Content-Type: %s)" % (ctype or "inconnu")
            else:
                return resp.json()
        except requests.RequestException as exc:
            last_err = str(exc)
        except ValueError as exc:
            last_err = "JSON invalide : %s" % exc
        if attempt < retries - 1:
            time.sleep(0.8 * (attempt + 1))  # backoff linéaire
    raise SkillError("échec de l'appel à %s" % url, detail=last_err)
