# -*- coding: utf-8 -*-
"""Client Overpass partagé (skills accessibilite-routes & logistique-hebergement).

Mutualise l'accès à OpenStreetMap via Overpass : endpoints + miroir, repli, et surtout la garde
`remark`. Piège : un timeout / dépassement mémoire côté serveur renvoie HTTP 200 + JSON VALIDE de
la forme {"elements": [], "remark": "runtime error: Query timed out…"}. Sans garde, la réponse
passe le client HTTP (200 + Content-Type JSON) et serait lue comme « secteur sans aucun élément »
(compteurs à 0, code 0) — un fallback silencieux interdit (cf. CLAUDE.md : distinguer « capteur
muet » d'« API en panne »). Confiné ici UNE seule fois pour que les deux skills Overpass aient
exactement la même robustesse et ne puissent plus diverger.

Le client HTTP (`get_json`) est INJECTÉ par l'appelant plutôt qu'importé en dur : le wrapper de
chaque skill passe sa propre référence `http_get_json` (module-global, donc patchable par les
tests hors-ligne). `build_query` (le QL) reste propre à chaque skill.
"""

import os

from .errors import SkillError, fail

# Endpoint Overpass primaire, sans clé (vérifié live, voir references/api.md des skills).
PRIMARY = "https://overpass-api.de/api/interpreter"

# Miroir de repli = instance Overpass d'OpenStreetMap France. CHOIX vérifié live (9 juin 2026) :
# couvre la métropole ET les DOM (Réunion, Guadeloupe, Mayotte testés → données réelles, PAS un
# faux vide), et c'est un dispatcher DISTINCT du primaire — donc disponible quand overpass-api.de
# renvoie 504 « dispatcher too busy ». Adapté à ce projet 100 % France. Les autres miroirs gratuits
# sont écartés : kumi.systems muet (coûtait ~225 s de blocage), private.coffee injoignable,
# maps.mail.ru suspendu (403 depuis mars 2026), overpass.osm.ch RÉGIONAL Suisse (→ 0 pour la France,
# faux secteur vide INTERDIT cf. CLAUDE.md). Surchargeable via l'env `FLOOD_OVERPASS_MIRROR`
# (doit viser une instance couvrant la France) ; mettre la chaîne vide pour désactiver tout repli.
MIRROR = "https://overpass.openstreetmap.fr/api/interpreter"

# Statuts transitoires : Overpass public sature par intermittence (429 rate-limit, 504 « dispatcher
# too busy ») — l'échec dit « réessaie », pas « cassé ». On le reformule pour l'utilisateur/l'IA.
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _is_query_timeout(exc):
    """L'échec vient-il d'une requête TROP LOURDE (read-timeout, ou remark « Query timed out »
    rendu en 200) — par opposition à une saturation transitoire (429/504) ? Si oui, basculer sur
    le miroir est vain : il exécuterait la même requête lourde et time-outerait à l'identique."""
    if getattr(exc, "status", None) in _TRANSIENT_STATUS:
        return False
    blob = ((exc.detail if isinstance(exc.detail, str) else "")
            + " " + (exc.message or "")).lower()
    return any(s in blob for s in ("timed out", "timeout", "délai dépassé", "tronquée"))


def _configured_mirror():
    """Miroir de repli effectif. L'env `FLOOD_OVERPASS_MIRROR`, si DÉFINIE (y compris vide =
    repli désactivé), surcharge le défaut `MIRROR` (OSM France)."""
    env = os.environ.get("FLOOD_OVERPASS_MIRROR")
    if env is not None:
        return env or None        # chaîne vide -> pas de repli
    return MIRROR or None


def _raise_unavailable(exc_primary, exc_mirror=None):
    """Lève l'erreur finale en REMONTANT le vrai code HTTP et en distinguant
    « temporairement saturé » (transitoire → réessayer) d'« indisponible » (durable).

    Sans miroir, un échec primaire déjà explicite (remark de troncature serveur) est remonté
    tel quel ; un échec HTTP transitoire est reformulé en « réessayer dans ~1 min »."""
    status = getattr(exc_primary, "status", None)
    transient = status in _TRANSIENT_STATUS
    if exc_mirror is None:
        if transient:
            fail("Overpass temporairement saturé (HTTP %s) — réessayer dans ~1 min" % status,
                 detail=exc_primary.detail, status=status)
        raise exc_primary  # message déjà explicite (remark tronqué, non-JSON, réseau…)
    msg = ("Overpass temporairement saturé (HTTP %s) — réessayer dans ~1 min" % status if transient
           else "Overpass indisponible (serveur principal et miroir)")
    fail(msg, detail={"principal": exc_primary.detail or exc_primary.message,
                      "miroir": exc_mirror.detail or exc_mirror.message}, status=status)


def check_remark(data):
    """Lève SkillError si la réponse Overpass porte un `remark` d'erreur serveur (timeout/OOM
    rendu en 200) ; sinon retourne `data`. Un `remark` informatif bénin (sans mot d'erreur) passe."""
    remark = data.get("remark") if isinstance(data, dict) else None
    if remark and ("error" in remark.lower() or "timed out" in remark.lower()):
        raise SkillError("Overpass a renvoyé une réponse tronquée (timeout/ressources serveur)",
                         detail=remark)
    return data


def query(ql, timeout, get_json, primary=PRIMARY, mirror=None, retries=2):
    """Exécute le QL sur Overpass (le QL passe en query-string `?data=`). Lève SkillError en échec.

    `get_json` = client HTTP injecté (le `http_get_json` du skill, patchable en test). Il retente
    les statuts transitoires « rapides » (429/504 servis vite sous charge) avec backoff exponentiel,
    échoue VITE sur un read-timeout (réessayer une requête trop lourde ne l'accélère pas) et sur un
    4xx définitif, et conserve le code HTTP. Quelques essais suffisent sur le primaire car le repli
    miroir (OSM France) prend le relais. Marge de timeout HTTP au-dessus du `[timeout:]` du QL. Un
    `remark` d'erreur serveur (rendu en 200) est traité comme un échec (check_remark) plutôt que
    relayé comme un faux résultat vide.

    Repli sur un miroir UNIQUEMENT s'il est explicitement configuré (env `FLOOD_OVERPASS_MIRROR`,
    qui DOIT viser une instance globale) : alors une seule tentative, en timeout court, pour ne pas
    s'acharner sur un serveur muet. Sinon, on remonte l'échec du primaire avec son vrai code HTTP.
    """
    http_timeout = timeout + 15
    mirror = mirror or _configured_mirror()
    try:
        return check_remark(get_json(primary, params={"data": ql},
                                     timeout=http_timeout, retries=retries))
    except SkillError as exc_primary:
        # Requête trop lourde côté primaire (read-timeout ou remark timeout) : le miroir, qui
        # exécuterait la MÊME requête, time-outerait à l'identique. On ne double donc PAS l'attente
        # (≠ d'un 504 « dispatcher busy » rapide, où le miroir, dispatcher distinct, prend le relais).
        if not mirror or _is_query_timeout(exc_primary):
            _raise_unavailable(exc_primary)
        try:
            return check_remark(get_json(mirror, params={"data": ql},
                                         timeout=min(http_timeout, 30), retries=1))
        except SkillError as exc_mirror:
            _raise_unavailable(exc_primary, exc_mirror)
