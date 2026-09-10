from learners.models import Learner
from lessons.models import ErrorItem


def learner_context(request):
    """Sidebar needs the learner and how many errors are due (prototype: 'My errors · 7 due')."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    learner = Learner.for_user(user)
    return {
        "learner": learner,
        "due_errors_count": ErrorItem.objects.due_for(learner).count(),
    }
