from __future__ import annotations

import unicodedata

_TYPOGRAPHIC_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }
)


def normalize_for_matching(text: str) -> str:
    """Return a language-neutral Unicode/whitespace/quote normalization.

    This helper is for comparing controller-owned markers and fingerprints.  It
    deliberately contains no vocabulary, stemming, translation, or
    language-specific character rewriting and must not be used as an intent
    classifier.
    """

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.translate(_TYPOGRAPHIC_QUOTE_TRANSLATION).casefold()
    return " ".join(normalized.split())
