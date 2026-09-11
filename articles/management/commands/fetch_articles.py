"""Read the tech feeds and store new articles. Runs daily before the lesson is planned."""

from django.core.management.base import BaseCommand

from articles import fetch
from articles.models import Article


class Command(BaseCommand):
    help = "Fetch recent tech articles from public feeds for reading lessons"

    def add_arguments(self, parser):
        parser.add_argument("--prune-days", type=int, default=60, help="Drop unread articles older than this")

    def handle(self, *args, **options):
        result = fetch.refresh()
        dropped = fetch.prune(options["prune_days"])
        self.stdout.write(self.style.SUCCESS(
            f"Stored {result['stored']} new article(s), skipped {result['skipped']}, "
            f"reached {result['feeds']}/{len(fetch.FEEDS)} feeds"
        ))
        if dropped:
            self.stdout.write(f"Pruned {dropped} unread article(s)")
        self.stdout.write(f"Pool: {Article.objects.count()} article(s)")
        for row in Article.objects.all()[:5]:
            self.stdout.write(f"  {row.source_name}: {row.title[:70]} ({row.word_count} words)")
