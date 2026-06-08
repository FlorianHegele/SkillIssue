#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lance toute la suite de tests hors-ligne du plugin flood-response, en une commande.

    python run_tests.py            # les 5 suites tests/test_contract.py (hors-ligne, déterministes)
    python run_tests.py --live     # AJOUTE les sondes live (réseau) via RUN_LIVE=1

Pourquoi ce script plutôt que `pytest` à la racine : chaque skill a un `tests/test_contract.py`
(même basename) qui fait `import main` / `import contract` (mêmes noms d'un skill à l'autre).
Une collecte pytest unique télescoperait ces modules dans `sys.modules` (collision : les tests
d'un skill s'exécuteraient contre le `main` d'un autre). On exécute donc CHAQUE fichier de test
dans un **sous-process isolé** — exactement comme un lancement individuel, qui lui passe.

Interpréteur : le venv `.venv/bin/python` à la racine s'il existe (cf. CLAUDE.md), sinon
l'interpréteur courant. Code retour != 0 si au moins une suite échoue.
"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _interpreter():
    """Interpréteur des sous-process : .venv/bin/python (ou Scripts/python.exe) si présent."""
    for rel in (os.path.join(".venv", "bin", "python"),
                os.path.join(".venv", "Scripts", "python.exe")):
        cand = os.path.join(ROOT, rel)
        if os.path.exists(cand):
            return cand
    return sys.executable


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    live = "--live" in argv
    py = _interpreter()

    patterns = ["skills/*/tests/test_contract.py"]
    if live:
        patterns.append("skills/*/tests/test_live.py")
    files = sorted(f for p in patterns for f in glob.glob(os.path.join(ROOT, p)))
    if not files:
        sys.stderr.write("aucun fichier de test trouvé (skills/*/tests/test_*.py)\n")
        return 1

    env = dict(os.environ)
    if live:
        env["RUN_LIVE"] = "1"

    print("Interpréteur : %s" % py)
    print("Mode : %s\n" % ("hors-ligne + live" if live else "hors-ligne"))

    failures = []
    for path in files:
        rel = os.path.relpath(path, ROOT)
        print("=" * 70)
        print("▶ %s" % rel)
        print("=" * 70)
        code = subprocess.call([py, path], cwd=ROOT, env=env)
        if code != 0:
            failures.append(rel)
        print("")

    print("=" * 70)
    if failures:
        print("ÉCHEC : %d/%d suite(s) en erreur :" % (len(failures), len(files)))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("OK : %d suite(s) vertes." % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
