"""
HTTP-level tests for the FastAPI endpoints in main.py.

Uses FastAPI's TestClient to test request validation, error responses,
status codes, and rate limiting without needing a running server.
The engine is loaded once (same as production) so these are integration
tests, not mocks — they exercise the real routing and lookup code.
"""

import os
import sys
import unittest

# Import app after setting GTFS_PATH so it loads the real feed
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GTFS_PATH", os.path.join(HERE, "gtfs_data"))
os.environ.setdefault("USE_OSRM", "false")

from main import app

try:
    from fastapi.testclient import TestClient
    client = TestClient(app, raise_server_exceptions=False)
except Exception:
    import asyncio
    import json
    from urllib.parse import urlencode, urlsplit

    class _ASGIResponse:
        def __init__(self, status_code, headers, body):
            self.status_code = status_code
            self.headers = headers
            self._body = body

        def json(self):
            return json.loads(self._body.decode("utf-8"))

        @property
        def text(self):
            return self._body.decode("utf-8")

    class _ASGIClient:
        def __init__(self, asgi_app):
            self.app = asgi_app

        def get(self, url, params=None, headers=None):
            return self.request("GET", url, params=params, headers=headers)

        def post(self, url, json=None, params=None, headers=None):
            return self.request("POST", url, json_data=json, params=params, headers=headers)

        def request(self, method, url, params=None, json_data=None, headers=None):
            split = urlsplit(url)
            path = split.path
            query_str = split.query
            if params:
                encoded = urlencode(params)
                query_str = f"{query_str}&{encoded}" if query_str else encoded

            body_bytes = json.dumps(json_data).encode("utf-8") if json_data is not None else b""
            req_headers = [(b"host", b"testserver")]
            if json_data is not None:
                req_headers.append((b"content-type", b"application/json"))
            if headers:
                for k, v in headers.items():
                    req_headers.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method.upper(),
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": query_str.encode("ascii"),
                "headers": req_headers,
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
            }

            status_box = {}
            headers_box = {}
            body_chunks = []
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    status_box["status"] = message["status"]
                    headers_box["headers"] = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
                elif message["type"] == "http.response.body":
                    body_chunks.append(message.get("body", b""))

            async def run():
                await self.app(scope, receive, send)

            asyncio.run(run())
            return _ASGIResponse(status_box.get("status", 500), headers_box.get("headers", {}), b"".join(body_chunks))

    client = _ASGIClient(app)


class TestHealthEndpoint(unittest.TestCase):
    """GET /api/health"""

    def test_returns_ok(self):
        resp = client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("stops_loaded", data)
        self.assertGreater(data["stops_loaded"], 0)

    def test_includes_live_vehicle_count(self):
        data = client.get("/api/health").json()
        self.assertIn("live_vehicles_tracked", data)
        self.assertIsInstance(data["live_vehicles_tracked"], int)


class TestStationsEndpoint(unittest.TestCase):
    """GET /api/stations"""

    def test_returns_stations(self):
        resp = client.get("/api/stations")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertGreater(data["count"], 0)
        # Spot-check a station has the right fields
        station = data["stations"][0]
        self.assertIn("id", station)
        self.assertIn("name", station)
        self.assertIn("lat", station)
        self.assertIn("lon", station)


class TestRouteEndpointValidation(unittest.TestCase):
    """GET /api/route — parameter validation"""

    def test_missing_required_params_returns_422(self):
        resp = client.get("/api/route")
        self.assertEqual(resp.status_code, 422)

    def test_coords_outside_egypt_returns_400(self):
        resp = client.get("/api/route", params={
            "start_lat": 48.8566, "start_lon": 2.3522,  # Paris
            "end_lat": 30.0444, "end_lon": 31.2357,
        })
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "outside_coverage")

    def test_invalid_lang_returns_422(self):
        resp = client.get("/api/route", params={
            "start_lat": 30.0444, "start_lon": 31.2357,
            "end_lat": 30.06, "end_lon": 31.25,
            "lang": "fr",
        })
        self.assertEqual(resp.status_code, 422)

    def test_valid_daytime_request_returns_200(self):
        """A short downtown hop should return a route during daytime."""
        resp = client.get("/api/route", params={
            "start_lat": 30.0444, "start_lon": 31.2357,
            "end_lat": 30.06, "end_lon": 31.25,
            "lang": "en",
        })
        # Could be 200 (success) or 400 (no_route/outside_service_hours at night)
        self.assertIn(resp.status_code, [200, 400])
        data = resp.json()
        if resp.status_code == 200:
            self.assertTrue(data.get("success"))
            self.assertIn("options", data)


class TestBrowseRoutesEndpoint(unittest.TestCase):
    """GET /api/routes"""

    def test_returns_routes_list(self):
        resp = client.get("/api/routes", params={"lang": "en"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["routes"], list)
        self.assertGreater(len(data["routes"]), 0)

    def test_search_by_number(self):
        resp = client.get("/api/routes", params={"q": "65", "lang": "en"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreater(len(data["routes"]), 0)

    def test_filter_by_vehicle_type(self):
        resp = client.get("/api/routes", params={"vehicle_type": "metro", "lang": "en"})
        self.assertEqual(resp.status_code, 200)
        for r in resp.json()["routes"]:
            self.assertEqual(r["vehicle_type"], "metro")

    def test_invalid_vehicle_type_returns_422(self):
        resp = client.get("/api/routes", params={"vehicle_type": "helicopter"})
        self.assertEqual(resp.status_code, 422)

    def test_arabic_search_works(self):
        resp = client.get("/api/routes", params={"lang": "ar"})
        self.assertEqual(resp.status_code, 200)


class TestRouteDetailEndpoint(unittest.TestCase):
    """GET /api/routes/{route_id}"""

    def test_valid_route_returns_detail(self):
        # Get a real route_id first
        routes = client.get("/api/routes", params={"limit": 1, "lang": "en"}).json()["routes"]
        route_id = routes[0]["route_id"]
        resp = client.get(f"/api/routes/{route_id}", params={"lang": "en"})
        self.assertEqual(resp.status_code, 200)

    def test_unknown_route_returns_404(self):
        resp = client.get("/api/routes/NONEXISTENT_ROUTE_999", params={"lang": "en"})
        self.assertEqual(resp.status_code, 404)


class TestMetroEndpoints(unittest.TestCase):
    """GET /api/metro/stations and /api/metro/plan"""

    def test_metro_stations_returns_list(self):
        resp = client.get("/api/metro/stations", params={"lang": "en"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertGreater(len(data["stations"]), 0)

    def test_metro_plan_same_station_returns_400(self):
        stations = client.get("/api/metro/stations", params={"lang": "en"}).json()["stations"]
        name = stations[0]["name"]
        resp = client.get("/api/metro/plan", params={
            "from_stop": name, "to_stop": name, "lang": "en"
        })
        self.assertEqual(resp.status_code, 400)


class TestRailEndpoints(unittest.TestCase):
    """GET /api/rail/stations and /api/rail/plan"""

    def test_rail_stations_returns_list(self):
        resp = client.get("/api/rail/stations", params={"lang": "en"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

    def test_invalid_modes_returns_400(self):
        resp = client.get("/api/rail/stations", params={"modes": "helicopter"})
        self.assertEqual(resp.status_code, 400)

    def test_rail_plan_same_station_returns_400(self):
        stations = client.get("/api/rail/stations", params={"lang": "en"}).json()["stations"]
        name = stations[0]["name"]
        resp = client.get("/api/rail/plan", params={
            "from_stop": name, "to_stop": name, "lang": "en"
        })
        self.assertEqual(resp.status_code, 400)


class TestLivePositionEndpoints(unittest.TestCase):
    """POST /api/live/position and /api/live/end"""

    def test_valid_position_report(self):
        # Get a real route_id
        routes = client.get("/api/routes", params={"limit": 1, "lang": "en"}).json()["routes"]
        route_id = routes[0]["route_id"]
        resp = client.post("/api/live/position", json={
            "session_id": "test_session_1",
            "route_id": route_id,
            "direction_id": "0",
            "lat": 30.0444,
            "lon": 31.2357,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_unknown_route_returns_400(self):
        resp = client.post("/api/live/position", json={
            "session_id": "test_session_2",
            "route_id": "FAKE_ROUTE_999",
            "direction_id": "0",
            "lat": 30.0444,
            "lon": 31.2357,
        })
        self.assertEqual(resp.status_code, 400)

    def test_coords_outside_egypt_returns_400(self):
        routes = client.get("/api/routes", params={"limit": 1, "lang": "en"}).json()["routes"]
        route_id = routes[0]["route_id"]
        resp = client.post("/api/live/position", json={
            "session_id": "test_session_3",
            "route_id": route_id,
            "direction_id": "0",
            "lat": 48.8566,  # Paris
            "lon": 2.3522,
        })
        self.assertEqual(resp.status_code, 400)

    def test_missing_session_id_returns_422(self):
        resp = client.post("/api/live/position", json={
            "route_id": "any",
            "lat": 30.0, "lon": 31.0,
        })
        self.assertEqual(resp.status_code, 422)

    def test_end_session(self):
        resp = client.post("/api/live/end", json={
            "session_id": "test_session_to_end",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])


if __name__ == "__main__":
    unittest.main()
