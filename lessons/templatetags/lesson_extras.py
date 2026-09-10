from django import template
from django.utils.html import escape, format_html_join
from django.utils.safestring import mark_safe

from ai import tutor

register = template.Library()


@register.filter
def tutor_line(text):
    """Tutor text with *recasts* rendered in green (spec 8: recasts en verde)."""
    parts = tutor.to_html_parts(text or "")
    html = "".join(
        f'<span class="recast">{escape(fragment)}</span>' if is_recast else escape(fragment)
        for is_recast, fragment in parts
    )
    return mark_safe(html)


@register.filter
def mmss(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
