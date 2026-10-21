"""Bounded plain-text projection of verified game localization templates.

Native UIUtil resolves the original [key] references once, then formats numeric
arguments as strings. Unsupported templates stay unavailable, never raw markup.
"""

from collections.abc import Mapping
import html
import math
import re
import struct


MAX_TEXT = 16_384
MAX_PARAMS = 128
_REFERENCE = re.compile(r"\[([^\]]*)\]")
_FORMAT = re.compile(r"\{\{|\}\}|\{([0-9]+)(?:,\s*(-?[0-9]+))?(?::[^{}]*)?\}")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_STYLE = re.compile(r"</?(?:color|size|b|i|u|s|mark|link|nobr|font|style|sup|sub)(?:\s*=[^<>]*|\s+[^<>]*)?\s*>", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def _number(value):
    if type(value) is int:
        if not -(1 << 63) <= value < 1 << 63:
            raise ValueError
        raw = str(value)
    elif type(value) is float and math.isfinite(value):
        single = struct.unpack("<f", struct.pack("<f", value))[0]
        if not math.isfinite(single):
            raise ValueError
        raw = format(single, ".7g").upper()
    else:
        raise ValueError
    if "E" in raw:
        return raw
    sign = "-" if raw.startswith("-") else ""
    integer, dot, fraction = raw.removeprefix("-").partition(".")
    # CN integer formatting groups only the integral part, preserving decimals.
    return sign + f"{int(integer):,}" + (dot + fraction if dot else "")


def _format(template, values):
    parts, previous = [], 0
    for match in _FORMAT.finditer(template):
        literal = template[previous:match.start()]
        if "{" in literal or "}" in literal:
            raise ValueError
        parts.append(literal)
        token = match.group()
        if token in ("{{", "}}"):
            parts.append(token[0])
        else:
            index = int(match[1])
            if index >= len(values):
                raise ValueError
            value = values[index]
            if match[2]:
                width = int(match[2])
                if abs(width) > 64:
                    raise ValueError
                value = value.rjust(width) if width >= 0 else value.ljust(-width)
            parts.append(value)
        previous = match.end()
    suffix = template[previous:]
    if "{" in suffix or "}" in suffix:
        raise ValueError
    parts.append(suffix)
    return "".join(parts)


def render_text(key, params, texts: Mapping[str, str]) -> str | None:
    """Return readable public text, or None for missing/unsupported source data."""
    if (type(key) is not str or not key or not isinstance(texts, Mapping)
            or type(params) not in (list, tuple) or len(params) > MAX_PARAMS):
        return None
    template = texts.get(key)
    if type(template) is not str or not template.strip() or len(template) > MAX_TEXT:
        return None
    try:
        values = [_number(value) for value in params]
        references = list(_REFERENCE.finditer(template))
        if len(references) > 256:
            return None
        for match in references:
            replacement = texts.get(match[1])
            if type(replacement) is not str or len(replacement) > MAX_TEXT:
                return None
            template = template.replace(match[0], replacement)
            if len(template) > MAX_TEXT:
                return None
        if _REFERENCE.search(template):
            return None
        text = _format(template, values)
        text = html.unescape(_STYLE.sub("", _BREAK.sub("\n", text)))
        if _TAG.search(text) or len(text) > MAX_TEXT:
            return None
        return text.strip() or None
    except (ValueError, OverflowError, struct.error):
        return None
