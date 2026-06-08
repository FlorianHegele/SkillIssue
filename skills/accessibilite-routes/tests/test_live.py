# -*- coding: utf-8 -*-
"""Sondes LIVE (opt-in) : vérifient qu'Overpass parle encore la forme attendue.

⚠ Désactivées par défaut (réseau, non déterministes). Pour les lancer :
    RUN_LIVE=1 python skills/accessibilite-routes/tests/test_live.py
    RUN_LIVE=1 python -m pytest skills/accessibilite-routes/tests/test_live.py

But : détecter la DÉRIVE (endpoint mort, schéma `elements` changé, `out tags center;` qui ne
renvoie plus de position) — pannes que les tests hors-ligne laisseraient au vert. On n'asserte
jamais une valeur (elles changent), seulement la STRUCTURE, plus une validation end-to-end.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> _common
sys.path.insert(0, SKILL_DIR)                   # accessibilite-routes/ -> main, contract

import main  # noqa: E402
from _common import resolve_commune, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
LIVE = os.environ.get("RUN_LIVE") == "1"

ALES_LAT, ALES_LON = 44.128, 4.081


@unittest.skipUnless(LIVE, "sondes live désactivées (mettre RUN_LIVE=1 pour activer)")
class LiveProbes(unittest.TestCase):

    def test_remote_skillmd_has_parsable_version(self):
        """Le SKILL.md distant (GitHub main) doit exposer un `version:` parsable : détecte la
        dérive (frontmatter sans version après le rendu). N.B. tant que le commit ajoutant
        `version:` n'est pas poussé sur main, version_distante reste « indisponible »."""
        import tempfile
        from _common import version
        block = version.check_update(SKILL_DIR, tempfile.mkdtemp(), timeout=10)
        self.assertRegex(block["version_distante"], r"^\d+\.\d+",
                         "SKILL.md distant sans version parsable : %s" % block["version_distante"])

    def test_geo_api_resolves_ales(self):
        loc = resolve_commune("Alès")
        self.assertEqual(loc.code_insee, "30007")

    def test_overpass_returns_elements_with_center_and_tags(self):
        """Une vraie requête autour d'Alès : les ways doivent porter `center` + `tags`."""
        ql = main.build_query(ALES_LAT, ALES_LON, 1500, 25)
        data = main.overpass_query(ql, 25)
        self.assertIn("elements", data)
        ways = [e for e in data["elements"] if e.get("type") == "way"]
        self.assertTrue(ways, "Alès devrait avoir des ways (ponts/tunnels) dans 1500 m")
        sample = ways[0]
        self.assertIn("center", sample)
        self.assertIn("lat", sample["center"])
        self.assertIn("tags", sample)

    def test_overpass_excludes_pedestrian_ways(self):
        """Le filtre EXCLUDE_HIGHWAY est posé dans le QL (testé hors-ligne) ; ici on vérifie
        qu'Overpass l'honore réellement : aucun way retourné ne doit porter un highway piéton."""
        exclus = set(main.EXCLUDE_HIGHWAY.split("|"))
        ql = main.build_query(ALES_LAT, ALES_LON, 1500, 25)
        data = main.overpass_query(ql, 25)
        ways = [e for e in data["elements"] if e.get("type") == "way"]
        offenders = [e for e in ways if (e.get("tags", {}).get("highway") in exclus)]
        self.assertEqual(offenders, [], "des voies piétonnes ont fui le filtre Overpass")

    def test_end_to_end_conforms(self):
        args = main.build_parser().parse_args(["--commune", "Alès"])
        out, code = main.run(args)
        validate(out, SCHEMA)
        self.assertNotIn("error", out.get("accessibilite", {}))
        # Alès (vallée du Gardon, sous-préfecture) a forcément des ponts -> au moins un ouvrage.
        self.assertGreater(out["accessibilite"]["resume"]["ouvrages_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
