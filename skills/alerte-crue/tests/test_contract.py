# -*- coding: utf-8 -*-
"""Tests hors-ligne du contrat de sortie d'alerte-crue.

On rejoue des réponses API enregistrées (fixtures) via un faux http_get_json, on fait
tourner les adaptateurs, et on valide la sortie contre contract.schema.json. Aucun réseau.
Lançable seul :  python tests/test_contract.py   (ou via pytest).
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> pour _common
sys.path.insert(0, SKILL_DIR)                   # alerte-crue/ -> pour main, contract

import main  # noqa: E402
from _common import SkillError, validate  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _openmeteo_now(rain=True):
    """Réponse OpenMeteo synthétique calée sur l'heure courante (sinon le filtre
    'prochaines heures' viderait la fenêtre selon la date du test)."""
    base = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    times, precip = [], []
    for i in range(48):
        times.append((base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"))
        precip.append(0.0)
    if rain:
        precip[1], precip[2], precip[3] = 0.7, 3.6, 0.2  # pic à +2 h
    return {"hourly_units": {"precipitation": "mm"},
            "hourly": {"time": times, "precipitation": precip, "rain": precip},
            "daily": {"precipitation_sum": [round(sum(precip), 1)]}}


def _make_fake_http(fail_vigicrues=False, rain=True, empty_debit=False):
    def fake_http(url, params=None, timeout=20, retries=3):
        if "vigicrues" in url:
            if fail_vigicrues:
                raise SkillError("échec simulé Vigicrues", detail="test")
            return _load("vigicrues.json")
        if "referentiel/stations" in url:
            return _load("hubeau_stations.json")
        if "observations_tr" in url:
            if empty_debit and params["grandeur_hydro"] == "Q":
                return {"count": 0, "data": []}  # station sans débit temps réel
            return _load("hubeau_obs_%s.json" % params["grandeur_hydro"])
        if "open-meteo" in url:
            return _openmeteo_now(rain=rain)
        raise AssertionError("URL non prévue par les fixtures : %s" % url)
    return fake_http


class ContractTest(unittest.TestCase):
    def setUp(self):
        self._orig = main.http_get_json

    def tearDown(self):
        main.http_get_json = self._orig

    def _run(self, argv):
        args = main.build_parser().parse_args(argv)
        return main.run(args)

    def test_full_output_conforms(self):
        main.http_get_json = _make_fake_http()
        out, code = self._run(["--lat", "44.12", "--lon", "4.08"])
        validate(out, SCHEMA)  # lève si non conforme
        self.assertEqual(code, 0)
        self.assertEqual(out["vigilance"]["couleur"], "jaune")  # NivInfViCr=2
        self.assertEqual(out["hydro"][0]["station"], "V715501001")
        self.assertEqual(out["pluie"]["pic"]["precipitation_mm"], 3.6)
        self.assertEqual(len(out["pluie"]["heures_pluvieuses"]), 2)  # 0.7 et 3.6 >= 0.5

    def test_detail_adds_par_heure_and_conforms(self):
        main.http_get_json = _make_fake_http()
        out, _ = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "pluie", "--detail"])
        validate(out, SCHEMA)
        self.assertIn("par_heure", out["pluie"])
        self.assertEqual(len(out["pluie"]["par_heure"]), 24)

    def test_dry_weather_empty_list_conforms(self):
        main.http_get_json = _make_fake_http(rain=False)
        out, _ = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "pluie"])
        validate(out, SCHEMA)
        self.assertEqual(out["pluie"]["heures_pluvieuses"], [])
        self.assertIsNone(out["pluie"]["pic"])

    def test_missing_measure_is_explanatory_string(self):
        main.http_get_json = _make_fake_http(empty_debit=True)
        out, code = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "hydro"])
        validate(out, SCHEMA)                       # number-or-string reste conforme
        st = out["hydro"][0]
        self.assertIsInstance(st["hauteur_mm"], float)        # mesure OK : nombre
        self.assertIsInstance(st["debit_ls"], str)            # absente : chaîne
        self.assertIn("indisponible", st["debit_ls"])
        self.assertEqual(code, 0)                             # station listée (a une mesure)

    def test_error_variant_conforms(self):
        main.http_get_json = _make_fake_http(fail_vigicrues=True)
        out, code = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "vigilance"])
        validate(out, SCHEMA)               # la variante {error} doit aussi être conforme
        self.assertIn("error", out["vigilance"])
        self.assertEqual(code, 1)           # seule source demandée -> échec total


if __name__ == "__main__":
    unittest.main(verbosity=2)
