# -*- coding: utf-8 -*-
"""Erreurs contrôlées partagées par tous les skills.

Principe (cf. CLAUDE.md) : jamais de fallback silencieux. Une entrée manquante ou
invalide lève une SkillError explicite (message + detail exploitable) que `main.py`
sérialise en JSON sur stderr avec un code retour != 0.
"""

import json
import sys


class SkillError(Exception):
    """Erreur métier renvoyée proprement à l'utilisateur (pas une trace Python)."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail


def fail(message, detail=None):
    """Raccourci pour lever une SkillError."""
    raise SkillError(message, detail)


def emit_error(exc, stream=None):
    """Écrit une SkillError en JSON sur stderr (ensure_ascii=False)."""
    stream = stream or sys.stderr
    payload = {"error": exc.message}
    if exc.detail is not None:
        payload["detail"] = exc.detail
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
