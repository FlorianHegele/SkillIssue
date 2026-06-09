# -*- coding: utf-8 -*-
"""Amorçage de l'environnement d'exécution des skills (stdlib pure, zéro dépendance tierce).

Problème résolu : un skill installé via Claude Code tourne sur le `python3` de l'utilisateur,
où `requests`/`shapely` ne sont pas forcément présents. Or sur les distributions récentes
(Arch, Debian, Ubuntu, Fedora) le Python système est « externally managed » (PEP 668) : un
`pip install` y échoue. PEP 668 ne s'applique PAS à un venv : on crée donc un venv local au
repo, on y installe `requirements.txt`, et on re-exécute le skill avec le Python de ce venv.

`ensure_runtime()` doit être appelé tout en haut de chaque `main.py`, AVANT `from _common …`
(qui importe `requests`). Ce module n'importe que la stdlib pour rester chargeable même quand
aucune dépendance n'est installée.

Garde-fous :
  - jamais d'écriture sur stdout (réservé au JSON du skill) — tout va sur stderr ;
  - un venv INCOMPLET (deps partielles) est réparé, jamais accepté en silence ;
  - nombre de re-exec borné par un compteur (jamais de boucle) ; échec persistant = erreur dure ;
  - opt-out par FLOOD_NO_BOOTSTRAP=1 (CI, venv déjà activé à la main) ;
  - échec d'installation = message explicite sur stderr + code retour ≠ 0 (pas de fallback).
"""

import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_DIR = os.path.join(_REPO_ROOT, ".venv")
_REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
# Compteur de tentatives propagé via l'environnement aux processus re-exécutés : borne dure
# contre toute boucle (venv qui resterait incomplet après réparation -> erreur explicite).
_GUARD = "FLOOD_BOOTSTRAP_ATTEMPTS"
_MAX_ATTEMPTS = 2
# Modules tiers du runtime (cf. requirements.txt) dont la présence rend tout bootstrap inutile.
# `jsonschema` n'y figure pas : il n'est utilisé qu'en test (import paresseux dans contract.py).
_RUNTIME_MODULES = ("requests", "shapely")


def _deps_present():
    """Vrai si toutes les dépendances runtime sont déjà importables dans cet interpréteur.

    Sonde non destructive (importlib.util.find_spec, sans exécuter le module). Si tout est là,
    aucun bootstrap n'est nécessaire : le skill tourne tel quel, y compris pour la suite de
    tests hors-ligne lancée dans un environnement déjà équipé.
    """
    from importlib.util import find_spec

    try:
        return all(find_spec(name) is not None for name in _RUNTIME_MODULES)
    except (ImportError, ValueError):
        return False


def _venv_python(venv_dir):
    """Chemin de l'interpréteur Python à l'intérieur d'un venv (POSIX et Windows)."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _log(msg):
    """Message de progression sur stderr (stdout est réservé au JSON du skill)."""
    sys.stderr.write("[flood-response/bootstrap] %s\n" % msg)
    sys.stderr.flush()


def _run(cmd):
    """Lance une commande en redirigeant TOUTE sa sortie vers stderr (jamais stdout)."""
    subprocess.run(cmd, check=True, stdout=sys.stderr, stderr=sys.stderr)


def _install_requirements(venv_python):
    """(Ré)installe requirements.txt DANS le venv. Idempotent (pip ignore le déjà-satisfait)."""
    from shutil import which

    if which("uv"):
        _run(["uv", "pip", "install", "--python", venv_python, "-r", _REQUIREMENTS])
    else:
        _run([venv_python, "-m", "pip", "install", "--upgrade", "pip", "-q"])
        _run([venv_python, "-m", "pip", "install", "-q", "-r", _REQUIREMENTS])


def _provision_venv(venv_python):
    """Garantit un venv présent ET pourvu de requirements.txt. Préfère `uv` s'il est présent.

    Crée le venv s'il manque, puis (ré)installe les dépendances — ce qui RÉPARE aussi un venv
    préexistant mais incomplet (cas d'un `pip install` partiel ou d'un venv antérieur à l'ajout
    d'une dépendance).
    """
    if not os.path.exists(_REQUIREMENTS):
        raise RuntimeError("requirements.txt introuvable (%s)" % _REQUIREMENTS)

    from shutil import which

    if not os.path.exists(venv_python):
        if which("uv"):
            _log("uv détecté — création du venv…")
            _run(["uv", "venv", _VENV_DIR])
        else:
            _log("création du venv (%s) via python -m venv…" % _VENV_DIR)
            _run([sys.executable, "-m", "venv", _VENV_DIR])
    else:
        _log("venv présent mais dépendances incomplètes — réparation…")

    _log("installation des dépendances (requirements.txt)…")
    _install_requirements(venv_python)
    _log("environnement prêt.")


def ensure_runtime():
    """Garantit que les dépendances runtime sont disponibles, sinon provisionne le venv et re-exec.

    Ne fait rien si les deps sont déjà importables (venv actif, deps système, 2e appel après
    re-exec) ou si FLOOD_NO_BOOTSTRAP=1. Un venv incomplet est réparé (réinstallation) plutôt
    qu'accepté ; un échec persistant après réparation lève une erreur dure (jamais de boucle).
    """
    if os.environ.get("FLOOD_NO_BOOTSTRAP"):
        return

    # Cas le plus courant : les dépendances sont déjà là (venv actif, deps système, ou appel
    # postérieur au re-exec). On ne touche à rien — pas de venv, pas de magie.
    if _deps_present():
        return

    # Dépendances manquantes dans CET interpréteur. On a déjà tenté le maximum ? Erreur dure :
    # le venv reste incomplet malgré (ré)installation — inutile de re-exécuter en boucle.
    attempts = int(os.environ.get(_GUARD, "0") or "0")
    if attempts >= _MAX_ATTEMPTS:
        _log(
            "dépendances runtime toujours absentes après %d tentative(s) de provisionnement."
            % attempts
        )
        _log(
            "Pistes : vérifier l'accès réseau (PyPI) et l'installation manuelle : "
            "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        )
        sys.exit(1)

    venv_python = _venv_python(_VENV_DIR)
    try:
        _provision_venv(venv_python)
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        _log("ÉCHEC de la préparation de l'environnement : %s" % exc)
        _log(
            "Pistes : vérifier l'accès réseau (PyPI), que le module venv est installé "
            "(paquet python3-venv sur Debian/Ubuntu), ou créer le venv à la main : "
            "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        )
        sys.exit(1)

    # Re-exécute le même script avec l'interpréteur du venv (où les dépendances existent
    # désormais), en incrémentant le compteur de tentatives.
    env = dict(os.environ)
    env[_GUARD] = str(attempts + 1)
    try:
        os.execve(venv_python, [venv_python] + sys.argv, env)
    except OSError as exc:  # pragma: no cover - cas dégénéré (venv corrompu)
        _log("impossible de re-exécuter avec %s : %s" % (venv_python, exc))
        sys.exit(1)
