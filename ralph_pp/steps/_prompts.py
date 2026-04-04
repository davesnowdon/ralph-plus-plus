"""Shared prompt rendering for step modules."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def render_prompt(template: str, **kwargs: str) -> str:
    """Substitute ``{placeholder}`` tokens in a prompt template.

    Warns about any tokens that were not substituted, since a typo
    (e.g. ``{diff_output}`` instead of ``{diff}``) would silently pass
    the literal token to the model.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)

    remaining = _PLACEHOLDER_RE.findall(result)
    if remaining:
        logger.warning(
            "Prompt template has unsubstituted placeholders: %s (available: %s)",
            ", ".join(f"{{{p}}}" for p in remaining),
            ", ".join(f"{{{k}}}" for k in kwargs),
        )
    return result
