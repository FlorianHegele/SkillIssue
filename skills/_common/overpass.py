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

from .errors import SkillError, fail

# Endpoints Overpass sans clé (vérifiés live, voir references/api.md des skills).
PRIMARY = "https://overpass-api.de/api/interpreter"
MIRROR = "https://overpass.kumi.systems/api/interpreter"


def check_remark(data):
    """Lève SkillError si la réponse Overpass porte un `remark` d'erreur serveur (timeout/OOM
    rendu en 200) ; sinon retourne `data`. Un `remark` informatif bénin (sans mot d'erreur) passe."""
    remark = data.get("remark") if isinstance(data, dict) else None
    if remark and ("error" in remark.lower() or "timed out" in remark.lower()):
        raise SkillError("Overpass a renvoyé une réponse tronquée (timeout/ressources serveur)",
                         detail=remark)
    return data


def query(ql, timeout, get_json, primary=PRIMARY, mirror=MIRROR):
    """Exécute le QL sur Overpass (le QL passe en query-string `?data=`), avec repli sur le miroir.
    Lève SkillError si les deux échouent.

    `get_json` = client HTTP injecté (le `http_get_json` du skill, patchable en test). Il rejette
    déjà les pages HTML d'erreur (406/429/504 servies en 200) via la garde Content-Type et retente
    avec backoff. Marge de timeout HTTP au-dessus du `[timeout:]` du QL. Un `remark` d'erreur
    serveur (rendu en 200) est traité comme un échec (check_remark) : on tente alors le miroir
    plutôt que de relayer un faux résultat vide.
    """
    http_timeout = timeout + 15
    try:
        return check_remark(get_json(primary, params={"data": ql}, timeout=http_timeout))
    except SkillError as exc_primary:
        try:
            return check_remark(get_json(mirror, params={"data": ql}, timeout=http_timeout))
        except SkillError as exc_mirror:
            fail("Overpass indisponible (serveur principal et miroir)",
                 detail={"principal": exc_primary.message, "miroir": exc_mirror.message})
