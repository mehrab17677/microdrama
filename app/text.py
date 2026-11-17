"""Text helpers shared by ingestion (phase 4) and the originality checks (phase 6)."""
from __future__ import annotations

import re
import unicodedata

_BENGALI = re.compile(r"[ঀ-৿]")
_LATIN = re.compile(r"[A-Za-z]")

def _keep(ch: str) -> str:
    # Letters, combining marks (Bangla vowel signs are marks, which \w would split on) and numbers stay.
    return ch if unicodedata.category(ch)[0] in "LMN" else " "


def normalize_genre(name: str) -> str:
    """The one genre-normalisation function, used for insert and lookup: lower-case, trimmed,
    runs of spaces and hyphens become a single space."""
    return re.sub(r"[\s\-]+", " ", name.strip().lower())


def normalize_for_match(text: str) -> str:
    """For blocklist and wording checks: NFKC, case-folded, punctuation to spaces, one space between
    words. Combining marks are kept so Bangla words stay intact."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(_keep(ch) for ch in text).split())


def word_count(text: str) -> int:
    return len(text.split())


def detect_language(text: str) -> str:
    """'bn' when Bangla script dominates the letters, 'en' when Latin does, else 'other'."""
    bn, en = len(_BENGALI.findall(text)), len(_LATIN.findall(text))
    if bn == en == 0:
        return "other"
    return "bn" if bn >= en else "en"


def has_bengali(text: str) -> bool:
    return bool(_BENGALI.search(text))


def contains_term(normalized_text: str, normalized_term: str) -> bool:
    """Does `normalized_text` contain the term? Latin terms must match whole words (so 'ali' is not
    found inside 'alice'); Bangla terms match as substrings because suffixes attach to names."""
    if not normalized_term:
        return False
    if has_bengali(normalized_term):
        return normalized_term in normalized_text
    return f" {normalized_term} " in f" {normalized_text} "


def has_shared_run(a: str, b: str, n: int = 8) -> bool:
    """True if `a` and `b` share any run of n identical consecutive words (after normalising)."""
    wa, wb = normalize_for_match(a).split(), normalize_for_match(b).split()
    if len(wa) < n or len(wb) < n:
        return False
    grams = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    return any(tuple(wa[i:i + n]) in grams for i in range(len(wa) - n + 1))
