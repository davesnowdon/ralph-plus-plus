"""Shared prompt rendering for step modules."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def render_prompt(template: str, **kwargs: str) -> str:
    """Substitute ``{placeholder}`` tokens in a prompt template.

    Warns about any placeholders present in the *original template* that
    were not provided in *kwargs*.  This catches typos
    (e.g. ``{diff_output}`` instead of ``{diff}``) without false-
    positives from ``{word}`` patterns inside substituted content such
    as code diffs.
    """
    # Detect unknown placeholders from the template BEFORE substitution
    # so that literals inside diff/code values are never scanned.
    template_placeholders = set(_PLACEHOLDER_RE.findall(template))
    unknown = template_placeholders - set(kwargs)
    if unknown:
        logger.warning(
            "Prompt template has unsubstituted placeholders: %s (available: %s)",
            ", ".join(f"{{{p}}}" for p in sorted(unknown)),
            ", ".join(f"{{{k}}}" for k in sorted(kwargs)),
        )

    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result
