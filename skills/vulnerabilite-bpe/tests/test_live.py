# -*- coding: utf-8 -*-
"""Sondes LIVE (opt-in) : vérifient que les vraies sources parlent encore la forme attendue.

⚠ Désactivées par défaut (réseau, non déterministes, gros téléchargement ~165 Mo). Pour les lancer :
    RUN_LIVE=1 python skills/vulnerabilite-bpe/tests/test_live.py
    RUN_LIVE=1 python -m pytest skills/vulnerabilite-bpe/tests/test_live.py

But : détecter la DÉRIVE (lien INSEE mort, structure de zip/CSV changée, colonnes renommées,
registre GitHub cassé) — pannes que les tests hors-ligne laisseraient au vert. On n'asserte jamais
une valeur (elles changent), seulement la STRUCTURE, plus une validation end-to-end contre le schéma.
Si l'URL du registre est morte, c'est le signal qu'il faut maintenir dataset-registry.json.
"""

import csv
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> _common
sys.path.insert(0, SKILL_DIR)                   # vulnerabilite-bpe/ -> main, contract

import main  # noqa: E402
from _common import http_get_json, resolve_commune, reverse_commune, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
LIVE = os.environ.get("RUN_LIVE") == "1"

ALES_LAT, ALES_LON = 44.125, 4.0905


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

    def test_reverse_commune_shape(self):
        loc = reverse_commune(ALES_LAT, ALES_LON)
        self.assertIsInstance(loc.code_insee, str)
        self.assertEqual(len(loc.code_insee), 5)

    def test_registry_remote_is_valid_json(self):
        reg = http_get_json(main.REGISTRY_URL, timeout=30, require_json=False)  # raw = text/plain
        self.assertIn("entries", reg)
        self.assertGreater(len(reg["entries"]), 0)
        for e in reg["entries"]:
            self.assertIn("min_skill_version", e)
            self.assertTrue(e.get("files"))
            for f in e["files"]:
                self.assertIn("zone", f)
                self.assertIn("url", f)

    def test_download_and_columns(self):
        """Télécharge réellement le fichier du dernier millésime compatible et vérifie la
        structure : vrai ZIP, CSV de données avec DEPCOM/TYPEQU/LATITUDE/LONGITUDE."""
        entry, _ = main.resolve_source(tempfile.mkdtemp(), 60)
        for file_entry in entry["files"]:
            with tempfile.TemporaryDirectory() as tmp:
                csv_path, meta = main.dataset_path(entry, file_entry, tmp, True, 300)
                self.assertGreater(os.path.getsize(csv_path), 0)
                # Lire l'en-tête EXACTEMENT comme le skill (csv.reader) : la BPE 2024 cite ses
                # colonnes ("DEPCOM";"TYPEQU";…) — csv.reader retire les guillemets, un split
                # manuel les laisserait et ferait échouer la vérification à tort.
                enc = main._csv_encoding(csv_path)
                with open(csv_path, encoding=enc, errors="replace", newline="") as fh:
                    cols = next(csv.reader(fh, delimiter=main.CSV_SEP), [])
                for col in ("DEPCOM", "TYPEQU", "LATITUDE", "LONGITUDE"):
                    self.assertIn(col, cols)

    def test_end_to_end_conforms_and_finds_equipements(self):
        args = main.build_parser().parse_args(["--commune", "Alès"])
        out, code = main.run(args)
        validate(out, SCHEMA)
        self.assertNotIn("error", out.get("vulnerabilite", {}))
        com = out["vulnerabilite"]["commune"]
        # Alès est une sous-préfecture : elle a forcément des écoles et de la santé.
        self.assertGreater(com["ecoles_count"], 0)
        self.assertGreater(com["sante_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
