"""Guards the rule from CLAUDE.md: every string exists in all three locales.

Adding a key to `en.json` only, and testing in English, is the easy
mistake this catches — the Ukrainian and Russian users would see the raw
key instead of text.
"""

import re
import string

import pytest

from bot.middlewares.i18n import LOCALES, SUPPORTED_LANGUAGES, Translator

REFERENCE = "uk"


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_all_languages_present():
    assert set(LOCALES) == set(SUPPORTED_LANGUAGES)


@pytest.mark.parametrize("language", [lang for lang in SUPPORTED_LANGUAGES if lang != REFERENCE])
def test_key_sets_match(language):
    reference_keys = set(LOCALES[REFERENCE])
    keys = set(LOCALES[language])

    missing = reference_keys - keys
    extra = keys - reference_keys

    assert not missing, f"{language}.json is missing: {sorted(missing)}"
    assert not extra, f"{language}.json has keys absent from {REFERENCE}.json: {sorted(extra)}"


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_placeholders_match(language):
    """A translated string that drops `{count}` breaks formatting at runtime."""
    for key, reference in LOCALES[REFERENCE].items():
        translated = LOCALES[language][key]
        assert _placeholders(reference) == _placeholders(translated), (
            f"placeholder mismatch for {key!r} in {language}.json"
        )


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_no_empty_strings(language):
    empty = [key for key, value in LOCALES[language].items() if not value.strip()]
    assert not empty, f"empty translations in {language}.json: {empty}"


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_html_tags_balanced(language):
    """We send with parse_mode=HTML; an unclosed <b> makes Telegram reject it."""
    for key, value in LOCALES[language].items():
        for tag in ("b", "i", "code"):
            opens = len(re.findall(rf"<{tag}>", value))
            closes = len(re.findall(rf"</{tag}>", value))
            assert opens == closes, f"unbalanced <{tag}> in {key!r} ({language})"


def test_translator_falls_back_to_key():
    i18n = Translator("en")
    assert i18n("this.key.does.not.exist") == "this.key.does.not.exist"


def test_translator_formats():
    i18n = Translator("en")
    assert "7" in i18n("search.found", count=7)
