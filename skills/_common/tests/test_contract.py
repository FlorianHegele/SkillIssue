# -*- coding: utf-8 -*-
"""Tests hors-ligne du module de versioning (_common/version.py).

Aucun réseau : `version.http_get_text` est monkeypatché (même pattern que les tests des skills CSV
qui mockent `ds.http_get_json`). Couvre le parseur de frontmatter, la lecture de version locale, et
le comportement best-effort de check_update (MAJ dispo / à jour / réseau KO / cache TTL / filet anti
-exception). Lançable seul : python skills/_common/tests/test_contract.py (ou via run_tests.py).
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(COMMON_DIR))  # skills/ -> pour le paquet _common

from _common import SkillError, version  # noqa: E402

SKILLMD = """---
name: %s
version: %s
description: >
  Trigger when user asks something. Mots-clés : un, deux,
  trois — lignes indentées qui ne doivent PAS être parsées.
allowed-tools: Bash(python3 *)
---

# Corps du SKILL.md (ignoré par le parseur de frontmatter).
version: 9.9.9
"""


def _make_skill(tmp, name="test-skill", ver="1.0.0"):
    """Écrit un SKILL.md de test dans `tmp` et renvoie le chemin du dossier."""
    skill_dir = os.path.join(tmp, name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(SKILLMD % (name, ver))
    return skill_dir


class FrontmatterTest(unittest.TestCase):
    def test_parse_basic(self):
        fm = version.parse_frontmatter(SKILLMD % ("alerte-crue", "1.2.3"))
        self.assertEqual(fm["name"], "alerte-crue")
        self.assertEqual(fm["version"], "1.2.3")
        self.assertEqual(fm["allowed-tools"], "Bash(python3 *)")

    def test_multiline_block_and_body_ignored(self):
        # `description: >` -> valeur vide (ignorée) ; les lignes indentées du bloc ne matchent pas ;
        # le `version: 9.9.9` du CORPS (après le 2e ---) ne doit pas être lu.
        fm = version.parse_frontmatter(SKILLMD % ("x", "1.0.0"))
        self.assertNotIn("description", fm)
        self.assertEqual(fm["version"], "1.0.0")

    def test_no_delimiters_returns_empty(self):
        self.assertEqual(version.parse_frontmatter("pas de frontmatter\nversion: 1.0.0\n"), {})
        self.assertEqual(version.parse_frontmatter(""), {})
        self.assertEqual(version.parse_frontmatter(None), {})

    def test_read_local_version(self):
        tmp = tempfile.mkdtemp()
        try:
            skill_dir = _make_skill(tmp, "demographie-iris", "2.5.0")
            self.assertEqual(version.read_local_version(skill_dir),
                             ("demographie-iris", "2.5.0"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_local_version_missing_file(self):
        self.assertEqual(version.read_local_version("/n/existe/pas"), (None, None))


class CheckUpdateTest(unittest.TestCase):
    def setUp(self):
        self._orig = version.http_get_text
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self):
        version.http_get_text = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote(self, ver):
        """Mock renvoyant un SKILL.md distant portant la version `ver`."""
        def fake(url, timeout=10):
            return SKILLMD % ("test-skill", ver)
        version.http_get_text = fake

    def test_maj_disponible(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        self._remote("1.1.0")
        block = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertEqual(block["version"], "1.0.0")
        self.assertEqual(block["version_distante"], "1.1.0")
        self.assertTrue(block["maj_disponible"])
        self.assertIsInstance(block["message"], str)
        self.assertEqual(block["source"], "reseau")

    def test_a_jour(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        self._remote("1.0.0")
        block = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertFalse(block["maj_disponible"])
        self.assertIsNone(block["message"])

    def test_locale_plus_recente_que_distante(self):
        # Cas de dev (version locale > GitHub) : pas de MAJ affirmée.
        skill_dir = _make_skill(self.tmp, "test-skill", "2.0.0")
        self._remote("1.0.0")
        block = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertFalse(block["maj_disponible"])

    def test_reseau_ko_est_chaine_explicative(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")

        def boom(url, timeout=10):
            raise SkillError("échec de l'appel", detail="connexion refusée")
        version.http_get_text = boom
        block = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertIn("indisponible", block["version_distante"])
        self.assertFalse(block["maj_disponible"])
        self.assertEqual(block["source"], "reseau")

    def test_distant_sans_version(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        version.http_get_text = lambda url, timeout=10: "# pas de frontmatter du tout\n"
        block = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertIn("indisponible", block["version_distante"])
        self.assertFalse(block["maj_disponible"])

    def test_cache_ttl_evite_le_reseau(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        calls = []

        def fake(url, timeout=10):
            calls.append(url)
            return SKILLMD % ("test-skill", "1.1.0")
        version.http_get_text = fake
        b1 = version.check_update(skill_dir, self.cache, ttl=300)
        b2 = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertEqual(len(calls), 1)           # 2e appel servi par le cache
        self.assertEqual(b1["source"], "reseau")
        self.assertEqual(b2["source"], "cache")
        self.assertEqual(b2["version_distante"], "1.1.0")
        self.assertTrue(b2["maj_disponible"])

    def test_cache_expire_refetch(self):
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        calls = []

        def fake(url, timeout=10):
            calls.append(url)
            return SKILLMD % ("test-skill", "1.1.0")
        version.http_get_text = fake
        version.check_update(skill_dir, self.cache, ttl=0)   # TTL nul -> jamais frais
        version.check_update(skill_dir, self.cache, ttl=0)
        self.assertEqual(len(calls), 2)           # refetch à chaque fois

    def test_cache_recompare_version_locale_mise_a_jour(self):
        # 1er run : local 1.0.0, distant 1.1.0 -> cache. Puis le skill est "mis à jour" (local
        # 1.1.0) et relancé dans la fenêtre TTL : on RECOMPARE -> plus de MAJ, sans refetch.
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")
        self._remote("1.1.0")
        b1 = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertTrue(b1["maj_disponible"])
        _make_skill(self.tmp, "test-skill", "1.1.0")   # réécrit le SKILL.md local en 1.1.0
        b2 = version.check_update(skill_dir, self.cache, ttl=300)
        self.assertEqual(b2["source"], "cache")
        self.assertFalse(b2["maj_disponible"])

    def test_jamais_dexception(self):
        # Une exception non-SkillError dans http_get_text ne doit jamais remonter.
        skill_dir = _make_skill(self.tmp, "test-skill", "1.0.0")

        def kaboom(url, timeout=10):
            raise RuntimeError("inattendu")
        version.http_get_text = kaboom
        block = version.check_update(skill_dir, self.cache, ttl=0)
        # RuntimeError n'est pas une SkillError : c'est le filet ultime qui répond.
        self.assertIn("indisponible", block["version_distante"])
        self.assertFalse(block["maj_disponible"])
        self.assertEqual(block["source"], "local-seul")


if __name__ == "__main__":
    unittest.main(verbosity=2)
