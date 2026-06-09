# -*- coding: utf-8 -*-
"""Client HTTP robuste partagé (couche d'accès aux API externes).

Confine ici les pièges communs : timeouts, retry/backoff, et surtout la vérification
du Content-Type (Vigicrues/Hub'Eau peuvent renvoyer du HTML d'erreur sous un code 200).
"""

import os
import time

import requests

from .errors import SkillError

# Hub'Eau renvoie 206 (Partial Content) en pagination : réponse JSON valide.
_OK_STATUS = (200, 206)
_USER_AGENT = "flood-response/0.1 (academic project)"
_DL_CHUNK = 1 << 20  # 1 Mio : on streame (datasets ~20 Mo), jamais tout en RAM

# Statuts TRANSITOIRES : serveur temporairement saturé / indisponible — un re-essai espacé
# a de bonnes chances d'aboutir (Overpass public sert des 429 « rate-limit » et 504 « dispatcher
# too busy » sous charge ; cf. rapport accessibilite-routes). À distinguer des 4xx définitifs
# (400/404/406), qu'il est inutile de retenter.
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)
_BACKOFF_BASE = 1.0   # s ; backoff EXPONENTIEL entre essais : 1, 2, 4… (laisse respirer un serveur saturé)
_BACKOFF_MAX = 20.0   # s ; plafond d'une attente entre deux essais


def _retry_after_seconds(raw):
    """Secondes d'attente demandées par l'en-tête `Retry-After` (forme « nombre de secondes »),
    ou None si absent/non numérique. La forme « date HTTP » (rare sur ces endpoints) est ignorée."""
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def http_get_json(url, params=None, timeout=20, retries=3, require_json=True):
    """GET JSON avec retry/backoff. Lève SkillError si tout échoue.

    Backoff EXPONENTIEL (et honore `Retry-After` s'il est fourni) sur les statuts transitoires
    (429/5xx), où un re-essai espacé aboutit souvent ; échec IMMÉDIAT sur un 4xx définitif
    (retenter un 400/404/406 ne changerait rien — on échoue vite plutôt que d'attendre). Le code
    HTTP du dernier échec est conservé dans la SkillError (`status`) pour que l'appelant distingue
    « temporairement saturé, réessayer » de « réellement indisponible ».

    `require_json` (défaut True) rejette une réponse dont le Content-Type n'annonce pas du
    JSON — garde-fou contre les pages HTML d'erreur servies en 200 (Vigicrues/Hub'Eau).
    Le passer à False pour les endpoints de confiance qui mal-étiquettent leur JSON
    (ex. GitHub raw sert les .json en text/plain) : le parsing JSON reste validé.
    """
    last_err = None
    last_status = None
    for attempt in range(retries):
        retry_after = None
        try:
            resp = requests.get(
                url, params=params, timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code not in _OK_STATUS:
                last_err = "HTTP %s" % resp.status_code
                last_status = resp.status_code
                # Beaucoup d'API (OpenMeteo, geo.api...) décrivent la cause dans un corps JSON
                # sur un 4xx (ex. {"reason": "No data is available for this location"}). On la
                # joint au detail plutôt que de la jeter : un "HTTP 400" seul n'est pas actionnable.
                if "json" in ctype.lower():
                    try:
                        body = resp.json()
                        reason = (body.get("reason") or body.get("message")
                                  or body.get("error_message")) if isinstance(body, dict) else None
                        if reason:
                            last_err += " — %s" % reason
                    except ValueError:
                        pass
                # 4xx définitif (hors 429) : retenter est inutile -> échec immédiat.
                if resp.status_code not in _TRANSIENT_STATUS:
                    break
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            elif require_json and "json" not in ctype.lower():
                last_err = "réponse non-JSON (Content-Type: %s)" % (ctype or "inconnu")
                last_status = None
            else:
                return resp.json()
        except requests.RequestException as exc:
            last_err = str(exc)
            last_status = None
        except ValueError as exc:
            last_err = "JSON invalide : %s" % exc
            last_status = None
        if attempt < retries - 1:
            # Délai serveur (Retry-After) s'il est fourni, sinon backoff exponentiel plafonné.
            delay = retry_after if retry_after is not None else _BACKOFF_BASE * (2 ** attempt)
            time.sleep(min(delay, _BACKOFF_MAX))
    raise SkillError("échec de l'appel à %s" % url, detail=last_err, status=last_status)


def http_get_text(url, timeout=10, retries=2):
    """GET le corps TEXTE d'une URL (ex. SKILL.md brut sur GitHub raw, servi en text/plain).

    Pendant à http_get_json pour le contenu non-JSON : pas de contrôle de Content-Type (GitHub
    raw étiquette les .md en text/plain) et timeout/retries courts car c'est un check best-effort
    non bloquant (vérification de version). Lève SkillError si tout échoue — l'appelant
    (version.check_update) l'avale et dégrade gracieusement.
    """
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
            if resp.status_code not in _OK_STATUS:
                last_err = "HTTP %s" % resp.status_code
            else:
                # GitHub raw n'annonce pas toujours le charset ; les SKILL.md sont en UTF-8.
                resp.encoding = resp.encoding or "utf-8"
                return resp.text
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < retries - 1:
            time.sleep(0.8 * (attempt + 1))  # backoff linéaire
    raise SkillError("échec de l'appel à %s" % url, detail=last_err)


def http_download(url, dest_path, timeout=60, retries=3, expect_content_type=None):
    """Télécharge un fichier binaire (ex. zip de dataset) vers `dest_path`.

    Streame par chunks (datasets volumineux), écrit dans un `.part` puis `os.replace`
    atomique : un téléchargement interrompu ne laisse jamais un cache tronqué. Vérifie
    optionnellement le Content-Type (rejette une page HTML d'erreur servie en 200, comme
    http_get_json). Lève SkillError si tout échoue. Retourne `dest_path`.
    """
    last_err = None
    part = dest_path + ".part"
    for attempt in range(retries):
        try:
            with requests.get(
                url, timeout=timeout, stream=True,
                headers={"User-Agent": _USER_AGENT},
            ) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code not in _OK_STATUS:
                    last_err = "HTTP %s" % resp.status_code
                elif expect_content_type and expect_content_type not in ctype.lower():
                    last_err = ("Content-Type inattendu : %s (attendu : %s)"
                                % (ctype or "inconnu", expect_content_type))
                else:
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=_DL_CHUNK):
                            if chunk:
                                fh.write(chunk)
                    os.replace(part, dest_path)  # publication atomique
                    return dest_path
        except requests.RequestException as exc:
            last_err = str(exc)
        except OSError as exc:
            last_err = "écriture impossible : %s" % exc
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        if attempt < retries - 1:
            time.sleep(0.8 * (attempt + 1))  # backoff linéaire
    raise SkillError("échec du téléchargement de %s" % url, detail=last_err)
