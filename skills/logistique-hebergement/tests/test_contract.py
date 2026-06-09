# -*- coding: utf-8 -*-
"""Tests hors-ligne du contrat de sortie de logistique-hebergement.

On remplace l'accès réseau (Overpass) et le géocodage par des mocks, et on valide la sortie
contre contract.schema.json. Aucun réseau. Lançable seul :
    python tests/test_contract.py   (ou via pytest).

Fixture `fixtures/overpass_ales.json` = réponse Overpass réduite (secteur Alès) :
  - node hotel rooms=50               -> hôtel, capacité estimée rooms×2 = 100
  - node hotel beds=80                -> hôtel, capacité OSM = 80
  - node hotel stars=4                -> hôtel, capacité estimée défaut 4★ = 70 chambres × 2 = 140
  - node hotel (nu)                   -> hôtel, capacité indisponible
  - way  sports_centre + geometry     -> gymnase, capacité estimée par surface
  - way  school + geometry            -> école, capacité estimée par surface
  - node community_centre (sans geom) -> salle_communale, capacité indisponible (pas de surface)
  - way  school SANS position         -> école, position absente (mesure -> chaîne), capacité indispo
  - node tourism=museum               -> IGNORÉ
Décompte attendu : 8 sites (4 hôtels, 1 gymnase, 2 écoles, 1 salle communale).
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> pour _common
sys.path.insert(0, SKILL_DIR)                   # logistique-hebergement/ -> pour main, contract

import main  # noqa: E402
from _common import Lieu, SkillError, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
FIXTURE = os.path.join(HERE, "fixtures", "overpass_ales.json")

with open(FIXTURE, encoding="utf-8") as _fh:
    OVERPASS_ALES = json.load(_fh)

# Centre d'Alès (proche des sites de la fixture).
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
        r = out["hebergement"]["resume"]
        self.assertEqual(r["sites_total"], 8)
        self.assertEqual(r["hotels"], 4)
        self.assertEqual(r["gymnases"], 1)
        self.assertEqual(r["ecoles"], 2)
        self.assertEqual(r["salles_communales"], 1)
        self.assertEqual(len(out["hebergement"]["sites"]), 8)
        self.assertEqual(out["hebergement"]["rayon_m"], main.DEFAULT_RADIUS_M)
        self.assertEqual(out["lieu"]["code_insee"], "30007")

    def test_non_shelter_is_ignored(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        ids = [s["osm_id"] for s in out["hebergement"]["sites"]]
        self.assertNotIn("node/9001", ids)   # tourism=museum -> écarté

    def test_type_classification(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        by_id = {s["osm_id"]: s for s in out["hebergement"]["sites"]}
        self.assertEqual(by_id["node/2001"]["type"], "hôtel")
        self.assertEqual(by_id["way/3001"]["type"], "gymnase")
        self.assertEqual(by_id["way/3002"]["type"], "école")
        self.assertEqual(by_id["node/3003"]["type"], "salle_communale")

    # --- capacité : source + méthode par branche ------------------------------
    def test_capacity_sources_and_methods(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        by_id = {s["osm_id"]: s for s in out["hebergement"]["sites"]}

        rooms = by_id["node/2001"]                       # rooms=50 -> 100, estimée
        self.assertEqual(rooms["capacite"], 100)
        self.assertEqual(rooms["capacite_source"], "estimee")
        self.assertIn("rooms×2", rooms["capacite_methode"])

        beds = by_id["node/2002"]                         # beds=80 -> OSM
        self.assertEqual(beds["capacite"], 80)
        self.assertEqual(beds["capacite_source"], "osm")

        stars = by_id["node/2003"]                        # 4★ -> 70 chambres × 2 = 140, estimée
        self.assertEqual(stars["capacite"], 140)
        self.assertEqual(stars["capacite_source"], "estimee")

        nu = by_id["node/2004"]                           # rien -> indisponible
        self.assertIsInstance(nu["capacite"], str)
        self.assertEqual(nu["capacite_source"], "indisponible")

        gym = by_id["way/3001"]                           # surface -> estimée
        self.assertIsInstance(gym["capacite"], (int, float))
        self.assertEqual(gym["capacite_source"], "estimee")
        self.assertIsInstance(gym["surface_m2"], (int, float))
        self.assertIn("surface", gym["capacite_methode"])

    def test_resume_aggregates_capacity(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        r = out["hebergement"]["resume"]
        # fiable = hôtels seuls (rooms 100 + beds 80 + 4★ 140) ; gymnase + école = majorants parcelle.
        self.assertEqual(r["sites_sans_capacite"], 3)
        self.assertEqual(r["capacite_fiable_totale"], 100 + 80 + 140)
        self.assertEqual(r["sites_capacite_majorant"], 2)             # gymnase + école (parcelles)
        self.assertGreater(r["capacite_majorant_parcelles"], 0)

    def test_missing_position_is_explanatory_string(self):
        self._mock()
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        blanks = [s for s in out["hebergement"]["sites"] if isinstance(s["lat"], str)]
        self.assertEqual(len(blanks), 1)                  # l'école way/3004 sans position
        self.assertEqual(blanks[0]["osm_id"], "way/3004")
        self.assertIsInstance(blanks[0]["distance_km"], str)
        self.assertIn("indisponible", blanks[0]["lat"])
        self.assertEqual(code, 0)

    def test_sorted_by_capacity_desc_string_last(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        liste = out["hebergement"]["sites"]
        caps = [s["capacite"] for s in liste if isinstance(s["capacite"], (int, float))]
        self.assertEqual(caps, sorted(caps, reverse=True))         # décroissant
        self.assertIsInstance(liste[-1]["capacite"], str)          # indisponibles en fin

    def test_relevant_tags_only(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        for s in out["hebergement"]["sites"]:
            self.assertTrue(set(s["tags"]).issubset(set(main.RELEVANT_TAGS)), s["tags"])

    def test_limit_truncates_list_not_resume(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès", "--limit", "3"])
        validate(out, SCHEMA)
        self.assertEqual(len(out["hebergement"]["sites"]), 3)
        self.assertEqual(out["hebergement"]["resume"]["sites_total"], 8)        # compte tout
        self.assertEqual(out["hebergement"]["resume"]["sites_sans_capacite"], 3)

    def test_default_has_no_geometry_key(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès"])
        for s in out["hebergement"]["sites"]:
            self.assertNotIn("geometry", s)

    def test_geometry_flag_adds_tracks(self):
        self._mock()
        out, _ = self._run(["--commune", "Alès", "--geometry"])
        validate(out, SCHEMA)
        gym = next(s for s in out["hebergement"]["sites"] if s["osm_id"] == "way/3001")
        self.assertIn("geometry", gym)
        self.assertEqual(len(gym["geometry"]), 5)

    def test_empty_sector_is_valid_empty(self):
        self._mock(data={"elements": []})
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["hebergement"]["resume"]["sites_total"], 0)
        self.assertEqual(out["hebergement"]["sites"], [])

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
        self.assertEqual(out["hebergement"]["sites"], [])
        self.assertEqual(out["hebergement"]["resume"]["sites_total"], 8)        # résumé complet

    def test_overpass_failure_becomes_error_variant(self):
        self._mock()

        def boom(ql, timeout):
            raise SkillError("Overpass indisponible (serveur principal et miroir)", detail="test")
        main.overpass_query = boom
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)                          # la variante {error} reste conforme
        self.assertIn("error", out["hebergement"])
        self.assertEqual(code, 1)

    # --- remark Overpass : timeout/OOM serveur rendu en 200 != secteur vide -------
    def test_remark_with_error_raises_not_empty_sector(self):
        """Un `remark` d'erreur (réponse 200 tronquée) doit lever, PAS être lu comme 0 site."""
        with self.assertRaises(SkillError):
            main._check_overpass_remark({"elements": [], "remark": "runtime error: Query timed out"})

    def test_remark_benign_passes_through(self):
        """Un `remark` informatif sans mot d'erreur ne doit PAS bloquer une réponse valide."""
        data = {"elements": [], "remark": "improve performance by ..."}
        self.assertIs(main._check_overpass_remark(data), data)

    def test_remark_without_mirror_raises_not_empty_sector(self):
        """Sans miroir configuré, un remark d'erreur du primaire -> overpass_query lève
        (PAS un secteur vide), et de bout en bout la variante {error} reste conforme."""
        timed_out = {"elements": [], "remark": "runtime error: Query timed out in 'recurse'"}
        main.http_get_json = lambda url, params=None, timeout=20, **kw: timed_out
        with self.assertRaises(SkillError):
            main.overpass_query("[out:json];", 25)
        main.resolve_location = lambda c, lat, lon, t: ALES
        out, code = self._run(["--commune", "Alès"])
        validate(out, SCHEMA)
        self.assertIn("error", out["hebergement"])

    def test_mirror_used_only_when_configured(self):
        """Avec FLOOD_OVERPASS_MIRROR, l'échec du primaire bascule sur le miroir ; les deux en
        échec -> lève. Sans la variable, le miroir n'est jamais appelé."""
        timed_out = {"elements": [], "remark": "runtime error: Query timed out"}
        ok = {"elements": []}
        calls = []

        def fake(url, params=None, timeout=20, **kw):
            calls.append(url)
            return timed_out if main.OVERPASS_PRIMARY in url else ok

        main.http_get_json = fake
        os.environ["FLOOD_OVERPASS_MIRROR"] = "https://example.org/api/interpreter"
        try:
            data = main.overpass_query("[out:json];", 25)
            self.assertEqual(data, ok)                       # repli miroir réussi
            self.assertEqual(len(calls), 2)                  # primaire puis miroir
        finally:
            del os.environ["FLOOD_OVERPASS_MIRROR"]
        calls.clear()
        with self.assertRaises(SkillError):                  # sans env : pas de miroir
            main.overpass_query("[out:json];", 25)
        self.assertEqual(calls, [main.OVERPASS_PRIMARY])     # primaire seul

    # --- QL Overpass : verrouille la forme (sinon elle ne vit qu'en live) -----
    def test_query_scopes_and_out_geom(self):
        ql = main.build_query(44.13, 4.08, 2000, 25)
        self.assertIn("out geom;", ql)                  # géométrie nécessaire au calcul de surface
        self.assertIn('nwr["tourism"="hotel"]', ql)
        self.assertIn('nwr["leisure"="sports_centre"]', ql)
        self.assertIn('nwr["amenity"="school"]', ql)
        self.assertIn('nwr["amenity"="community_centre"]', ql)
        # around: sur CHAQUE sous-requête -> jamais de scan national (6 sous-requêtes).
        self.assertEqual(ql.count("around:"), 6)

    # --- unités ---------------------------------------------------------------
    def test_classify_unit(self):
        self.assertEqual(main.classify({"tourism": "hotel"}), "hôtel")
        self.assertEqual(main.classify({"leisure": "sports_centre"}), "gymnase")
        self.assertEqual(main.classify({"building": "sports_hall"}), "gymnase")
        self.assertEqual(main.classify({"amenity": "school"}), "école")
        self.assertEqual(main.classify({"amenity": "community_centre"}), "salle_communale")
        self.assertIsNone(main.classify({"tourism": "museum"}))

    def test_footprint_m2_known_square(self):
        # Carré 0.001° × 0.001° à l'équateur : côté ≈ 111.32 m -> aire ≈ 12392 m².
        square = [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.001},
                  {"lat": 0.001, "lon": 0.001}, {"lat": 0.001, "lon": 0.0}]
        area = main.footprint_m2(square)
        self.assertIsInstance(area, (int, float))
        self.assertAlmostEqual(area, 111.32 ** 2, delta=5.0)

    def test_footprint_m2_missing_is_string(self):
        self.assertIsInstance(main.footprint_m2(None), str)
        self.assertIsInstance(main.footprint_m2([{"lat": 1.0, "lon": 1.0}]), str)

    def test_surface_of_relation_multipolygon(self):
        # Relation `out geom` : la géométrie est dans les membres (outer - inner).
        ring = lambda d: [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": d},
                          {"lat": d, "lon": d}, {"lat": d, "lon": 0.0}, {"lat": 0.0, "lon": 0.0}]
        rel = {"type": "relation", "members": [
            {"type": "way", "role": "outer", "geometry": ring(0.002)},   # grand carré
            {"type": "way", "role": "inner", "geometry": ring(0.001)},   # trou
        ]}
        area = main.surface_of(rel)
        self.assertIsInstance(area, (int, float))
        outer = main.footprint_m2(ring(0.002))
        inner = main.footprint_m2(ring(0.001))
        self.assertAlmostEqual(area, outer - inner, delta=1.0)
        # Sans membres surfaciques -> chaîne.
        self.assertIsInstance(main.surface_of({"type": "relation", "members": []}), str)
        self.assertIsInstance(main.surface_of({"type": "node", "lat": 1.0, "lon": 1.0}), str)

    def test_hotel_bare_capacity_is_estimee_not_osm(self):
        # `capacity` nu sur un hôtel est ambigu -> estimee, pas osm.
        cap, src, methode = main.estimate_capacity("hôtel", {"capacity": "120"}, "x")
        self.assertEqual((cap, src), (120.0, "estimee"))
        self.assertIn("ambigu", methode)
        # rooms prime sur capacity nu.
        cap, src, _ = main.estimate_capacity("hôtel", {"rooms": "10", "capacity": "999"}, "x")
        self.assertEqual((cap, src), (20.0, "estimee"))

    def test_estimate_capacity_branches(self):
        cap, src, _ = main.estimate_capacity("hôtel", {"rooms": "50"}, "x")
        self.assertEqual((cap, src), (100.0, "estimee"))
        cap, src, _ = main.estimate_capacity("hôtel", {"beds": "80"}, "x")
        self.assertEqual((cap, src), (80.0, "osm"))
        # parcelle (sans building) : majorant étiqueté.
        cap, src, methode = main.estimate_capacity("gymnase", {}, 1200.0)
        self.assertEqual((cap, src), (300.0, "estimee"))
        self.assertIn("parcelle", methode)
        # empreinte bâtie : méthode distincte.
        cap, src, methode = main.estimate_capacity("gymnase", {"building": "sports_hall"}, 1200.0)
        self.assertEqual((cap, src), (300.0, "estimee"))
        self.assertIn("bâtie", methode)
        cap, src, _ = main.estimate_capacity("gymnase", {}, "indisponible : …")
        self.assertEqual(src, "indisponible")
        self.assertIsInstance(cap, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
