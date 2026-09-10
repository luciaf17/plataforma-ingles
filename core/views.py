from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from ai.models import ApiCall

# Title and subtitle per sidebar section, lifted from the prototype copy.
SECTIONS = {
    "listening": ("Listening", "Two listens max. You'll see the transcript after you answer."),
    "reading": ("Reading", "Real formats: issues, docs, emails, engineering blogs."),
    "writing": ("Writing", "You'll get a corrected version, a native \"upgrade\", and every error goes into your file."),
    "grammar": ("Grammar", "Built from your own errors, not a textbook index."),
    "vocabulary": ("Vocabulary", "Words you looked up, words the tutor planted, and words you've started using on your own."),
    "errors": ("My errors", "Every error you've made, where it came from, and how close it is to being gone."),
    "progress": ("Progress", "The number that matters is the last one."),
}


def today(request):
    now = timezone.localtime()
    # Day number without a leading zero, portable across Windows and Unix.
    title = f"{now:%A}, {now:%B} {now.day}"
    return render(request, "core/today.html", {"section": "today", "title": title})


def placeholder(request, section):
    if section not in SECTIONS:
        raise Http404
    title, subtitle = SECTIONS[section]
    context = {"section": section, "title": title, "subtitle": subtitle}
    if section == "progress":
        # The full Progress screen is module 22; the running API cost is shown from day one.
        context["usage"] = ApiCall.totals()
    return render(request, "core/placeholder.html", context)
