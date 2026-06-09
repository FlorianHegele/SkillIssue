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
        self._orig = {k: getattr(main, k)
                      for k in ("overpass_query", "resolve_location", "http_get_json")}

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

    def test_no_geometry_key_in_output(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        for o in out["accessibilite"]["ouvrages_a_risque"]:
            self.assertNotIn("geometry", o)   # tracé complet jamais exposé (récupérable via osm_id)

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

    def test_negative_limit_raises(self):
        self._mock()
        with self.assertRaises(SkillError):
            self._run(["--commune", "Alès", "--limit", "-1"])

    def test_limit_zero_is_summary_only(self):
        self._mock()
        out, code = self._run(["--commune", "Alès", "--limit", "0"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["accessibilite"]["ouvrages_a_risque"], [])
        self.assertEqual(out["accessibilite"]["resume"]["ouvrages_total"], 6)  # résumé complet

    def test_overpass_failure_becomes_error_variant(self):
        self._mock()

        def boom(ql, timeout):
            raise SkillError("Overpass indisponible (serveur principal et miroir)", detail="test")
        main.overpass_query = boom
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)                          # la variante {error} reste conforme
        self.assertIn("error", out["accessibilite"])
        self.assertEqual(code, 1)

    # --- remark Overpass : timeout/OOM serveur rendu en 200 != secteur vide -------
    def test_remark_with_error_raises_not_empty_sector(self):
        """Un `remark` d'erreur (réponse 200 tronquée) doit lever, PAS être lu comme 0 ouvrage."""
        with self.assertRaises(SkillError):
            main._check_overpass_remark({"elements": [], "remark": "runtime error: Query timed out"})

    def test_remark_benign_passes_through(self):
        """Un `remark` informatif sans mot d'erreur ne doit PAS bloquer une réponse valide."""
        data = {"elements": [], "remark": "improve performance by ..."}
        self.assertIs(main._check_overpass_remark(data), data)

    def test_remark_error_raises_not_empty_sector(self):
        """Primaire ET miroir renvoient un remark d'erreur -> overpass_query lève (PAS un secteur
        vide), et de bout en bout la variante {error} reste conforme."""
        timed_out = {"elements": [], "remark": "runtime error: Query timed out in 'recurse'"}
        main.http_get_json = lambda url, params=None, timeout=20, **kw: timed_out
        with self.assertRaises(SkillError):
            main.overpass_query("[out:json];", 25)
        main.resolve_location = lambda c, lat, lon, t: ALES
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        self.assertIn("error", out["accessibilite"])
        self.assertEqual(code, 1)

    def test_mirror_default_used_on_saturation_and_overridable(self):
        """Sur SATURATION (504) du primaire, bascule sur le miroir par défaut (OSM France) ;
        FLOOD_OVERPASS_MIRROR le surcharge, et une valeur vide DÉSACTIVE le repli (primaire seul)."""
        ok = {"elements": []}
        calls = []

        def fake(url, params=None, timeout=20, **kw):
            calls.append(url)
            if main.OVERPASS_PRIMARY in url:
                raise SkillError("échec de l'appel", detail="HTTP 504", status=504)
            return ok

        main.http_get_json = fake
        # 1) défaut : 504 primaire (transitoire) -> bascule sur le miroir OSM France
        os.environ.pop("FLOOD_OVERPASS_MIRROR", None)
        self.assertEqual(main.overpass_query("[out:json];", 25), ok)
        self.assertEqual(calls, [main.OVERPASS_PRIMARY, main.OVERPASS_MIRROR])
        # 2) repli désactivé (chaîne vide) : primaire seul -> lève
        calls.clear()
        os.environ["FLOOD_OVERPASS_MIRROR"] = ""
        try:
            with self.assertRaises(SkillError):
                main.overpass_query("[out:json];", 25)
            self.assertEqual(calls, [main.OVERPASS_PRIMARY])
        finally:
            del os.environ["FLOOD_OVERPASS_MIRROR"]

    def test_query_timeout_skips_mirror(self):
        """Un primaire en TIMEOUT DE CALCUL (remark « Query timed out ») ne tente PAS le miroir
        (qui exécuterait la même requête lourde) : échec direct, miroir non appelé."""
        timed_out = {"elements": [], "remark": "runtime error: Query timed out in 'query'"}
        calls = []

        def fake(url, params=None, timeout=20, **kw):
            calls.append(url)
            return timed_out

        main.http_get_json = fake
        os.environ.pop("FLOOD_OVERPASS_MIRROR", None)
        with self.assertRaises(SkillError):
            main.overpass_query("[out:json];", 25)
        self.assertEqual(calls, [main.OVERPASS_PRIMARY])   # miroir sauté (inutile sur requête lourde)

    # --- QL Overpass : verrouille les filtres (sinon ils ne vivent qu'en live) ----
    def test_query_excludes_pedestrian_and_scopes(self):
        ql = main.build_query(44.13, 4.08, 1200, 25)
        excl = '["highway"!~"^(footway|steps|path|cycleway|pedestrian|bridleway|corridor)$"]'
        self.assertIn(excl, ql)                       # exclusion piétonne posée
        self.assertIn('["layer"~"^-"]', ql)           # points bas seulement (layer négatif)
        # bridge/tunnel/layer exigent une voie carrossable PRÉSENTE (évite ponts ferroviaires…)
        self.assertIn('way ["highway"]%s["bridge"]["bridge"!="no"]' % excl, ql)
        self.assertIn('way ["highway"]%s["tunnel"]["tunnel"!="no"]' % excl, ql)
        # flood_prone/hazard restreints aux voies (pas de polygones de zone)
        self.assertIn('way ["highway"]%s["flood_prone"="yes"]' % excl, ql)
        self.assertIn('way ["highway"]%s["hazard"="flooding"]' % excl, ql)
        # le gué-node reste sans filtre highway (un gué peut ne pas porter highway)
        self.assertIn('node["ford"]["ford"!="no"](around:', ql)
        # around: sur CHAQUE sous-requête -> jamais de scan national (7 sous-requêtes)
        self.assertEqual(ql.count("around:"), 7)

    def test_query_uses_center_out_statement(self):
        ql = main.build_query(44.13, 4.08, 1200, 25)
        self.assertIn("out tags center;", ql)   # point représentatif + tags, léger
        self.assertNotIn("out geom;", ql)        # le tracé complet n'est jamais demandé

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
