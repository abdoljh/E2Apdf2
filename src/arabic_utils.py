"""
Arabic text utilities: reshaping and BiDi reordering.

Arabic requires two transformations before PDF rendering:
1. Reshaping: Letters change form based on position (isolated/initial/medial/final).
2. BiDi reordering: Logical order → visual order for RTL display.

This module implements both from scratch (no external deps).
If arabic-reshaper and python-bidi are available, it uses them instead.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Try to use external libraries if available; fall back to built-in impl
# ---------------------------------------------------------------------------
_USE_EXTERNAL = False
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display

    _USE_EXTERNAL = True
except ImportError:
    pass


# ===========================================================================
# PUBLIC API
# ===========================================================================

def prepare_arabic(text: str) -> str:
    """
    Full pipeline: reshape Arabic glyphs, then apply BiDi reordering.
    Returns text ready for PDF rendering engines that expect visual order.
    """
    if not text or not text.strip():
        return text

    if _USE_EXTERNAL:
        reshaped = arabic_reshaper.reshape(text)
        return _bidi_get_display(reshaped)

    reshaped = reshape_arabic(text)
    return bidi_reorder(reshaped)


def reshape_arabic(text: str) -> str:
    """Reshape Arabic characters to their positional forms."""
    if _USE_EXTERNAL:
        return arabic_reshaper.reshape(text)
    return _reshape_text(text)


def bidi_reorder(text: str) -> str:
    """Apply simplified BiDi algorithm for visual ordering."""
    if _USE_EXTERNAL:
        return _bidi_get_display(text)
    return _simple_bidi(text)


def is_arabic_char(ch: str) -> bool:
    """Check if a character is Arabic (letter, mark, or number form)."""
    if len(ch) != 1:
        return False
    cp = ord(ch)
    return (
        0x0600 <= cp <= 0x06FF  # Arabic
        or 0x0750 <= cp <= 0x077F  # Arabic Supplement
        or 0xFB50 <= cp <= 0xFDFF  # Arabic Presentation Forms-A
        or 0xFE70 <= cp <= 0xFEFF  # Arabic Presentation Forms-B
        or 0x0860 <= cp <= 0x086F  # Arabic Extended-A
        or 0x08A0 <= cp <= 0x08FF  # Arabic Extended-B
    )


def has_arabic(text: str) -> bool:
    """Check if text contains any Arabic characters."""
    return any(is_arabic_char(ch) for ch in text)


def is_arabic_diacritic(ch: str) -> bool:
    """Check if character is an Arabic diacritic (tashkeel)."""
    cp = ord(ch)
    return 0x064B <= cp <= 0x065F or cp in (0x0670, 0x0640)


# ===========================================================================
# ARABIC LETTER FORMS TABLE
# Maps base Unicode code point → (isolated, final, initial, medial)
# Using Arabic Presentation Forms-B (0xFE70–0xFEFF)
# ===========================================================================

# fmt: off
_LETTER_FORMS: dict[int, tuple[int, int, int, int]] = {
    # char: (isolated, final, initial, medial)
    0x0621: (0xFE80, 0xFE80, 0xFE80, 0xFE80),  # HAMZA (non-joining)
    0x0622: (0xFE81, 0xFE82, 0xFE81, 0xFE82),  # ALEF WITH MADDA
    0x0623: (0xFE83, 0xFE84, 0xFE83, 0xFE84),  # ALEF WITH HAMZA ABOVE
    0x0624: (0xFE85, 0xFE86, 0xFE85, 0xFE86),  # WAW WITH HAMZA
    0x0625: (0xFE87, 0xFE88, 0xFE87, 0xFE88),  # ALEF WITH HAMZA BELOW
    0x0626: (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C),  # YEH WITH HAMZA
    0x0627: (0xFE8D, 0xFE8E, 0xFE8D, 0xFE8E),  # ALEF
    0x0628: (0xFE8F, 0xFE90, 0xFE91, 0xFE92),  # BEH
    0x0629: (0xFE93, 0xFE94, 0xFE93, 0xFE94),  # TEH MARBUTA
    0x062A: (0xFE95, 0xFE96, 0xFE97, 0xFE98),  # TEH
    0x062B: (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),  # THEH
    0x062C: (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0),  # JEEM
    0x062D: (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4),  # HAH
    0x062E: (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8),  # KHAH
    0x062F: (0xFEA9, 0xFEAA, 0xFEA9, 0xFEAA),  # DAL
    0x0630: (0xFEAB, 0xFEAC, 0xFEAB, 0xFEAC),  # THAL
    0x0631: (0xFEAD, 0xFEAE, 0xFEAD, 0xFEAE),  # REH
    0x0632: (0xFEAF, 0xFEB0, 0xFEAF, 0xFEB0),  # ZAIN
    0x0633: (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4),  # SEEN
    0x0634: (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8),  # SHEEN
    0x0635: (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC),  # SAD
    0x0636: (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0),  # DAD
    0x0637: (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4),  # TAH
    0x0638: (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),  # ZAH
    0x0639: (0xFEC9, 0xFECA, 0xFECB, 0xFECC),  # AIN
    0x063A: (0xFECD, 0xFECE, 0xFECF, 0xFED0),  # GHAIN
    0x0640: (0x0640, 0x0640, 0x0640, 0x0640),   # TATWEEL (kashida)
    0x0641: (0xFED1, 0xFED2, 0xFED3, 0xFED4),  # FEH
    0x0642: (0xFED5, 0xFED6, 0xFED7, 0xFED8),  # QAF
    0x0643: (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC),  # KAF
    0x0644: (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),  # LAM
    0x0645: (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4),  # MEEM
    0x0646: (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8),  # NOON
    0x0647: (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC),  # HEH
    0x0648: (0xFEED, 0xFEEE, 0xFEED, 0xFEEE),  # WAW
    0x0649: (0xFEEF, 0xFEF0, 0xFEEF, 0xFEF0),  # ALEF MAKSURA
    0x064A: (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),  # YEH
}
# fmt: on

# Letters that ONLY join to the right (never to the left).
# They have only isolated & final forms (initial==isolated, medial==final).
_RIGHT_JOIN_ONLY = {
    0x0622, 0x0623, 0x0624, 0x0625, 0x0627,  # alef variants, waw+hamza
    0x062F, 0x0630, 0x0631, 0x0632,  # dal, thal, reh, zain
    0x0648, 0x0649,  # waw, alef maksura
    0x0629,  # teh marbuta
    0x0621,  # hamza
}

# Lam-Alef ligatures: (lam_form, alef_char) → ligature
_LAM_ALEF_LIGATURES: dict[tuple[int, int], int] = {
    (0x0644, 0x0622): 0xFEF5,  # Lam + Alef with Madda (isolated)
    (0x0644, 0x0623): 0xFEF7,  # Lam + Alef with Hamza above (isolated)
    (0x0644, 0x0625): 0xFEF9,  # Lam + Alef with Hamza below (isolated)
    (0x0644, 0x0627): 0xFEFB,  # Lam + Alef (isolated)
}

# Final forms of lam-alef ligatures (when preceded by a joining char)
_LAM_ALEF_FINAL: dict[tuple[int, int], int] = {
    (0x0644, 0x0622): 0xFEF6,
    (0x0644, 0x0623): 0xFEF8,
    (0x0644, 0x0625): 0xFEFA,
    (0x0644, 0x0627): 0xFEFC,
}


class _Form(Enum):
    ISOLATED = 0
    FINAL = 1
    INITIAL = 2
    MEDIAL = 3


def _can_join(cp: int) -> bool:
    """Whether this code point is a joining Arabic character."""
    return cp in _LETTER_FORMS and cp != 0x0621  # hamza doesn't join


def _joins_left(cp: int) -> bool:
    """Whether this character can join to the next character (on its left in RTL)."""
    return _can_join(cp) and cp not in _RIGHT_JOIN_ONLY


def _reshape_text(text: str) -> str:
    """
    Reshape Arabic text by converting each letter to its positional form.
    Handles Lam-Alef ligatures.
    """
    if not text:
        return text

    # Strip diacritics for joining analysis, but keep them in output
    chars: list[tuple[int, list[int]]] = []  # (base_cp, [diacritic_cps])
    for ch in text:
        cp = ord(ch)
        if is_arabic_diacritic(ch) and chars:
            chars[-1][1].append(cp)
        else:
            chars.append((cp, []))

    result: list[str] = []
    i = 0
    while i < len(chars):
        cp, diacritics = chars[i]

        # --- Lam-Alef ligature check ---
        if cp == 0x0644 and i + 1 < len(chars):
            next_cp = chars[i + 1][0]
            key = (0x0644, next_cp)
            if key in _LAM_ALEF_LIGATURES:
                # Determine if preceded by a left-joining char
                prev_joins = False
                if i > 0:
                    prev_cp = chars[i - 1][0]
                    prev_joins = _joins_left(prev_cp)

                if prev_joins:
                    lig = _LAM_ALEF_FINAL[key]
                else:
                    lig = _LAM_ALEF_LIGATURES[key]

                result.append(chr(lig))
                # Add diacritics from both lam and alef
                for d in diacritics:
                    result.append(chr(d))
                for d in chars[i + 1][1]:
                    result.append(chr(d))
                i += 2
                continue

        # --- Normal character ---
        if cp not in _LETTER_FORMS:
            # Non-Arabic or non-joining: pass through
            result.append(chr(cp))
            for d in diacritics:
                result.append(chr(d))
            i += 1
            continue

        # Determine neighbors (skipping non-joining chars for context)
        prev_joins_left = False
        if i > 0:
            prev_cp = chars[i - 1][0]
            prev_joins_left = _joins_left(prev_cp)

        next_joins = False
        if i + 1 < len(chars):
            next_cp = chars[i + 1][0]
            next_joins = _can_join(next_cp)

        # Current char's own abilities
        cur_joins_left = _joins_left(cp)

        # Determine form
        if prev_joins_left and next_joins and cur_joins_left:
            form = _Form.MEDIAL
        elif prev_joins_left and (not next_joins or not cur_joins_left):
            form = _Form.FINAL
        elif not prev_joins_left and next_joins and cur_joins_left:
            form = _Form.INITIAL
        else:
            form = _Form.ISOLATED

        forms = _LETTER_FORMS[cp]
        result.append(chr(forms[form.value]))
        for d in diacritics:
            result.append(chr(d))
        i += 1

    return "".join(result)


# ===========================================================================
# SIMPLIFIED BIDI ALGORITHM
# ===========================================================================

def _simple_bidi(text: str) -> str:
    """
    Simplified BiDi reordering for Arabic-dominant text.

    Full UAX #9 is ~50 pages. This handles the common cases:
    - Arabic text is reversed to visual order (RTL)
    - Embedded LTR runs (Latin, digits) stay in logical order
    - Parentheses/brackets are mirrored

    Good enough for translated PDF text; for edge cases,
    install python-bidi for the full algorithm.
    """
    if not text:
        return text

    _MIRROR = str.maketrans("()[]{}«»", ")(][}{»«")

    # Split into directional runs
    runs: list[tuple[str, str]] = []  # (direction, text)
    current_dir = ""
    current_text: list[str] = []

    for ch in text:
        if is_arabic_char(ch) or is_arabic_diacritic(ch):
            ch_dir = "R"
        elif ch.isascii() and ch.isalpha():
            ch_dir = "L"
        elif ch.isdigit():
            # Digits in Arabic context stay LTR
            ch_dir = "AN"  # Arabic numeral context
        elif ch in " \t":
            ch_dir = current_dir if current_dir else "R"  # neutral follows context
        else:
            ch_dir = current_dir if current_dir else "R"

        if ch_dir != current_dir and current_text:
            runs.append((current_dir, "".join(current_text)))
            current_text = []
        current_dir = ch_dir
        current_text.append(ch)

    if current_text:
        runs.append((current_dir, "".join(current_text)))

    # For RTL base direction: reverse run order, keep LTR runs internal order
    visual_parts: list[str] = []
    for direction, run_text in reversed(runs):
        if direction == "R":
            # Reverse individual characters and mirror brackets
            visual_parts.append(run_text[::-1].translate(_MIRROR))
        elif direction == "L":
            # LTR run keeps its order
            visual_parts.append(run_text)
        elif direction == "AN":
            # Arabic numerals: keep digit order (LTR within RTL)
            visual_parts.append(run_text)
        else:
            visual_parts.append(run_text)

    return "".join(visual_parts)
