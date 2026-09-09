"""Closed error taxonomy (spec 6). The analyzer schema and the models both
import from here so the LLM can never invent a category."""

TAXONOMY = {
    "grammar": [
        "tense", "aspect", "articles", "prepositions", "word_order",
        "agreement", "conditionals", "modals", "plurals",
    ],
    "vocabulary": ["wrong_word", "false_friend", "l1_interference", "register", "collocation"],
    # Pronunciation is inferred from transcripts only, so it is always low confidence.
    "pronunciation": ["phoneme", "word_stress", "sentence_stress"],
    "fluency": ["fillers", "self_correction", "long_pause", "circumlocution"],
    "discourse": ["connectors", "politeness", "directness"],
}

CATEGORIES = list(TAXONOMY)
SUBCATEGORIES = sorted({sub for subs in TAXONOMY.values() for sub in subs})

CATEGORY_CHOICES = [(c, c.replace("_", " ").capitalize()) for c in CATEGORIES]
SUBCATEGORY_CHOICES = [(s, s.replace("_", " ").capitalize()) for s in SUBCATEGORIES]


def validate_taxonomy(category, subcategory):
    """Raise ValueError unless (category, subcategory) is a known pair."""
    if category not in TAXONOMY:
        raise ValueError(f"Unknown error category: {category!r}")
    if subcategory not in TAXONOMY[category]:
        raise ValueError(f"Subcategory {subcategory!r} does not belong to {category!r}")
