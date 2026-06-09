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

# Endpoint Overpass sans clé (vérifié live, voir references/api.md des skills).
PRIMARY = "https://overpass-api.de/api/interpreter"

# Pas de miroir public CODÉ EN DUR. Au 9 juin 2026, tous les miroirs Overpass gratuits sont
# inexploitables : kumi.systems accepte le TCP mais ne répond JAMAIS (timeout garanti) ;
# overpass.private.coffee est injoignable ; maps.mail.ru est suspendu (HTTP 403 depuis mars 2026) ;
# overpass.osm.ch est RÉGIONAL (Suisse) et renvoie 200 + 0 élément pour la France — soit un « faux
# secteur vide » formellement interdit (cf. CLAUDE.md). Un miroir mort coûtait jusqu'à ~225 s de
# blocage (timeout+15 × retries). On retire donc le miroir par défaut et on s'appuie sur le retry
# du primaire (les 429/504 d'Overpass sont transitoires). Un miroir GLOBAL vérifié peut être fourni
# via l'environnement `FLOOD_OVERPASS_MIRROR` ; il n'est alors tenté qu'une fois, en timeout court.
MIRROR = None

# Statuts transitoires : Overpass public sature par intermittence (429 rate-limit, 504 « dispatcher
# too busy ») — l'échec dit « réessaie », pas « cassé ». On le reformule pour l'utilisateur/l'IA.
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _configured_mirror():
    """Miroir global explicitement fourni par l'utilisateur (env), ou None."""
    return MIRROR or os.environ.get("FLOOD_OVERPASS_MIRROR") or None


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


def query(ql, timeout, get_json, primary=PRIMARY, mirror=None, retries=4):
    """Exécute le QL sur Overpass (le QL passe en query-string `?data=`). Lève SkillError en échec.

    `get_json` = client HTTP injecté (le `http_get_json` du skill, patchable en test). Il distingue
    déjà les statuts transitoires (429/5xx) qu'il retente avec backoff exponentiel des 4xx définitifs
    (échec immédiat), et conserve le code HTTP. Comme Overpass sature par intermittence, on lui
    accorde plus d'essais (`retries`) sur le primaire que le défaut. Marge de timeout HTTP au-dessus
    du `[timeout:]` du QL. Un `remark` d'erreur serveur (rendu en 200) est traité comme un échec
    (check_remark) plutôt que relayé comme un faux résultat vide.

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
        if not mirror:
            _raise_unavailable(exc_primary)
        try:
            return check_remark(get_json(mirror, params={"data": ql},
                                         timeout=min(http_timeout, 30), retries=1))
        except SkillError as exc_mirror:
            _raise_unavailable(exc_primary, exc_mirror)
