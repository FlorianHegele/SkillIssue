# -*- coding: utf-8 -*-
"""Erreurs contrôlées partagées par tous les skills.

Principe (cf. CLAUDE.md) : jamais de fallback silencieux. Une entrée manquante ou
invalide lève une SkillError explicite (message + detail exploitable) que `main.py`
sérialise en JSON sur stderr avec un code retour != 0.
"""

import json
import sys


class SkillError(Exception):
    """Erreur métier renvoyée proprement à l'utilisateur (pas une trace Python).

    `status` (optionnel) : code HTTP du dernier échec quand l'erreur vient d'un appel réseau.
    Conservé pour que l'appelant distingue un échec TRANSITOIRE (429/5xx → « réessayer ») d'un
    échec définitif, sans re-parser le `detail`. Non sérialisé tel quel : l'appelant le reformule.
    """

    def __init__(self, message, detail=None, status=None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


def fail(message, detail=None, status=None):
    """Raccourci pour lever une SkillError."""
    raise SkillError(message, detail, status)


def emit_error(exc, stream=None, skill=None):
    """Écrit une SkillError en JSON sur stderr (ensure_ascii=False).

    `skill` (optionnel) : bloc de version/MAJ (cf. _common.version.check_update). Joint à la sortie
    d'erreur pour qu'un skill qui ÉCHOUE signale quand même qu'une mise à jour existe — l'IA peut
    alors proposer de mettre à jour le skill plutôt que d'attribuer l'échec à l'entrée utilisateur.
    """
    stream = stream or sys.stderr
    payload = {"error": exc.message}
    if exc.detail is not None:
        payload["detail"] = exc.detail
    if skill is not None:
        payload["skill"] = skill
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
