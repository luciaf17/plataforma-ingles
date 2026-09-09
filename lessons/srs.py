"""Leitner boxes (spec 7). Box 5 means mastered.

box:   0   1   2   3   4   5
days:  1   2   4   8   16  mastered
"""

from datetime import timedelta

from django.utils import timezone

BOX_INTERVAL_DAYS = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16}
MASTERED_BOX = 5


def today():
    return timezone.localdate()


def next_review_date(box, from_date=None):
    """Date of the next review for an item sitting in `box`."""
    from_date = from_date or today()
    days = BOX_INTERVAL_DAYS.get(min(box, MASTERED_BOX - 1), BOX_INTERVAL_DAYS[4])
    return from_date + timedelta(days=days)


def promote(box):
    return min(box + 1, MASTERED_BOX)


def demote(box):
    """A miss sends the item back to box 0 (spec 7)."""
    return 0
