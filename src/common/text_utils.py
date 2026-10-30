"""
Vietnamese text normalization utilities for ViecLamBot.

Centralizes text cleaning logic used across ETL, search, and matching.
Extracted from loader.py to avoid circular imports.
"""

from __future__ import annotations

import re
import unicodedata

# Map of accented Vietnamese characters to their base Latin equivalents.
_ACCENT_MAP: dict[str, str] = {}
_CHAR_GROUPS = {
    "a": "aàáảãạăằắẳẵặâầấẩẫậ",
    "e": "eèéẻẽẹêềếểễệ",
    "i": "iìíỉĩị",
    "o": "oòóỏõọôồốổỗộơờớởỡợ",
    "u": "uùúủũụưừứửữự",
    "y": "yỳýỷỹỵ",
    "d": "dđð",  # ð (U+00F0) is how some boards (Indeed) write Đ in lowercase
}
for base, chars in _CHAR_GROUPS.items():
    for char in chars:
        _ACCENT_MAP[char] = base

_TRANSLATION_TABLE = str.maketrans(_ACCENT_MAP)


# Latin "Ð/ð" (U+00D0/U+00F0) look identical to Vietnamese "Đ/đ" (U+0110/U+0111).
_ETH_TABLE = str.maketrans({"Ð": "Đ", "ð": "đ"})


def normalize_vn_text(text: str) -> str:
    """Lowercase NFC text with diacritics kept and whitespace collapsed.

    ``fold_vn_text(normalize_vn_text(x)) == clean_vn_text(x)`` and folding never
    changes the length, so a span found in the folded string is the same span in
    the accented one.  Search uses that to tell "ít" from "it" or "điện" from "diện".
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text).translate(_ETH_TABLE).lower())
    return re.sub(r"\s+", " ", text).strip()


def fold_vn_text(text: str) -> str:
    """Strip Vietnamese diacritics from already normalized text (1:1 per character)."""
    return text.translate(_TRANSLATION_TABLE)


def clean_vn_text(text: str) -> str:
    """Normalize Vietnamese text for search matching.

    - NFC unicode normalization
    - Lowercase
    - Strip diacritics (accents)
    - Collapse whitespace

    Args:
        text: Raw Vietnamese or English text.

    Returns:
        Cleaned text suitable for token matching.

    Examples:
        >>> clean_vn_text("Kế Toán")
        'ke toan'
        >>> clean_vn_text("Hồ Chí Minh")
        'ho chi minh'
        >>> clean_vn_text("Công Nghệ Thông Tin")
        'cong nghe thong tin'
    """
    # NOTE: Previously had `cleaned.replace("y", "i")` for Vietnamese y/i
    # equivalence (kĩ vs kỹ), but this breaks ALL English words containing 'y'
    # (analyst→analist, python→pithon, system→sistem).
    # Vietnamese accented y-variants (ỳýỷỹỵ) are already handled by _ACCENT_MAP.
    return fold_vn_text(normalize_vn_text(text))


def slugify_vn(text: str) -> str:
    """Convert Vietnamese text to URL-safe slug.

    Args:
        text: Vietnamese text (e.g. "Kế Toán").

    Returns:
        URL slug (e.g. "ke-toan").

    Examples:
        >>> slugify_vn("Kế Toán")
        'ke-toan'
        >>> slugify_vn("Data Engineer")
        'data-engineer'
        >>> slugify_vn("nhân viên marketing")
        'nhan-vien-marketing'
    """
    # NFD decomposition to separate base characters from combining marks
    text = unicodedata.normalize("NFD", text.lower().strip())
    # Remove combining marks (diacritics)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    # Handle đ → d explicitly (NFD doesn't decompose đ)
    text = text.replace("đ", "d")
    # Replace non-alphanumeric with hyphens
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
