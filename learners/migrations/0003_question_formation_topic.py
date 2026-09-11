"""Add question formation to the syllabus, right after the present simple.

It was missing, and it is the point this learner most visibly needs: her
checkpoint produced "Which the mean of trade off?" — the Spanish question
shape with English words, no auxiliary and no inversion.

Inserting it at order 2 shifts every later topic down by one. `order` is
unique, so the shift runs from the end backwards.
"""

from django.db import migrations

SLUG = "question-formation"
AT = 2

TOPIC = {
    "slug": SLUG,
    "title": "Questions: auxiliaries and word order",
    "cefr_level": "A2",
    "order": AT,
    "summary_es": (
        "En español una pregunta es la misma frase con otra entonación: '¿Vos trabajás acá?' / 'Vos trabajás acá.' "
        "En inglés hay que reordenar y, casi siempre, agregar un auxiliar que el español no tiene: do, does, did. "
        "El verbo principal queda en base form porque el auxiliar ya carga el tiempo y la persona. "
        "Con question words (what, where, how long) el auxiliar va igual, después de la pregunta: 'Where do you work?', "
        "no 'Where you work?'. Y 'What does X mean?' es la traducción de '¿Qué significa X?', no '¿Cuál es el "
        "significado?' — 'which' se usa para elegir entre opciones conocidas, no para pedir un significado."
    ),
    "examples": [
        {
            "wrong": "Which the mean of trade-off?",
            "right": "What does trade-off mean?",
            "note_es": "'¿Qué significa…?' es 'What does … mean?'. Falta el auxiliar 'does' y el verbo va en base form.",
        },
        {
            "wrong": "Where you work now?",
            "right": "Where do you work now?",
            "note_es": "Después de la question word hace falta el auxiliar: where + do + sujeto + verbo.",
        },
        {
            "wrong": "You have finished the migration?",
            "right": "Have you finished the migration?",
            "note_es": "En inglés no alcanza con la entonación: el auxiliar se adelanta al sujeto.",
        },
    ],
    # Only word order: a question is an auxiliary-and-inversion problem. Leaving
    # "tense" here would make this topic outrank past-simple for every tense error,
    # because the planner takes the lowest-order topic covering the subcategory.
    "related_subcategories": ["word_order"],
}


def add(apps, schema_editor):
    GrammarTopic = apps.get_model("learners", "GrammarTopic")
    if GrammarTopic.objects.filter(slug=SLUG).exists():
        return
    # Nothing seeded yet (a fresh database loads the fixture later): leave it alone.
    if not GrammarTopic.objects.exists():
        return
    for topic in GrammarTopic.objects.filter(order__gte=AT).order_by("-order"):
        topic.order += 1
        topic.save(update_fields=["order"])
    GrammarTopic.objects.create(**TOPIC)


def remove(apps, schema_editor):
    GrammarTopic = apps.get_model("learners", "GrammarTopic")
    if not GrammarTopic.objects.filter(slug=SLUG).exists():
        return
    GrammarTopic.objects.filter(slug=SLUG).delete()
    for topic in GrammarTopic.objects.filter(order__gt=AT).order_by("order"):
        topic.order -= 1
        topic.save(update_fields=["order"])


class Migration(migrations.Migration):
    dependencies = [("learners", "0002_learnergrammartopic_introduced_in")]
    operations = [migrations.RunPython(add, remove)]
