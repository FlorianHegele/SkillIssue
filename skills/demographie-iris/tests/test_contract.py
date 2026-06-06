# -*- coding: utf-8 -*-
"""Tests hors-ligne du contrat de sortie de demographie-iris.

On remplace les accès réseau / fichier par des mocks (registre, géocodage, CSV en cache) et on
valide la sortie contre contract.schema.json. Aucun réseau. Lançable seul :
    python tests/test_contract.py   (ou via pytest).
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> pour _common
sys.path.insert(0, SKILL_DIR)                   # demographie-iris/ -> pour main, contract

import main  # noqa: E402
from _common import Lieu, SkillError, validate  # noqa: E402

FIXTURE_CSV = os.path.join(HERE, "fixtures", "sample_cfm.csv")
SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")

ENTRY = {"millesime": 2022, "geographie": 2024, "prefix": "C22_", "min_skill_version": "1.0.0",
         "url_metropole": "https://example.test/metro.zip",
         "url_com": "https://example.test/com.zip"}
INFO = {"registre_source": "local", "registry_version": 1,
        "maj_skill_disponible": False, "message": None}

ALES = Lieu(commune="Alès", code_insee="30007", lat=44.12, lon=4.08)


class ContractTest(unittest.TestCase):
    def setUp(self):
        self._orig = {k: getattr(main, k) for k in
                      ("resolve_source", "resolve_location", "reverse_commune",
                       "dataset_path", "http_get_json")}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(main, k, v)

    def _mock_all(self, loc=ALES, population=40000, pop_raises=False, meta=None):
        meta = meta or {"telecharge_le": "2026-06-06T10:00:00+02:00",
                        "sha256": "deadbeef", "depuis_cache": False}
        main.resolve_source = lambda cache_dir, timeout: (dict(ENTRY), dict(INFO))
        main.resolve_location = lambda c, lat, lon, t: loc
        main.dataset_path = lambda entry, zone, c, r, t: (FIXTURE_CSV, dict(meta))

        def fake_get(url, params=None, timeout=20, retries=3):
            if pop_raises:
                raise SkillError("geo.api en panne", detail="test")
            return [{"population": population}]
        main.http_get_json = fake_get

    def _run(self, argv):
        return main.run(main.build_parser().parse_args(argv))

    # --- conformité + valeurs stables ----------------------------------------
    def test_output_conforms_and_stable_values(self):
        self._mock_all()
        out, code = self._run(["--commune", "30007"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        com = out["demographie"]["commune"]
        self.assertEqual(com["iris_count"], 4)               # 4 IRIS d'Alès (pas Montpellier)
        self.assertEqual(com["menages_total"], 2820)         # 820+900+700+400
        self.assertEqual(com["familles_total"], 1570)        # 540+610+420 (Tamaris vide ignoré)
        self.assertEqual(com["monoparentales_total"], 350)   # 95+120+80+55
        self.assertEqual(com["part_monoparentales_pct"], 22.3)
        self.assertEqual(com["population"], 40000)
        self.assertEqual(len(out["demographie"]["iris"]), 4)
        # tri par population décroissante
        pops = [it["population"] for it in out["demographie"]["iris"]]
        self.assertEqual(pops, sorted(pops, reverse=True))
        # provenance
        self.assertEqual(out["dataset"]["zone"], "metropole")
        self.assertEqual(out["dataset"]["millesime"], 2022)

    def test_com_filter_excludes_other_communes(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007"])
        codes = [it["code"] for it in out["demographie"]["iris"]]
        self.assertTrue(all(c.startswith("30007") for c in codes), codes)
        self.assertNotIn("341090000", codes)

    def test_missing_measure_is_explanatory_string(self):
        self._mock_all()
        out, code = self._run(["--commune", "30007"])
        validate(out, SCHEMA)
        tamaris = [it for it in out["demographie"]["iris"] if it["code"] == "300070104"][0]
        self.assertIsInstance(tamaris["familles"], str)      # C22_FAM vide -> chaîne
        self.assertIn("indisponible", tamaris["familles"])
        self.assertIsInstance(tamaris["menages"], int)       # mesure présente -> nombre
        self.assertEqual(code, 0)

    def test_detail_adds_couples_fields(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007", "--detail"])
        validate(out, SCHEMA)
        it = out["demographie"]["iris"][0]
        self.assertIn("couples_avec_enfants", it)
        self.assertIn("couples_sans_enfants", it)
        self.assertIn("type_iris", it)

    def test_no_detail_omits_couples_fields(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007"])
        it = out["demographie"]["iris"][0]
        self.assertNotIn("couples_avec_enfants", it)
        self.assertNotIn("type_iris", it)

    def test_population_failure_is_graceful(self):
        self._mock_all(pop_raises=True)
        out, code = self._run(["--commune", "30007"])
        validate(out, SCHEMA)
        self.assertIsInstance(out["demographie"]["commune"]["population"], str)
        self.assertEqual(code, 0)                            # données IRIS toujours là

    def test_no_iris_for_commune_returns_error_variant(self):
        self._mock_all(loc=Lieu(commune="Nulle part", code_insee="99999", lat=1.0, lon=1.0))
        out, code = self._run(["--commune", "99999"])
        validate(out, SCHEMA)                                # la variante {error} reste conforme
        self.assertIn("error", out["demographie"])
        self.assertEqual(code, 1)

    def test_lat_lon_triggers_reverse_geocode(self):
        self._mock_all(loc=Lieu(commune=None, code_insee=None, lat=44.12, lon=4.08))
        main.reverse_commune = lambda lat, lon, t: ALES    # coords -> commune
        out, code = self._run(["--lat", "44.12", "--lon", "4.08"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["demographie"]["commune"]["code"], "30007")

    # --- zone -----------------------------------------------------------------
    def test_choose_zone(self):
        self.assertEqual(main.choose_zone("auto", "97501"), "com")
        self.assertEqual(main.choose_zone("auto", "30007"), "metropole")
        self.assertEqual(main.choose_zone("metropole", "97501"), "metropole")
        self.assertEqual(main.choose_zone("com", "30007"), "com")

    # --- registre / versions --------------------------------------------------
    def test_registry_picks_latest_compatible_and_flags_update(self):
        reg = {"registry_version": 99, "entries": [
            {"millesime": 2022, "prefix": "C22_", "min_skill_version": "1.0.0",
             "url_metropole": "a", "url_com": "b"},
            {"millesime": 2099, "prefix": "C99_", "min_skill_version": "2.0.0",
             "url_metropole": "c", "url_com": "d"}]}
        main.http_get_json = lambda url, params=None, timeout=20, retries=3: reg
        tmp = tempfile.mkdtemp()
        try:
            entry, info = main.resolve_source(tmp, 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(entry["millesime"], 2022)           # dernier COMPATIBLE
        self.assertTrue(info["maj_skill_disponible"])        # 2099 existe mais incompatible
        self.assertEqual(info["registre_source"], "github")

    def test_registry_no_compatible_raises(self):
        reg = {"registry_version": 99, "entries": [
            {"millesime": 2099, "prefix": "C99_", "min_skill_version": "9.0.0",
             "url_metropole": "c", "url_com": "d"}]}
        main.http_get_json = lambda url, params=None, timeout=20, retries=3: reg
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(SkillError):
                main.resolve_source(tmp, 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- cache par hash d'URL + extraction du zip -----------------------------
    def _make_zip(self, path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("base-ic-couples-familles-menages-2022.CSV",
                        "IRIS;COM;C22_MEN\n300070101;30007;820\n")
            zf.writestr("meta_base-ic-couples-familles-menages-2022.CSV",
                        "COD_VAR;LIB_VAR\nC22_MEN;Nombre de ménages\n")

    def test_cache_by_urlhash_downloads_once(self):
        tmp = tempfile.mkdtemp()
        src_zip = os.path.join(tmp, "src.zip")
        self._make_zip(src_zip)
        calls = []

        def fake_dl(url, dest, timeout=60, retries=3, expect_content_type=None):
            calls.append(url)
            shutil.copyfile(src_zip, dest)
            return dest
        orig_dl = main.http_download
        main.http_download = fake_dl
        try:
            entry = dict(ENTRY)
            p1, m1 = main.dataset_path(entry, "metropole", tmp, False, 10)
            p2, m2 = main.dataset_path(entry, "metropole", tmp, False, 10)
            self.assertEqual(len(calls), 1)                  # 2e appel : aucun téléchargement
            self.assertFalse(m1["depuis_cache"])
            self.assertTrue(m2["depuis_cache"])
            with open(p1, encoding="utf-8") as fh:           # c'est bien le CSV de données
                self.assertTrue(fh.readline().startswith("IRIS;COM"))
            # URL différente -> urlhash différent -> nouveau téléchargement
            entry2 = dict(ENTRY, url_metropole="https://example.test/autre.zip")
            main.dataset_path(entry2, "metropole", tmp, False, 10)
            self.assertEqual(len(calls), 2)
        finally:
            main.http_download = orig_dl
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_failure_without_cache_raises_update_message(self):
        tmp = tempfile.mkdtemp()

        def fail_dl(url, dest, timeout=60, retries=3, expect_content_type=None):
            raise SkillError("réseau coupé", detail="test")
        orig_dl = main.http_download
        main.http_download = fail_dl
        try:
            with self.assertRaises(SkillError) as ctx:
                main.dataset_path(dict(ENTRY), "metropole", tmp, False, 10)
            self.assertIn("mettre à jour le repo", ctx.exception.message)
        finally:
            main.http_download = orig_dl
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
