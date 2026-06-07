# -*- coding: utf-8 -*-
"""Tests hors-ligne du contrat de sortie d'accessibilite-routes.

On remplace l'accès réseau (Overpass) et le géocodage par des mocks, et on valide la sortie
contre contract.schema.json. Aucun réseau. Lançable seul :
    python tests/test_contract.py   (ou via pytest).

Fixture `fixtures/overpass_ales.json` = réponse Overpass réduite (secteur Alès) :
  - node ford=yes                          -> gué
  - way bridge=yes (+ layer=1)             -> pont          (bridge prioritaire sur layer positif)
  - way tunnel=yes (+ layer=-1)            -> tunnel        (tunnel prioritaire sur passage)
  - way layer=-1                           -> passage_inférieur
  - way flood_prone=yes                    -> zone_inondable
  - way bridge=yes SANS center             -> pont, position absente (mesure -> chaîne)
  - way layer=1 (positif, ni pont/tunnel)  -> IGNORÉ
Décompte attendu : 6 ouvrages (1 gué, 2 ponts, 1 tunnel, 1 passage, 1 zone inondable).
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> pour _common
sys.path.insert(0, SKILL_DIR)                   # accessibilite-routes/ -> pour main, contract

import main  # noqa: E402
from _common import Lieu, SkillError, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
FIXTURE = os.path.join(HERE, "fixtures", "overpass_ales.json")

with open(FIXTURE, encoding="utf-8") as _fh:
    OVERPASS_ALES = json.load(_fh)

# Centre d'Alès (proche des ouvrages de la fixture).
ALES = Lieu(commune="Alès", code_insee="30007", lat=44.128, lon=4.081)


class ContractTest(unittest.TestCase):
    def setUp(self):
        self._orig = {k: getattr(main, k) for k in ("overpass_query", "resolve_location")}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(main, k, v)

    def _mock(self, loc=ALES, data=None):
        data = OVERPASS_ALES if data is None else data
        main.resolve_location = lambda c, lat, lon, t: loc
        main.overpass_query = lambda ql, timeout: data

    def _run(self, argv):
        return main.run(main.build_parser().parse_args(argv))

    # --- conformité + décompte stable ----------------------------------------
    def test_output_conforms_and_stable_counts(self):
        self._mock()
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        r = out["accessibilite"]["resume"]
        self.assertEqual(r["ouvrages_total"], 6)
        self.assertEqual(r["gues"], 1)
        self.assertEqual(r["ponts"], 2)
        self.assertEqual(r["tunnels"], 1)
        self.assertEqual(r["passages_inferieurs"], 1)
        self.assertEqual(r["zones_inondables"], 1)
        self.assertEqual(len(out["accessibilite"]["ouvrages_a_risque"]), 6)
        self.assertEqual(out["accessibilite"]["rayon_m"], main.DEFAULT_RADIUS_M)
        self.assertIn("Géorisques", out["accessibilite"]["note"])
        self.assertEqual(out["lieu"]["code_insee"], "30007")

    def test_positive_layer_only_is_ignored(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        ids = [o["osm_id"] for o in out["accessibilite"]["ouvrages_a_risque"]]
        self.assertNotIn("way/105", ids)   # layer=1 positif, ni pont/tunnel/gué -> écarté

    def test_kind_classification_priority(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        by_id = {o["osm_id"]: o for o in out["accessibilite"]["ouvrages_a_risque"]}
        self.assertEqual(by_id["node/1001"]["kind"], "gué")
        self.assertEqual(by_id["way/100"]["kind"], "pont")    # bridge prioritaire sur layer=1
        self.assertEqual(by_id["way/101"]["kind"], "tunnel")  # tunnel prioritaire sur layer=-1
        self.assertEqual(by_id["way/102"]["kind"], "passage_inférieur")
        self.assertEqual(by_id["way/103"]["kind"], "zone_inondable")
        # nom = name sinon ref de la voie
        self.assertEqual(by_id["way/101"]["nom"], "D6110")
        self.assertEqual(by_id["way/100"]["nom"], "Pont Vieux")

    def test_missing_position_is_explanatory_string(self):
        self._mock()
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        blanks = [o for o in out["accessibilite"]["ouvrages_a_risque"]
                  if isinstance(o["lat"], str)]
        self.assertEqual(len(blanks), 1)                      # le pont sans center
        self.assertEqual(blanks[0]["osm_id"], "way/104")
        self.assertIsInstance(blanks[0]["distance_km"], str)
        self.assertIn("indisponible", blanks[0]["lat"])
        self.assertIn("indisponible", blanks[0]["distance_km"])
        self.assertEqual(code, 0)

    def test_sorted_by_distance_string_last(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        liste = out["accessibilite"]["ouvrages_a_risque"]
        dists = [o["distance_km"] for o in liste if isinstance(o["distance_km"], (int, float))]
        self.assertEqual(dists, sorted(dists))
        self.assertIsInstance(liste[-1]["distance_km"], str)  # position absente rejetée en fin

    def test_relevant_tags_only(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        for o in out["accessibilite"]["ouvrages_a_risque"]:
            self.assertTrue(set(o["tags"]).issubset(set(main.RELEVANT_TAGS)), o["tags"])

    def test_limit_truncates_list_not_resume(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès", "--limit", "2"])
        validate(out, SCHEMA)
        self.assertEqual(len(out["accessibilite"]["ouvrages_a_risque"]), 2)
        self.assertEqual(out["accessibilite"]["resume"]["ouvrages_total"], 6)  # compte tout

    def test_default_has_no_geometry_key(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        for o in out["accessibilite"]["ouvrages_a_risque"]:
            self.assertNotIn("geometry", o)

    def test_geometry_flag_adds_tracks(self):
        geom = {"elements": [{
            "type": "way", "id": 200,
            "geometry": [{"lat": 44.130, "lon": 4.080}, {"lat": 44.131, "lon": 4.081}],
            "tags": {"highway": "residential", "bridge": "yes"}}]}
        self._mock(data=geom)
        out, _ = self._run(["--commune", "Alès", "--geometry"])
        validate(out, SCHEMA)
        o = out["accessibilite"]["ouvrages_a_risque"][0]
        self.assertIn("geometry", o)
        self.assertEqual(len(o["geometry"]), 2)
        # point représentatif = centroïde de la géométrie -> distance numérique
        self.assertIsInstance(o["distance_km"], (int, float))

    def test_empty_sector_is_valid_empty(self):
        self._mock(data={"elements": []})
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["accessibilite"]["resume"]["ouvrages_total"], 0)
        self.assertEqual(out["accessibilite"]["ouvrages_a_risque"], [])

    # --- erreurs contrôlées ---------------------------------------------------
    def test_radius_out_of_bounds_raises(self):
        self._mock()
        with self.assertRaises(SkillError):
            self._run(["--commune", "Alès", "--radius-m", "6000"])
        with self.assertRaises(SkillError):
            self._run(["--commune", "Alès", "--radius-m", "0"])

    def test_overpass_failure_becomes_error_variant(self):
        self._mock()

        def boom(ql, timeout):
            raise SkillError("Overpass indisponible (serveur principal et miroir)", detail="test")
        main.overpass_query = boom
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)                          # la variante {error} reste conforme
        self.assertIn("error", out["accessibilite"])
        self.assertEqual(code, 1)

    # --- unités ---------------------------------------------------------------
    def test_classify_unit(self):
        self.assertEqual(main.classify({"ford": "yes"}), "gué")
        self.assertEqual(main.classify({"ford": "no"}), None)
        self.assertEqual(main.classify({"tunnel": "culvert"}), "tunnel")
        self.assertEqual(main.classify({"bridge": "viaduct"}), "pont")
        self.assertEqual(main.classify({"layer": "-2"}), "passage_inférieur")
        self.assertEqual(main.classify({"layer": "2"}), None)
        self.assertEqual(main.classify({"hazard": "flooding"}), "zone_inondable")
        self.assertEqual(main.classify({"highway": "residential"}), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
