from django import template

from core.ratings import base_model_slug, scope_label

register = template.Library()


@register.filter
def judge_label(model_slug):
    """``"google/gemini-4-hydrogen:batch"`` → ``"gemini-4-hydrogen"``."""
    if not model_slug:
        return ""
    return scope_label(f"model:{base_model_slug(model_slug)}")
