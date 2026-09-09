from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

# Title and subtitle per sidebar section, lifted from the prototype copy.
SECTIONS = {
    "speaking": ("Speaking", "Push-to-talk conversation with a tutor who knows your file."),
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
    return render(
        request,
        "core/placeholder.html",
        {"section": section, "title": title, "subtitle": subtitle},
    )
