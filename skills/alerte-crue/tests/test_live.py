# -*- coding: utf-8 -*-
"""Sondes LIVE (opt-in) : vérifient que les vraies API parlent encore la forme attendue.

⚠ Désactivées par défaut (réseau, non déterministes). Pour les lancer :
    RUN_LIVE=1 python skills/alerte-crue/tests/test_live.py
    RUN_LIVE=1 python -m pytest skills/alerte-crue/tests/test_live.py

But : détecter la DÉRIVE des API (redirection, pagination, champ renommé, coupure de
version) — le genre de panne qui laisserait les tests hors-ligne au vert. On n'asserte
jamais une valeur (elles changent), seulement la STRUCTURE / le contrat, plus une
validation end-to-end de la sortie contre contract.schema.json.

À lancer avant de rendre / quand quelque chose semble cassé.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SKILL_DIR))  # skills/ -> _common
sys.path.insert(0, SKILL_DIR)                   # alerte-crue/ -> main, contract

import main  # noqa: E402
from _common import http_get_json, resolve_commune, validate  # noqa: E402

SCHEMA = os.path.join(SKILL_DIR, "contract.schema.json")
LIVE = os.environ.get("RUN_LIVE") == "1"

# Point de référence : Alès (sert d'exemple, pas de fallback applicatif).
ALES_LAT, ALES_LON = 44.125, 4.0905


@unittest.skipUnless(LIVE, "sondes live désactivées (mettre RUN_LIVE=1 pour activer)")
class LiveProbes(unittest.TestCase):

    def test_geo_api_resolves_ales(self):
        loc = resolve_commune("Alès")
        self.assertEqual(loc.code_insee, "30007")
        self.assertTrue(-90 <= loc.lat <= 90 and -180 <= loc.lon <= 180)

    def test_vigicrues_shape(self):
        # Si l'URL change (redirection) ou renvoie du HTML, http_get_json lèvera déjà.
        d = http_get_json(main.VIGICRUES_GEOJSON, timeout=40)
        self.assertEqual(d.get("type"), "FeatureCollection")
        feats = d.get("features", [])
        self.assertGreater(len(feats), 0)
        self.assertIn("NivInfViCr", feats[0]["properties"])
        self.assertIn(feats[0]["geometry"]["type"], ("MultiLineString", "LineString"))

    def test_hubeau_stations_and_obs_shape(self):
        stations = http_get_json(
            main.HUBEAU_STATIONS,
            params={"bbox": "3.95,44.02,4.23,44.23", "en_service": "true",
                    "format": "json", "size": 5,
                    "fields": "code_station,latitude_station,longitude_station"},
            timeout=30,
        )
        self.assertIn("data", stations)
        self.assertGreater(len(stations["data"]), 0)
        self.assertIn("code_station", stations["data"][0])
        # observations_tr : le simple fait que l'appel réussisse valide le statut 200/206
        # et le Content-Type JSON (aurait attrapé le 206 et la coupure de l'API v1).
        obs = http_get_json(
            main.HUBEAU_OBS,
            params={"code_entite": "V715501001", "grandeur_hydro": "H",
                    "size": 1, "sort": "desc", "format": "json",
                    "fields": "date_obs,resultat_obs"},
            timeout=20,
        )
        self.assertIsInstance(obs.get("data"), list)

    def test_openmeteo_shape(self):
        d = http_get_json(
            main.OPENMETEO,
            params={"latitude": ALES_LAT, "longitude": ALES_LON,
                    "hourly": "precipitation,rain",
                    "models": "meteofrance_arome_france_hd",
                    "timezone": "Europe/Paris", "forecast_days": 1},
            timeout=25,
        )
        self.assertIn("hourly", d)
        self.assertIn("precipitation", d["hourly"])
        self.assertEqual(d.get("hourly_units", {}).get("precipitation"), "mm")

    def test_end_to_end_conforms_to_schema(self):
        args = main.build_parser().parse_args(["--commune", "Alès"])
        out, _ = main.run(args)
        validate(out, SCHEMA)  # la sortie réelle doit rester conforme au contrat
        # Au moins une source doit répondre (sinon : panne réseau ou dérive généralisée).
        ok = [s for s in ("vigilance", "hydro", "pluie")
              if not (isinstance(out.get(s), dict) and "error" in out[s])]
        self.assertTrue(ok, "toutes les sources ont échoué en live : %s" % out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
