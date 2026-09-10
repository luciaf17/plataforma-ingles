"""Data for the Grammar screen (spec 7b): the syllabus with per-learner status,
and the learner's recurring error patterns grouped like the prototype cards."""

from collections import defaultdict

from learners.models import CEFR_ORDER, GrammarTopic, LearnerGrammarTopic
from lessons import srs
from lessons.models import ErrorItem
from lessons.taxonomy import TAXONOMY

STATUS_LABEL = {
    "not_started": "Not started",
    "introduced": "Introduced",
    "practicing": "Practicing",
    "mastered": "Mastered",
}
STATUS_PILL = {"not_started": "", "introduced": "amber", "practicing": "amber", "mastered": "green"}

FILTERS = [("all", "All"), ("needs_work", "Needs work"), ("improving", "Improving"), ("mastered", "Mastered")]

# Friendly titles for error patterns, by subcategory.
PATTERN_TITLES = {
    "tense": "Tenses", "aspect": "Simple vs continuous", "articles": "Articles", "prepositions": "Prepositions after verbs",
    "word_order": "Word order", "agreement": "Subject–verb agreement", "conditionals": "Conditionals", "modals": "Modals", "plurals": "Plurals",
    "wrong_word": "Wrong word", "false_friend": "False friends", "l1_interference": "Spanish structures translated literally",
    "register": "Register", "collocation": "Collocations", "phoneme": "Sounds", "word_stress": "Word stress", "sentence_stress": "Sentence stress",
    "fillers": "Fillers", "self_correction": "Self-corrections", "long_pause": "Long pauses", "circumlocution": "Circumlocution",
    "connectors": "Connectors", "politeness": "Politeness", "directness": "Directness",
}


def program(learner):
    """Syllabus grouped by level, each topic with the learner's status and counters."""
    progress = {p.topic_id: p for p in LearnerGrammarTopic.objects.filter(learner=learner)}
    levels = defaultdict(list)
    for topic in GrammarTopic.objects.order_by("order"):
        p = progress.get(topic.id)
        status = p.status if p else "not_started"
        levels[topic.cefr_level].append({
            "topic": topic,
            "status": status,
            "label": STATUS_LABEL[status],
            "pill": STATUS_PILL[status],
            "times_targeted": p.times_targeted if p else 0,
            "times_avoided": p.times_avoided if p else 0,
        })
    ordered = [level for level in CEFR_ORDER if level in levels]
    return [{"level": level, "topics": levels[level], "mastered": sum(1 for t in levels[level] if t["status"] == "mastered")} for level in ordered]


def topic_for_subcategory(subcategory, learner):
    mastered = set(LearnerGrammarTopic.objects.filter(learner=learner, status="mastered").values_list("topic_id", flat=True))
    for topic in GrammarTopic.objects.order_by("order"):
        if subcategory in (topic.related_subcategories or []) and topic.id not in mastered:
            return topic
    return None


def recurring_errors(learner, filter_key="all"):
    """Active (and mastered) errors grouped by subcategory into prototype-style cards."""
    items = ErrorItem.objects.filter(learner=learner).exclude(status="dismissed").order_by("-occurrences", "next_review_at")
    groups = defaultdict(list)
    for item in items:
        groups[(item.category, item.subcategory)].append(item)
    today = srs.today()
    cards = []
    for (category, subcategory), errors in groups.items():
        active = [e for e in errors if e.status == "active"]
        top = active[0] if active else errors[0]
        occurrences = sum(e.occurrences for e in errors)
        box = min((e.srs_box for e in active), default=srs.MASTERED_BOX)
        next_due = min((e.next_review_at for e in active), default=None)
        if not active:
            state = "mastered"
        elif box <= 1:
            state = "needs_work"
        else:
            state = "improving"
        if next_due is None:
            due_label = ""
        elif next_due <= today:
            due_label = "due today"
        else:
            days = (next_due - today).days
            due_label = "due tomorrow" if days == 1 else f"due in {days} days"
        cards.append({
            "category": category,
            "subcategory": subcategory,
            "title": PATTERN_TITLES.get(subcategory, subcategory.replace("_", " ").capitalize()),
            "description": top.explanation,
            "example": top,
            "count": occurrences,
            "active_count": len(active),
            "box": box,
            "pct": min(100, round(box * 100 / srs.MASTERED_BOX)),
            "due_label": due_label,
            "state": state,
            "topic": topic_for_subcategory(subcategory, learner),
        })
    if filter_key in ("needs_work", "improving", "mastered"):
        cards = [c for c in cards if c["state"] == filter_key]
    cards.sort(key=lambda c: (c["state"] == "mastered", -c["count"]))
    return cards
