"""Leave today's lesson planned (spec 4.1). Run by cron at 3 AM, or lazily
when the dashboard opens and nothing is planned yet."""

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from ai import client, planner
from learners.models import Learner


class Command(BaseCommand):
    help = "Prepare today's lesson for a learner (default: the first learner)"

    def add_arguments(self, parser):
        parser.add_argument("--learner", help="Username; defaults to the only/first learner")
        parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD, defaults to today")
        parser.add_argument("--skill", choices=planner.SKILLS)
        parser.add_argument("--track", choices=planner.TRACKS)
        parser.add_argument("--topic", type=int, help="Topic id")
        parser.add_argument("--duration", type=int, default=planner.DEFAULT_DURATION)
        parser.add_argument("--force", action="store_true", help="Replace an unstarted plan for that day")
        parser.add_argument("--show", action="store_true", help="Print the plan JSON")

    def handle(self, *args, **options):
        learners = Learner.objects.select_related("user")
        if options["learner"]:
            learners = learners.filter(user__username=options["learner"])
        learner = learners.first()
        if not learner:
            raise CommandError("No learner found. Create a superuser and open the admin once.")

        try:
            lesson, created = planner.prepare_next_lesson(
                learner,
                on=options["date"],
                skill=options["skill"],
                track=options["track"],
                topic=options["topic"],
                duration=options["duration"],
                force=options["force"],
            )
        except (client.AIUnavailable, planner.NothingToPlan, NotImplementedError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        verb = "Prepared" if created else "Already planned"
        plan = lesson.plan
        self.stdout.write(self.style.SUCCESS(f"{verb}: lesson #{lesson.id} for {lesson.scheduled_for}"))
        self.stdout.write(f"  {plan.get('title')}")
        self.stdout.write(f"  {lesson.skill} · {lesson.track.slug} · {plan.get('duration_min')} min")
        if lesson.grammar_topic:
            self.stdout.write(f"  mini-lesson: {lesson.grammar_topic.title} ({plan.get('grammar_reason')})")
        self.stdout.write(f"  targets {len(plan.get('targeted_error_ids', []))} due errors, {len(plan.get('vocabulary', []))} vocabulary items")
        for phase in plan.get("phases", []):
            self.stdout.write(f"  - {phase['key']:<12}{phase['minutes']:>3} min  {phase['title']}")
        if options["show"]:
            self.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2))
