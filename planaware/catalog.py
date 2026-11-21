"""Static reference data for the partial Taipei 212 pilot corridor."""

from dataclasses import dataclass
from importlib.resources import files
import json


class CatalogError(ValueError):
    """Invalid catalog data or selection."""


def _required_text(record, field):
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{field} must be a nonblank string")
    return value


@dataclass(frozen=True)
class StopOccurrence:
    stop_uid: str
    sequence: int
    name: str


@dataclass(frozen=True)
class RouteVariant:
    route_uid: str
    route_id: str
    subroute_uid: str
    subroute_id: str
    name: str
    direction: int
    headsign: str
    stops: tuple[StopOccurrence, ...]

    def stop(self, stop_uid: str, sequence: int) -> StopOccurrence:
        """Look up an occurrence, including when a stop UID repeats."""
        if type(sequence) is int:
            for stop in self.stops:
                if (stop.stop_uid, stop.sequence) == (stop_uid, sequence):
                    return stop
        raise CatalogError("Unknown stop occurrence for this route variant")

    def validate_stop_pair(
        self, boarding: tuple[str, int], alighting: tuple[str, int]
    ) -> tuple[StopOccurrence, StopOccurrence]:
        origin = self.stop(*boarding)
        destination = self.stop(*alighting)
        if origin.sequence >= destination.sequence:
            raise CatalogError("Alighting must follow boarding in stop order")
        return origin, destination


@dataclass(frozen=True)
class ReferenceCatalog:
    version: str
    coverage: str
    source: str
    route: RouteVariant

    def lookup_route(self, route_uid: str, subroute_uid: str, direction: int) -> RouteVariant:
        route = self.route
        if (route_uid, subroute_uid) != (route.route_uid, route.subroute_uid):
            raise CatalogError("Unknown route variant")
        if type(direction) is not int or direction != route.direction:
            raise CatalogError("Direction is not available for this route variant")
        return route

    @classmethod
    def from_json(cls, text: str) -> "ReferenceCatalog":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise CatalogError("Invalid catalog JSON") from error
        if not isinstance(data, dict) or not isinstance(data.get("route"), dict):
            raise CatalogError("Catalog must contain a route object")
        version = _required_text(data, "version")
        coverage = _required_text(data, "coverage")
        if coverage != "partial_pilot_corridor":
            raise CatalogError("Expected partial pilot corridor coverage")
        source = _required_text(data, "source")
        route = data["route"]
        direction = route.get("direction")
        if type(direction) is not int or not 0 <= direction <= 255:
            raise CatalogError("direction must be an integer in 0..255")
        entries = route.get("stops")
        if not isinstance(entries, list) or len(entries) < 2:
            raise CatalogError("Route requires at least two stop occurrences")
        stops = []
        previous_sequence = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise CatalogError("Each stop occurrence must be an object")
            sequence = entry.get("sequence")
            if type(sequence) is not int or sequence <= previous_sequence:
                raise CatalogError("Stop sequences must be positive and strictly increasing")
            stops.append(StopOccurrence(_required_text(entry, "stop_uid"), sequence,
                                        _required_text(entry, "name")))
            previous_sequence = sequence
        return cls(version, coverage, source, RouteVariant(
            route_uid=_required_text(route, "route_uid"),
            route_id=_required_text(route, "route_id"),
            subroute_uid=_required_text(route, "subroute_uid"),
            subroute_id=_required_text(route, "subroute_id"),
            name=_required_text(route, "name"),
            direction=direction,
            headsign=_required_text(route, "headsign"),
            stops=tuple(stops),
        ))


def load_catalog() -> ReferenceCatalog:
    """Load the bundled Taipei 212 pilot catalog."""
    resource = files("planaware").joinpath("catalogs", "taipei-212-v1.json")
    return ReferenceCatalog.from_json(resource.read_text(encoding="utf-8"))
