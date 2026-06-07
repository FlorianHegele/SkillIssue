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
from zoneinfo import ZoneInfo

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


def _openmeteo_now(rain=True, hours=48):
    """Réponse OpenMeteo synthétique calée sur l'heure courante (sinon le filtre
    'prochaines heures' viderait la fenêtre selon la date du test). Heures en zone
    Europe/Paris comme la vraie API (timezone=Europe/Paris), pour rester cohérent quel que
    soit le fuseau de la machine de test. `hours` permet de simuler une prévision tronquée."""
    base = datetime.now(ZoneInfo("Europe/Paris")).replace(minute=0, second=0, microsecond=0)
    times, precip = [], []
    for i in range(hours):
        times.append((base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"))
        precip.append(0.0)
    if rain and hours > 3:
        precip[1], precip[2], precip[3] = 0.7, 3.6, 0.2  # pic à +2 h, créneau contigu +1..+2
    return {"hourly_units": {"precipitation": "mm"},
            "hourly": {"time": times, "precipitation": precip}}


def _make_fake_http(fail_vigicrues=False, rain=True, empty_debit=False,
                    empty_all=False, vigicrues_doc=None, meteo_hours=48):
    def fake_http(url, params=None, timeout=20, retries=3):
        if "vigicrues" in url:
            if fail_vigicrues:
                raise SkillError("échec simulé Vigicrues", detail="test")
            return vigicrues_doc if vigicrues_doc is not None else _load("vigicrues.json")
        if "referentiel/stations" in url:
            return _load("hubeau_stations.json")
        if "observations_tr" in url:
            if empty_all:
                return {"count": 0, "data": []}  # aucune mesure (H ni Q) sur la station
            if empty_debit and params["grandeur_hydro"] == "Q":
                return {"count": 0, "data": []}  # station sans débit temps réel
            return _load("hubeau_obs_%s.json" % params["grandeur_hydro"])
        if "open-meteo" in url:
            return _openmeteo_now(rain=rain, hours=meteo_hours)
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
        self.assertEqual(out["hydro"]["stations"][0]["station"], "V715501001")
        self.assertGreaterEqual(out["hydro"]["stations_dans_rayon"], 1)
        self.assertEqual(out["pluie"]["pic"]["precipitation_mm"], 3.6)
        self.assertEqual(len(out["pluie"]["heures_pluvieuses"]), 2)  # 0.7 et 3.6 >= 0.5
        # 0.7 et 3.6 sont sur deux heures consécutives -> un seul créneau (cumul 4.3)
        self.assertEqual(len(out["pluie"]["creneaux"]), 1)
        self.assertEqual(out["pluie"]["creneaux"][0]["cumul_mm"], 4.3)

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
        st = out["hydro"]["stations"][0]
        self.assertIsInstance(st["hauteur_mm"], float)        # mesure OK : nombre
        self.assertIsInstance(st["debit_ls"], str)            # absente : chaîne
        self.assertIn("indisponible", st["debit_ls"])
        # Date propre à chaque mesure : H présente -> date_hauteur ; Q absente -> date_debit null
        self.assertIsInstance(st["date_hauteur"], str)
        self.assertIsNone(st["date_debit"])
        self.assertEqual(code, 0)                             # station listée (a une mesure)

    def test_vigicrues_niveau_string_is_coerced(self):
        # L'API renvoie NivInfViCr en chaîne "3" -> doit devenir l'entier 3 + couleur orange,
        # et rester conforme au schéma (sinon dérive d'API non détectée en offline).
        doc = {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "geometry": {"type": "MultiLineString",
                         "coordinates": [[[4.07, 44.11], [4.09, 44.13]]]},
            "properties": {"lbentcru": "Gardon d'Alès", "NivInfViCr": "3"}}]}
        main.http_get_json = _make_fake_http(vigicrues_doc=doc)
        out, _ = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "vigilance"])
        validate(out, SCHEMA)
        self.assertEqual(out["vigilance"]["niveau"], 3)
        self.assertIsInstance(out["vigilance"]["niveau"], int)
        self.assertEqual(out["vigilance"]["couleur"], "orange")

    def test_short_forecast_degrades_cumul_to_string(self):
        # < 24 h de prévision -> cumul devient une chaîne honnête (pas un faux total).
        main.http_get_json = _make_fake_http(meteo_hours=6)
        out, _ = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "pluie"])
        validate(out, SCHEMA)
        self.assertIsInstance(out["pluie"]["cumul_prochaines_24h_mm"], str)
        self.assertIn("< 24", out["pluie"]["cumul_prochaines_24h_mm"])

    def test_creneaux_split_on_dry_gap(self):
        # Deux épisodes pluvieux séparés par une heure sèche -> DEUX créneaux distincts
        # (et non un seul début/fin englobant l'accalmie).
        base = datetime.now(ZoneInfo("Europe/Paris")).replace(minute=0, second=0, microsecond=0)
        n = 12
        times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
        precip = [0.0] * n
        precip[1], precip[2] = 1.0, 2.0   # créneau A : +1 h, +2 h (contigus)
        precip[3] = 0.0                   # accalmie -> coupe
        precip[5] = 3.0                   # créneau B : +5 h (isolé)
        doc = {"hourly_units": {"precipitation": "mm"},
               "hourly": {"time": times, "precipitation": precip}}
        main.http_get_json = lambda url, params=None, timeout=20, retries=3: doc
        out, _ = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "pluie"])
        validate(out, SCHEMA)
        cr = out["pluie"]["creneaux"]
        self.assertEqual(len(cr), 2)
        self.assertEqual(cr[0]["cumul_mm"], 3.0)   # 1.0 + 2.0
        self.assertEqual(cr[0]["debut"], times[1])
        self.assertEqual(cr[0]["fin"], times[2])
        self.assertEqual(cr[1]["cumul_mm"], 3.0)   # épisode d'une seule heure
        self.assertEqual(cr[1]["debut"], cr[1]["fin"])

    def test_station_without_any_measure_is_dropped(self):
        # Station sans AUCUNE mesure numérique (H et Q vides) -> écartée. Plus aucune station
        # exploitable -> la source hydro renvoie {error}, code retour != 0 (seule source).
        main.http_get_json = _make_fake_http(empty_all=True)
        out, code = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "hydro"])
        validate(out, SCHEMA)
        self.assertIn("error", out["hydro"])
        self.assertEqual(code, 1)

    def test_max_stations_caps_and_reports_total(self):
        # 2 stations dans le rayon, --max-stations 1 -> une seule retournée, mais
        # stations_dans_rayon=2 révèle le plafonnement (pas de tri silencieux).
        main.http_get_json = _make_fake_http()
        out, code = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "hydro",
                               "--max-stations", "1"])
        validate(out, SCHEMA)
        self.assertEqual(len(out["hydro"]["stations"]), 1)
        self.assertEqual(out["hydro"]["stations_dans_rayon"], 2)
        self.assertEqual(code, 0)

    def test_only_deduplicates_sources(self):
        # --only répété ne doit déclencher qu'UN appel par source (pas de double exécution).
        main.http_get_json = _make_fake_http()
        calls = {"n": 0}
        orig = main.collect_vigilance

        def counting(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        main.collect_vigilance = counting
        try:
            out, code = self._run(["--lat", "44.12", "--lon", "4.08",
                                   "--only", "vigilance", "--only", "vigilance"])
        finally:
            main.collect_vigilance = orig
        validate(out, SCHEMA)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(code, 0)

    def test_error_variant_conforms(self):
        main.http_get_json = _make_fake_http(fail_vigicrues=True)
        out, code = self._run(["--lat", "44.12", "--lon", "4.08", "--only", "vigilance"])
        validate(out, SCHEMA)               # la variante {error} doit aussi être conforme
        self.assertIn("error", out["vigilance"])
        self.assertEqual(code, 1)           # seule source demandée -> échec total


if __name__ == "__main__":
    unittest.main(verbosity=2)
