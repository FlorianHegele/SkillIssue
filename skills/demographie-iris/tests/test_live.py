# -*- coding: utf-8 -*-
"""Sondes LIVE (opt-in) : vérifient que les vraies sources parlent encore la forme attendue.

⚠ Désactivées par défaut (réseau, non déterministes). Pour les lancer :
    RUN_LIVE=1 python skills/demographie-iris/tests/test_live.py
    RUN_LIVE=1 python -m pytest skills/demographie-iris/tests/test_live.py

But : détecter la DÉRIVE (lien INSEE mort, structure de zip/CSV changée, colonnes renommées,
registre GitHub cassé) — pannes que les tests hors-ligne laisseraient au vert. On n'asserte jamais
une valeur (elles changent), seulement la STRUCTURE, plus une validation end-to-end contre le schéma.
Si une URL du registre est morte, c'est le signal qu'il faut maintenir dataset-registry.json.
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> _common
sys.path.insert(0, SKILL_DIR)                   # demographie-iris/ -> main, contract

import main  # noqa: E402
from _common import http_get_json, resolve_commune, reverse_commune, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
LIVE = os.environ.get("RUN_LIVE") == "1"

ALES_LAT, ALES_LON = 44.125, 4.0905


@unittest.skipUnless(LIVE, "sondes live désactivées (mettre RUN_LIVE=1 pour activer)")
class LiveProbes(unittest.TestCase):

    def test_geo_api_resolves_ales(self):
        loc = resolve_commune("Alès")
        self.assertEqual(loc.code_insee, "30007")

    def test_reverse_commune_shape(self):
        loc = reverse_commune(ALES_LAT, ALES_LON)
        self.assertIsInstance(loc.code_insee, str)
        self.assertEqual(len(loc.code_insee), 5)

    def test_registry_remote_is_valid_json(self):
        reg = http_get_json(main.REGISTRY_URL, timeout=30, require_json=False)  # raw = text/plain
        self.assertIn("entries", reg)
        self.assertGreater(len(reg["entries"]), 0)
        for e in reg["entries"]:
            self.assertIn("url_metropole", e)
            self.assertIn("min_skill_version", e)

    def test_download_and_columns(self):
        """Télécharge réellement les 2 fichiers du dernier millésime compatible et vérifie
        la structure : vrai ZIP, CSV de données (membre non meta_) avec IRIS/COM/<prefix>MEN."""
        entry, _ = main.resolve_source(tempfile.mkdtemp(), 60)
        for zone, prefix_required in (("metropole", True), ("com", True)):
            with tempfile.TemporaryDirectory() as tmp:
                csv_path, meta = main.dataset_path(entry, zone, tmp, True, 120)
                self.assertTrue(os.path.getsize(csv_path) > 0)
                with open(csv_path, "rb") as fh:
                    head = fh.readline().decode(main._csv_encoding(csv_path), "replace")
                self.assertIn("IRIS", head)
                self.assertIn("COM", head)
                prefix = main.resolve_prefix(head.strip().split(main.CSV_SEP), entry)
                self.assertRegex(prefix, r"^C\d{2}_$")

    def test_end_to_end_metropole_conforms(self):
        args = main.build_parser().parse_args(["--commune", "Alès"])
        out, code = main.run(args)
        validate(out, SCHEMA)
        self.assertNotIn("error", out.get("demographie", {}))
        self.assertGreater(out["demographie"]["commune"]["iris_count"], 0)

    def test_end_to_end_com_conforms(self):
        # 97501 = Saint-Pierre (Saint-Pierre-et-Miquelon), couvert par le fichier COM.
        args = main.build_parser().parse_args(["--commune", "97501"])
        out, _ = main.run(args)
        validate(out, SCHEMA)
        self.assertEqual(out["dataset"]["zone"], "com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
