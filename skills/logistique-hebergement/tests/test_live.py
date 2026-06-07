# -*- coding: utf-8 -*-
"""Sondes LIVE (opt-in) : vérifient qu'Overpass parle encore la forme attendue.

⚠ Désactivées par défaut (réseau, non déterministes). Pour les lancer :
    RUN_LIVE=1 python skills/logistique-hebergement/tests/test_live.py
    RUN_LIVE=1 python -m pytest skills/logistique-hebergement/tests/test_live.py

But : détecter la DÉRIVE (endpoint mort, schéma `elements` changé, `out geom` qui ne renvoie plus
de `geometry`/`center`) — pannes que les tests hors-ligne laisseraient au vert. On n'asserte jamais
une valeur (elles changent), seulement la STRUCTURE, plus une validation end-to-end.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> _common
sys.path.insert(0, SKILL_DIR)                   # logistique-hebergement/ -> main, contract

import main  # noqa: E402
from _common import resolve_commune, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
LIVE = os.environ.get("RUN_LIVE") == "1"

ALES_LAT, ALES_LON = 44.128, 4.081


@unittest.skipUnless(LIVE, "sondes live désactivées (mettre RUN_LIVE=1 pour activer)")
class LiveProbes(unittest.TestCase):

    def test_geo_api_resolves_ales(self):
        loc = resolve_commune("Alès")
        self.assertEqual(loc.code_insee, "30007")

    def test_overpass_returns_elements_with_geometry_and_tags(self):
        """Une vraie requête autour d'Alès : les ways doivent porter `geometry` + `tags`."""
        ql = main.build_query(ALES_LAT, ALES_LON, 2000, 25)
        data = main.overpass_query(ql, 25)
        self.assertIn("elements", data)
        self.assertTrue(data["elements"], "Alès devrait avoir des sites d'hébergement candidats")
        ways = [e for e in data["elements"] if e.get("type") == "way"]
        self.assertTrue(ways, "Alès devrait avoir des ways (gymnases/écoles) dans 2000 m")
        sample = ways[0]
        self.assertIn("tags", sample)
        self.assertTrue("geometry" in sample or "center" in sample)

    def test_end_to_end_conforms(self):
        args = main.build_parser().parse_args(["--commune", "Alès"])
        out, code = main.run(args)
        validate(out, SCHEMA)
        self.assertNotIn("error", out.get("hebergement", {}))
        # Alès (sous-préfecture) a forcément des écoles/hôtels -> au moins un site.
        self.assertGreater(out["hebergement"]["resume"]["sites_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
