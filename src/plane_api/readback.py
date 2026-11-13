"""Narrow effect comparison, independent of request fingerprints and transport."""
from collections.abc import Mapping
from typing import Any


HTML_FIELDS = frozenset({"description_html", "comment_html"})


def effect_matches(
    row: Mapping[str, Any], field: str, expected: Any, *, allow_html_envelope: bool = False
) -> bool:
    """Require presence and typed equality, or one literal plain HTML envelope.

    This is not HTML/text canonicalization: attributes, entities, whitespace,
    links and all interior bytes remain exact. Only the observed side may gain
    one ``<div>``/``</div>`` pair; never unwrap recursively or alter caller data.
    """
    if field not in row:
        return False
    observed = row[field]
    if type(observed) is type(expected) and observed == expected:
        return True
    return (
        allow_html_envelope
        and field in HTML_FIELDS
        and isinstance(expected, str)
        and isinstance(observed, str)
        and observed == "<div>" + expected + "</div>"
    )
