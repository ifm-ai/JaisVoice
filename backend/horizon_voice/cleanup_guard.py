"""Conservative lexical checks for deletion-only transcript refinement.

This is a rejection guard, not a semantic validator or a second corrector. It
never guesses replacement wording. A passing result can still be semantically
wrong; inflection, translation and reordered words may be rejected deliberately.
"""
from __future__ import annotations

from collections import Counter
import re
import unicodedata


def normalized_words(text: str) -> list[str]:
    # NFKD handles accented French and Arabic hamza/harakat consistently. Leave
    # numbers and negations as content; punctuation and case are immaterial.
    text = "".join(character for character in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.category(character).startswith("M") and character != "ـ")
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


_FUNCTION_WORDS = set(normalized_words(
    "a an the to at in on of for from with and or i you he she it we they "
    "le la les un une des du de à au aux en et ou pour dans par sur je tu il elle nous vous ils elles "
    "ce cet cette ces mon ma mes ton ta tes son sa ses "
    "في إلى من عن على و ف ب ل أن أنا أنت هو هي نحن هم هذا هذه ذلك تلك"
))
_REPAIR_MARKERS = tuple(tuple(normalized_words(marker)) for marker in (
    "عفوا أقصد", "أقصد", "بل", "je rectifie", "I mean",
))
_NEGATIONS = set(normalized_words("لا ليس ليست لن لم ما مو مش not never no cannot non pas jamais"))
_NEGATIVE_CONTRACTIONS = {"don", "doesn", "didn", "can", "couldn", "won", "wouldn", "shouldn", "isn", "aren", "wasn", "weren", "haven", "hasn", "hadn", "mustn"}


def _content(words: list[str]) -> list[str]:
    return [word for word in words if word not in _FUNCTION_WORDS]


def _negation_signals(words: list[str]) -> set[str]:
    signals = set(words) & _NEGATIONS
    # Apostrophe variants are normalized to token boundaries, so both don't
    # and don’t become the adjacent tokens "don", "t".
    signals.update(first + "'t" for first, second in zip(words, words[1:]) if first in _NEGATIVE_CONTRACTIONS and second == "t")
    return signals


def cleanup_problem(original: str, cleaned: str) -> str | None:
    """Return a rejection reason, or None when these limited checks pass.

    Bare ``لا``, ``non``, ``pas`` and ``not`` are never repair markers. The
    tail after the LAST explicit marker must remain in order, so overwritten
    earlier choices may disappear while the final choice/instructions survive.
    """
    source, result = normalized_words(original), normalized_words(cleaned)
    source_content, result_content = _content(source), _content(result)
    if Counter(result_content) - Counter(source_content):
        return "added_content"
    final_end = None
    for index in range(len(source)):
        for marker in _REPAIR_MARKERS:
            if tuple(source[index:index + len(marker)]) == marker:
                final_end = max(final_end or 0, index + len(marker))
    if final_end is not None:
        required = _content(source[final_end:])
        if not required:
            return "unfinished_repair"
        remaining = iter(result_content)
        if any(not any(word == candidate for candidate in remaining) for word in required):
            return "missing_final_repair"
    elif _negation_signals(source) - _negation_signals(result):
        # A bare negative is not a repair cue. Preserve its presence, but allow
        # a repeated/stuttered occurrence to be deleted. Arabic ما can have a
        # nonnegative use; rejecting its loss is intentionally conservative.
        return "missing_negation"
    return None
