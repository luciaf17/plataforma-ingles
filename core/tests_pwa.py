import json

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from learners.models import Learner


def static_exists(url):
    """True when a `/static/...` URL maps to a file the app ships."""
    return finders.find(url.removeprefix("/static/")) is not None


class PwaTests(TestCase):
    fixtures = ["seed"]

    def test_manifest_is_public_and_opens_on_today(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/manifest+json"))
        data = json.loads(response.content)
        self.assertEqual(data["start_url"], "/")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in data["icons"]}, {"192x192", "512x512"})
        self.assertIn("maskable", [icon.get("purpose") for icon in data["icons"]])
        for icon in data["icons"]:
            self.assertTrue(static_exists(icon["src"]), icon["src"])

    def test_service_worker_is_served_from_the_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/javascript"))
        self.assertEqual(response["Cache-Control"], "no-cache")
        body = response.content.decode()
        self.assertIn('"/offline/"', body)
        self.assertIn('"/static/"', body)
        for url in ["/static/css/app.css", "/static/js/app.js"]:
            self.assertIn(url, body)
            self.assertTrue(static_exists(url), url)

    def test_offline_page_needs_no_login_and_no_cdn(self):
        response = self.client.get("/offline/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You're offline")
        self.assertNotContains(response, "cdn.")
        self.assertNotContains(response, "fonts.googleapis")

    def test_pages_link_the_manifest_and_the_apple_icon(self):
        user = get_user_model().objects.create_user("lu", password="pw")
        learner = Learner.for_user(user)
        learner.goal_statement = "technical interviews"
        learner.save()
        self.client.login(username="lu", password="pw")
        response = self.client.get("/errors/")
        self.assertContains(response, 'rel="manifest" href="/manifest.webmanifest"')
        self.assertContains(response, 'rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"')
        self.assertContains(response, 'name="theme-color" content="#1a1e27"')
        self.assertTrue(static_exists("/static/icons/apple-touch-icon.png"))
