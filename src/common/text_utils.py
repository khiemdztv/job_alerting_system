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
    "d": "dđ",
}
for base, chars in _CHAR_GROUPS.items():
    for char in chars:
        _ACCENT_MAP[char] = base

_TRANSLATION_TABLE = str.maketrans(_ACCENT_MAP)


def clean_vn_text(text: str) -> str:
    """Normalize Vietnamese text for search matching.

    - NFC unicode normalization
    - Lowercase
    - Strip diacritics (accents)
    - Treat 'y' and 'i' as equivalent
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
    if not text:
        return ""

    text = unicodedata.normalize("NFC", text.lower())

    cleaned = text.translate(_TRANSLATION_TABLE)
    # NOTE: Previously had `cleaned.replace("y", "i")` for Vietnamese y/i
    # equivalence (kĩ vs kỹ), but this breaks ALL English words containing 'y'
    # (analyst→analist, python→pithon, system→sistem).
    # Vietnamese accented y-variants (ỳýỷỹỵ) are already handled by _ACCENT_MAP.
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


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
