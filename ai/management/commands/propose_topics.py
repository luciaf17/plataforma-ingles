"""Write new class subjects out of each learner's file when they run low."""

from django.core.management.base import BaseCommand

from ai import client, propose_topics
from learners.models import Learner


class Command(BaseCommand):
    help = "Propose new lesson topics for every learner who is running out of them"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Propose even when there are topics left")
        parser.add_argument("--count", type=int, default=propose_topics.HOW_MANY)

    def handle(self, *args, **options):
        for learner in Learner.objects.select_related("user"):
            try:
                if options["force"]:
                    created = propose_topics.propose(learner, how_many=options["count"])
                else:
                    created = propose_topics.propose_if_needed(learner, how_many=options["count"])
            except client.AIUnavailable as exc:
                self.stderr.write(self.style.ERROR(f"{learner}: {exc}"))
                continue
            if not created:
                self.stdout.write(f"{learner}: still has topics to work through")
                continue
            self.stdout.write(self.style.SUCCESS(f"{learner}: proposed {len(created)} topic(s)"))
            for topic in created:
                self.stdout.write(f"  [{topic.track.slug}] {topic.title}")
                self.stdout.write(f"      {topic.proposed_reason}")
