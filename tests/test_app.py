import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")

import app as app_module
from app import app, get_db, init_db


class WildTrackSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sheet_patcher = patch.object(
            app_module, "_load_from_google_sheets", side_effect=RuntimeError("offline test")
        )
        self.sheet_patcher.start()
        app_module._cache.update(time=0, data=[], source="")
        app.config.update(
            TESTING=True,
            DATABASE=os.path.join(self.temp_dir.name, "test.db"),
        )
        with app.app_context():
            init_db()
        self.client = app.test_client()

    def tearDown(self):
        self.sheet_patcher.stop()
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_public_pages_and_api(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/about").status_code, 200)
        self.assertEqual(self.client.get("/species/SP001").status_code, 200)
        response = self.client.get("/api/species")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.get_json()), 10)
        status = self.client.get("/api/data-status").get_json()
        self.assertEqual(status["source"], "Local CSV sample")
        self.assertFalse(status["connected"])

    def test_public_google_sheet_parser(self):
        self.sheet_patcher.stop()
        csv_content = (
            "Species_ID,Common_Name,Scientific_Name,IUCN_Status,Risk_Score,Map_Latitude,Map_Longitude\n"
            "SP900,Test Animal,Animalia testus,Endangered,4,27.5,84.4\n"
        ).encode("utf-8")
        response = Mock(content=csv_content)
        response.raise_for_status.return_value = None
        with patch.object(app_module.requests, "get", return_value=response) as request_get:
            data = app_module._load_from_google_sheets()
        self.assertEqual(data[0]["Species_ID"], "SP900")
        self.assertEqual(data[0]["Status_Code"], "EN")
        self.assertEqual(request_get.call_args.kwargs["params"]["range"], "A4:W")
        self.sheet_patcher.start()

    def test_account_watchlist_and_observation_flow(self):
        self.client.get("/register")
        response = self.client.post(
            "/register",
            data={
                "csrf_token": self.csrf(),
                "username": "field_user",
                "email": "field@example.com",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Welcome back, field_user", response.data)

        response = self.client.post(
            "/species/SP001/favorite",
            data={"csrf_token": self.csrf(), "next": "/dashboard"},
            follow_redirects=True,
        )
        self.assertIn(b"Tiger", response.data)
        self.assertIn(b"Species watched", response.data)

        response = self.client.post(
            "/observations/new",
            data={
                "csrf_token": self.csrf(),
                "species_id": "SP001",
                "location": "Chitwan, Nepal",
                "observed_on": "2026-01-02",
                "notes": "Observed tracks from a safe distance near the forest edge.",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Chitwan, Nepal", response.data)
        with app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)

    def test_protected_dashboard_redirects_to_login(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
