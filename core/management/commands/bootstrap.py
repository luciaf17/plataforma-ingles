"""First-boot setup for a fresh environment (Railway has no shell by default).

Idempotent: creates the superuser from DJANGO_SUPERUSER_* if it does not
exist, loads the seed fixture if the syllabus is empty, and makes sure the
learner row exists. Safe to run on every start.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from learners.models import GrammarTopic, Learner


class Command(BaseCommand):
    help = "Create the superuser from env, load the seed if missing, ensure the learner exists"

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        User = get_user_model()

        if username and password and not User.objects.filter(username=username).exists():
            user = User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f"Created superuser {username}"))
        else:
            user = User.objects.filter(username=username).first() if username else None
            self.stdout.write(f"Superuser {username or '(unset)'}: {'exists' if user else 'not created'}")

        if not GrammarTopic.objects.exists():
            call_command("loaddata", "seed", verbosity=0)
            self.stdout.write(self.style.SUCCESS("Loaded seed fixture"))
        else:
            self.stdout.write(f"Seed present: {GrammarTopic.objects.count()} grammar topics")

        if user:
            Learner.for_user(user)
            self.stdout.write(f"Learner ready for {user.get_username()}")
