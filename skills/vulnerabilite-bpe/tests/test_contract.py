# -*- coding: utf-8 -*-
"""Tests hors-ligne du contrat de sortie de vulnerabilite-bpe.

On remplace les accès réseau / fichier par des mocks (registre, géocodage, CSV en cache) et on
valide la sortie contre contract.schema.json. Aucun réseau. Lançable seul :
    python tests/test_contract.py   (ou via pytest).

Fixture `fixtures/sample_bpe.csv` = vraies lignes BPE 2024 d'Alès (30007) + une de Montpellier
(34172, doit être filtrée) + des types non ciblés (C109, D101 : inclus seulement via --all-types ;
A504 : jamais), avec une C108 dont les coordonnées ont été vidées (mesure manquante). Plus une
commune fictive 30258 ne portant QU'un équipement non ciblé (A504) : sert à vérifier le cas
« commune présente dans la BPE mais sans équipement ciblé » (réponse vide légitime, code 0).
Décompte attendu — défaut : 6 écoles / 3 santé ; --all-types : 7 écoles / 4 santé.
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
sys.path.insert(0, SKILL_DIR)                   # vulnerabilite-bpe/ -> pour main, contract

import main  # noqa: E402
from _common import Lieu, SkillError, dataset as ds, validate  # noqa: E402

FIXTURE_CSV = os.path.join(HERE, "fixtures", "sample_bpe.csv")
SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")

ENTRY = {"millesime": 2024, "geographie": 2024, "min_skill_version": "1.0.0",
         "files": [{"zone": "france", "url": "https://example.test/bpe24.zip"}]}
INFO = {"registre_source": "local", "registry_version": 1,
        "maj_skill_disponible": False, "message": None}

# Centre d'Alès (proche des équipements réels de la fixture).
ALES = Lieu(commune="Alès", code_insee="30007", lat=44.128, lon=4.081)


class ContractTest(unittest.TestCase):
    def setUp(self):
        # resolve_source/dataset_path/… sont des wrappers du skill ; http_* vivent désormais
        # dans le socle _common.dataset, c'est là qu'on les mocke et qu'on les restaure.
        self._orig_main = {k: getattr(main, k) for k in
                           ("resolve_source", "resolve_location", "reverse_commune",
                            "dataset_path")}
        self._orig_ds = {k: getattr(ds, k) for k in ("http_get_json", "http_download")}

    def tearDown(self):
        for k, v in self._orig_main.items():
            setattr(main, k, v)
        for k, v in self._orig_ds.items():
            setattr(ds, k, v)

    def _mock_all(self, loc=ALES, meta=None):
        meta = meta or {"zone": "france", "url": ENTRY["files"][0]["url"],
                        "urlhash": "deadbeef", "telecharge_le": "2026-06-06T10:00:00+02:00",
                        "sha256": "deadbeef", "depuis_cache": False}
        main.resolve_source = lambda cache_dir, timeout: (dict(ENTRY), dict(INFO))
        main.resolve_location = lambda c, lat, lon, t: loc
        main.dataset_path = lambda entry, file_entry, c, r, t: (FIXTURE_CSV, dict(meta))

    def _run(self, argv):
        return main.run(main.build_parser().parse_args(argv))

    # --- conformité + valeurs stables ----------------------------------------
    def test_output_conforms_and_stable_counts(self):
        self._mock_all()
        out, code = self._run(["--commune", "30007"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        com = out["vulnerabilite"]["commune"]
        self.assertEqual(com["ecoles_count"], 6)   # C107x2, C108x2, C201x1, C301x1
        self.assertEqual(com["sante_count"], 3)    # D106, D108, D109
        self.assertEqual(com["code"], "30007")
        self.assertEqual(com["nom"], "Alès")
        self.assertEqual(len(out["vulnerabilite"]["ecoles"]), 6)
        self.assertEqual(len(out["vulnerabilite"]["sante"]), 3)
        # le nom de l'établissement (NOMRS) est exposé quand renseigné
        noms = [e["nom"] for e in out["vulnerabilite"]["ecoles"] if e["nom"]]
        self.assertTrue(noms, "au moins une école devrait porter un nom (NOMRS)")
        self.assertEqual(out["dataset"]["zone"], "france")
        self.assertEqual(out["dataset"]["millesime"], 2024)

    def test_depcom_filter_excludes_other_communes(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007"])
        eq = out["vulnerabilite"]["ecoles"] + out["vulnerabilite"]["sante"]
        # La ligne de Montpellier (34172, C107) ne doit jamais apparaître : on la repère par
        # une distance numérique très grande (Montpellier est à ~60 km d'Alès) — mais surtout
        # le compte d'écoles (6) l'exclut déjà. Vérifions qu'aucune école n'est anormalement loin.
        dists = [e["distance_km"] for e in out["vulnerabilite"]["ecoles"]
                 if isinstance(e["distance_km"], (int, float))]
        self.assertTrue(all(d < 30 for d in dists), dists)

    def test_default_excludes_non_targeted_types(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007"])
        codes = [e["type_code"] for e in
                 out["vulnerabilite"]["ecoles"] + out["vulnerabilite"]["sante"]]
        self.assertNotIn("C109", codes)   # domaine C mais non ciblé
        self.assertNotIn("D101", codes)   # domaine D mais hors D106–D113
        self.assertNotIn("A504", codes)   # hors domaines C/D

    def test_all_types_broadens(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007", "--all-types"])
        validate(out, SCHEMA)
        com = out["vulnerabilite"]["commune"]
        self.assertEqual(com["ecoles_count"], 7)   # + C109
        self.assertEqual(com["sante_count"], 4)    # + D101
        codes = [e["type_code"] for e in
                 out["vulnerabilite"]["ecoles"] + out["vulnerabilite"]["sante"]]
        self.assertIn("C109", codes)
        self.assertIn("D101", codes)
        self.assertNotIn("A504", codes)            # A reste hors domaines C/D

    def test_missing_coords_is_explanatory_string(self):
        self._mock_all()
        out, code = self._run(["--commune", "30007"])
        validate(out, SCHEMA)
        # une C108 de la fixture a ses coordonnées vidées
        blanks = [e for e in out["vulnerabilite"]["ecoles"]
                  if isinstance(e["lat"], str)]
        self.assertEqual(len(blanks), 1)
        self.assertIsInstance(blanks[0]["distance_km"], str)
        self.assertIn("indisponible", blanks[0]["lat"])
        self.assertIn("indisponible", blanks[0]["distance_km"])
        self.assertEqual(code, 0)

    def test_sorted_by_distance(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007"])
        for liste in ("ecoles", "sante"):
            dists = [e["distance_km"] for e in out["vulnerabilite"][liste]
                     if isinstance(e["distance_km"], (int, float))]
            self.assertEqual(dists, sorted(dists), liste)
        # l'équipement sans coordonnées (distance non numérique) est rejeté en fin de liste
        last = out["vulnerabilite"]["ecoles"][-1]
        self.assertIsInstance(last["distance_km"], str)

    def test_radius_filters_by_distance(self):
        self._mock_all()
        full, _ = self._run(["--commune", "30007"])
        tight, _ = self._run(["--commune", "30007", "--radius", "1"])
        validate(tight, SCHEMA)
        n_full = len([e for e in full["vulnerabilite"]["ecoles"]
                      if isinstance(e["distance_km"], (int, float))])
        n_tight = len([e for e in tight["vulnerabilite"]["ecoles"]
                       if isinstance(e["distance_km"], (int, float))])
        self.assertLessEqual(n_tight, n_full)
        # aucun équipement retenu au-delà du rayon
        for e in tight["vulnerabilite"]["ecoles"] + tight["vulnerabilite"]["sante"]:
            if isinstance(e["distance_km"], (int, float)):
                self.assertLessEqual(e["distance_km"], 1)

    def test_lat_lon_triggers_reverse_geocode(self):
        self._mock_all(loc=Lieu(commune=None, code_insee=None, lat=44.128, lon=4.081))
        main.reverse_commune = lambda lat, lon, t: ALES    # coords -> commune
        out, code = self._run(["--lat", "44.128", "--lon", "4.081"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["vulnerabilite"]["commune"]["code"], "30007")

    def test_reverse_geocode_without_insee_code_raises_clear_error(self):
        # geo.api renvoie un point sans code commune : on veut l'erreur « géocodage incomplet »
        # (cause réelle), pas le message trompeur « commune absente de la BPE ».
        self._mock_all(loc=Lieu(commune=None, code_insee=None, lat=44.128, lon=4.081))
        main.reverse_commune = lambda lat, lon, t: Lieu(commune="Ω", code_insee=None,
                                                        lat=lat, lon=lon)
        with self.assertRaises(SkillError) as ctx:
            self._run(["--lat", "44.128", "--lon", "4.081"])
        self.assertIn("géocodage incomplet", ctx.exception.message)

    def test_commune_present_without_targeted_equipment(self):
        # Commune bien présente dans la BPE (30258) mais ne portant qu'un type non ciblé (A504) :
        # réponse VALIDE à listes vides, code 0 — surtout pas l'erreur « absente de la BPE ».
        self._mock_all(loc=Lieu(commune="Saint-Exemple", code_insee="30258",
                                lat=44.139, lon=4.10))
        out, code = self._run(["--commune", "30258"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertNotIn("error", out["vulnerabilite"])
        com = out["vulnerabilite"]["commune"]
        self.assertEqual(com["ecoles_count"], 0)
        self.assertEqual(com["sante_count"], 0)
        self.assertEqual(out["vulnerabilite"]["ecoles"], [])
        self.assertEqual(out["vulnerabilite"]["sante"], [])
        self.assertIsNone(out["vulnerabilite"]["note"])

    def test_top_truncates_lists_without_silent_loss(self):
        self._mock_all()
        # Alès a 6 écoles ; --top 2 ne garde que les 2 plus proches mais le compteur reste 6.
        out, code = self._run(["--commune", "30007", "--top", "2"])
        validate(out, SCHEMA)
        self.assertEqual(code, 0)
        self.assertEqual(out["vulnerabilite"]["commune"]["ecoles_count"], 6)   # total préservé
        self.assertEqual(len(out["vulnerabilite"]["ecoles"]), 2)               # liste tronquée
        self.assertIsInstance(out["vulnerabilite"]["note"], str)               # coupe explicite
        self.assertIn("6", out["vulnerabilite"]["note"])
        # les 2 retenus sont bien les plus proches (distances triées croissantes)
        dists = [e["distance_km"] for e in out["vulnerabilite"]["ecoles"]]
        self.assertEqual(dists, sorted(dists))

    def test_top_zero_means_unlimited(self):
        self._mock_all()
        out, _ = self._run(["--commune", "30007", "--top", "0"])
        self.assertEqual(len(out["vulnerabilite"]["ecoles"]), 6)
        self.assertIsNone(out["vulnerabilite"]["note"])

    def test_commune_absent_from_bpe_returns_error_variant(self):
        self._mock_all(loc=Lieu(commune="Nulle part", code_insee="99999", lat=1.0, lon=1.0))
        out, code = self._run(["--commune", "99999"])
        validate(out, SCHEMA)                                # la variante {error} reste conforme
        self.assertIn("error", out["vulnerabilite"])
        self.assertEqual(code, 1)

    def test_libelle_fallback_on_unknown_code(self):
        # Les codes TYPEQU évoluent par millésime : un code inconnu ne doit jamais planter,
        # mais retomber sur un libellé de repli par domaine (C = enseignement, D = santé).
        self.assertEqual(main._libelle("C107"), "École maternelle")        # connu
        self.assertEqual(main._libelle("C999"), "Équipement d'enseignement (C999)")
        self.assertEqual(main._libelle("D999"), "Équipement de santé / action sociale (D999)")
        self.assertEqual(main._libelle("Z999"), "Z999")                    # hors domaines C/D

    def test_select_files(self):
        self.assertEqual([f["zone"] for f in main.select_files(ENTRY, "auto")], ["france"])
        self.assertEqual([f["zone"] for f in main.select_files(ENTRY, "france")], ["france"])
        with self.assertRaises(SkillError):
            main.select_files(ENTRY, "zone_inexistante")

    # --- registre / versions --------------------------------------------------
    def test_registry_picks_latest_compatible_and_flags_update(self):
        reg = {"registry_version": 99, "entries": [
            {"millesime": 2024, "min_skill_version": "1.0.0",
             "files": [{"zone": "france", "url": "a"}]},
            {"millesime": 2099, "min_skill_version": "2.0.0",
             "files": [{"zone": "france", "url": "b"}]}]}
        ds.http_get_json = lambda url, params=None, timeout=20, retries=3, require_json=True: reg
        tmp = tempfile.mkdtemp()
        try:
            entry, info = main.resolve_source(tmp, 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(entry["millesime"], 2024)           # dernier COMPATIBLE
        self.assertTrue(info["maj_skill_disponible"])        # 2099 existe mais incompatible
        self.assertEqual(info["registre_source"], "github")

    def test_registry_no_compatible_raises(self):
        reg = {"registry_version": 99, "entries": [
            {"millesime": 2099, "min_skill_version": "9.0.0",
             "files": [{"zone": "france", "url": "c"}]}]}
        ds.http_get_json = lambda url, params=None, timeout=20, retries=3, require_json=True: reg
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(SkillError):
                main.resolve_source(tmp, 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_registry_entry_without_millesime_raises(self):
        # Garde-fou : une entrée sans millesime entier donnerait millesime:null en sortie
        # (violation du schéma). On veut une erreur contrôlée, détectée hors-ligne.
        reg = {"registry_version": 99, "entries": [
            {"min_skill_version": "1.0.0", "files": [{"zone": "france", "url": "a"}]}]}
        ds.http_get_json = lambda url, params=None, timeout=20, retries=3, require_json=True: reg
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(SkillError) as ctx:
                main.resolve_source(tmp, 10)
            self.assertIn("millesime", ctx.exception.message)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- cache par hash d'URL + extraction du zip -----------------------------
    def _make_zip(self, path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("BPE24.csv",
                        "DEPCOM;TYPEQU;NOMRS;LATITUDE;LONGITUDE;QUALITE_XY;TR_DIST_PRECISION\n"
                        "30007;C107;ECOLE TEST;44.13;4.08;B;< 100\n")

    def test_cache_by_urlhash_downloads_once(self):
        tmp = tempfile.mkdtemp()
        src_zip = os.path.join(tmp, "src.zip")
        self._make_zip(src_zip)
        calls = []

        def fake_dl(url, dest, timeout=60, retries=3, expect_content_type=None):
            calls.append(url)
            shutil.copyfile(src_zip, dest)
            return dest
        orig_dl = ds.http_download
        ds.http_download = fake_dl
        try:
            entry = dict(ENTRY)
            fe = {"zone": "france", "url": "https://example.test/a.zip"}
            p1, m1 = main.dataset_path(entry, fe, tmp, False, 10)
            p2, m2 = main.dataset_path(entry, fe, tmp, False, 10)
            self.assertEqual(len(calls), 1)                  # 2e appel : aucun téléchargement
            self.assertFalse(m1["depuis_cache"])
            self.assertTrue(m2["depuis_cache"])
            with open(p1, encoding="utf-8") as fh:           # c'est bien le CSV de données
                self.assertTrue(fh.readline().startswith("DEPCOM;TYPEQU"))
            fe2 = {"zone": "france", "url": "https://example.test/autre.zip"}
            main.dataset_path(entry, fe2, tmp, False, 10)
            self.assertEqual(len(calls), 2)                  # URL différente -> re-téléchargement
        finally:
            ds.http_download = orig_dl
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_failure_without_cache_raises_update_message(self):
        tmp = tempfile.mkdtemp()

        def fail_dl(url, dest, timeout=60, retries=3, expect_content_type=None):
            raise SkillError("réseau coupé", detail="test")
        orig_dl = ds.http_download
        ds.http_download = fail_dl
        try:
            with self.assertRaises(SkillError) as ctx:
                main.dataset_path(dict(ENTRY),
                                  {"zone": "france", "url": "https://example.test/x.zip"},
                                  tmp, False, 10)
            self.assertIn("mettre à jour le repo", ctx.exception.message)
        finally:
            ds.http_download = orig_dl
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
