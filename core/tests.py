from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import clear_url_caches
from learners.models import GrammarTopic
from unittest import mock
import importlib
import io
import os


class BootstrapCommandTests(TestCase):
    def run_bootstrap(self, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            call_command("bootstrap", stdout=out)
        return out.getvalue()

    def test_creates_superuser_seed_and_learner_once(self):
        output = self.run_bootstrap(DJANGO_SUPERUSER_USERNAME="lu", DJANGO_SUPERUSER_PASSWORD="pw", DJANGO_SUPERUSER_EMAIL="lu@example.com")
        self.assertIn("Created superuser lu", output)
        self.assertIn("Loaded seed fixture", output)
        self.assertIn("Learner ready for lu", output)
        user = get_user_model().objects.get(username="lu")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("pw"))
        self.assertTrue(hasattr(user, "learner"))

        again = self.run_bootstrap(DJANGO_SUPERUSER_USERNAME="lu", DJANGO_SUPERUSER_PASSWORD="other")
        self.assertIn("exists", again)
        self.assertIn(f"Seed present: {GrammarTopic.objects.count()}", again)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertTrue(get_user_model().objects.get().check_password("pw"))

    def test_without_env_it_only_loads_the_seed(self):
        output = self.run_bootstrap(DJANGO_SUPERUSER_USERNAME="", DJANGO_SUPERUSER_PASSWORD="")
        self.assertIn("not created", output)
        self.assertIn("Loaded seed fixture", output)
        self.assertEqual(get_user_model().objects.count(), 0)


class ProtectedMediaTests(TestCase):
    def test_media_requires_login_when_debug_is_off(self):
        with override_settings(DEBUG=False):
            import config.urls

            importlib.reload(config.urls)
            clear_url_caches()
            try:
                response = self.client.get("/media/turns/x.mp3")
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])
                get_user_model().objects.create_user("lu", password="pw")
                self.client.login(username="lu", password="pw")
                self.assertEqual(self.client.get("/media/turns/missing.mp3").status_code, 404)
            finally:
                importlib.reload(config.urls)
                clear_url_caches()
