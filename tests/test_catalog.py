import json
from importlib.resources import files
import unittest

from planaware.catalog import CatalogError, ReferenceCatalog, load_catalog


class ReferenceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()
        self.route = self.catalog.lookup_route("TPE10912", "TPE10912", 0)
        self.boarding = ("TPE10600", 10)
        self.alighting = ("TPE10606", 13)

    def catalog_data(self):
        resource = files("planaware").joinpath("catalogs", "taipei-212-v1.json")
        return json.loads(resource.read_text(encoding="utf-8"))

    def test_bundled_catalog_matches_verified_pilot(self):
        self.assertEqual(self.catalog.version, "taipei-212-v1")
        self.assertEqual(self.catalog.coverage, "partial_pilot_corridor")
        self.assertIn("TDX", self.catalog.source)
        self.assertEqual((self.route.name, self.route.headsign), ("212", "青年公園"))
        self.assertEqual((self.route.route_id, self.route.subroute_id), ("10912", "10912"))
        boarding, alighting = self.route.validate_stop_pair(self.boarding, self.alighting)
        self.assertEqual(self.route.stops, (boarding, alighting))
        self.assertEqual(boarding.name, "南港展覽館")
        self.assertEqual(alighting.name, "南港行政中心（南港車站）")

    def test_rejects_wrong_direction_and_unknown_variant(self):
        for route_uid, subroute_uid, direction in (
            ("TPE10912", "TPE10912", 1),
            ("TPE10912", "TPE10912", False),
            ("TPE10912", "TPE10911", 0),
            ("TPE16132", "TPE157442", 0),
        ):
            with self.subTest(route_uid=route_uid, subroute_uid=subroute_uid, direction=direction):
                with self.assertRaises(CatalogError):
                    self.catalog.lookup_route(route_uid, subroute_uid, direction)

    def test_rejects_invalid_stop_pairs(self):
        for boarding, alighting in (
            (self.alighting, self.boarding),
            (self.boarding, self.boarding),
            (self.boarding, ("TPE10702", 13)),
            (("TPE10600", 13), self.alighting),
        ):
            with self.subTest(boarding=boarding, alighting=alighting):
                with self.assertRaises(CatalogError):
                    self.route.validate_stop_pair(boarding, alighting)

    def test_rejects_malformed_catalogs(self):
        for text in ("{", "[]", "{}"):
            with self.subTest(text=text), self.assertRaises(CatalogError):
                ReferenceCatalog.from_json(text)

        for field in ("route_uid", "route_id", "subroute_uid", "subroute_id"):
            data = self.catalog_data()
            del data["route"][field]
            with self.subTest(field=field), self.assertRaises(CatalogError):
                ReferenceCatalog.from_json(json.dumps(data))

        for changes in ({"stop_uid": ""}, {"sequence": 10}, {"sequence": 9}, {"sequence": True}):
            data = self.catalog_data()
            data["route"]["stops"][1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(CatalogError):
                ReferenceCatalog.from_json(json.dumps(data))

    def test_repeated_stop_uid_is_distinguished_by_sequence(self):
        data = self.catalog_data()
        stops = data["route"]["stops"]
        stops.append(dict(stops[0], sequence=20))
        route = ReferenceCatalog.from_json(json.dumps(data)).lookup_route("TPE10912", "TPE10912", 0)
        self.assertEqual(route.stop("TPE10600", 20).sequence, 20)
        self.assertEqual(route.validate_stop_pair(self.alighting, ("TPE10600", 20))[1].sequence, 20)
        with self.assertRaises(CatalogError):
            route.validate_stop_pair(("TPE10600", 20), self.alighting)
