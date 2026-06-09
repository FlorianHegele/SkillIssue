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
  - re-exec une seule fois (détection « on tourne déjà dans le venv ») ;
  - opt-out par FLOOD_NO_BOOTSTRAP=1 (CI, venv déjà activé à la main) ;
  - échec d'installation = message explicite sur stderr + code retour ≠ 0 (pas de fallback).
"""

import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_DIR = os.path.join(_REPO_ROOT, ".venv")
_REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
# Sentinelle d'environnement : évite toute boucle de re-exec si jamais la détection par chemin
# échoue (montages exotiques, liens symboliques).
_GUARD = "FLOOD_BOOTSTRAPPED"
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


def _same_path(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _log(msg):
    """Message de progression sur stderr (stdout est réservé au JSON du skill)."""
    sys.stderr.write("[flood-response/bootstrap] %s\n" % msg)
    sys.stderr.flush()


def _run(cmd):
    """Lance une commande en redirigeant TOUTE sa sortie vers stderr (jamais stdout)."""
    subprocess.run(cmd, check=True, stdout=sys.stderr, stderr=sys.stderr)


def _create_venv(venv_python):
    """Crée `.venv` et y installe requirements.txt. Préfère `uv` s'il est présent."""
    if not os.path.exists(_REQUIREMENTS):
        raise RuntimeError("requirements.txt introuvable (%s)" % _REQUIREMENTS)

    from shutil import which

    if which("uv"):
        _log("uv détecté — création du venv et installation des dépendances…")
        _run(["uv", "venv", _VENV_DIR])
        _run(["uv", "pip", "install", "--python", venv_python, "-r", _REQUIREMENTS])
    else:
        _log("création du venv (%s) via python -m venv…" % _VENV_DIR)
        _run([sys.executable, "-m", "venv", _VENV_DIR])
        _log("installation des dépendances (requirements.txt)…")
        _run([venv_python, "-m", "pip", "install", "--upgrade", "pip", "-q"])
        _run([venv_python, "-m", "pip", "install", "-q", "-r", _REQUIREMENTS])
    _log("environnement prêt.")


def ensure_runtime():
    """Garantit que le skill tourne dans le venv local (le crée au 1er appel), puis re-exec.

    Idempotent : ne fait rien si on est déjà dans le venv ou si FLOOD_NO_BOOTSTRAP=1.
    """
    if os.environ.get("FLOOD_NO_BOOTSTRAP"):
        return

    # Cas le plus courant : les dépendances sont déjà là (venv actif, deps système, ou 2e appel
    # après re-exec). On ne touche à rien — pas de venv, pas de magie.
    if _deps_present():
        return

    venv_python = _venv_python(_VENV_DIR)

    # Déjà à l'intérieur du venv (par chemin OU par sentinelle) : rien à faire.
    if _same_path(sys.executable, venv_python) or os.environ.get(_GUARD):
        return

    if not os.path.exists(venv_python):
        try:
            _create_venv(venv_python)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
            _log("ÉCHEC de la préparation de l'environnement : %s" % exc)
            _log(
                "Pistes : vérifier l'accès réseau (PyPI), que le module venv est installé "
                "(paquet python3-venv sur Debian/Ubuntu), ou créer le venv à la main : "
                "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
            )
            sys.exit(1)

    # Re-exécute le même script avec l'interpréteur du venv (où les dépendances existent).
    env = dict(os.environ)
    env[_GUARD] = "1"
    try:
        os.execve(venv_python, [venv_python] + sys.argv, env)
    except OSError as exc:  # pragma: no cover - cas dégénéré (venv corrompu)
        _log("impossible de re-exécuter avec %s : %s" % (venv_python, exc))
        sys.exit(1)
