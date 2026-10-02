"""EN / RU / UZ (Uzbek Latin) translations."""
from __future__ import annotations

import json
from pathlib import Path

from flask import g, request, session

LANGUAGES = {"en": "English", "ru": "Русский", "uz": "O‘zbekcha"}
DEFAULT_LANG = "en"
_DIR = Path(__file__).resolve().parent / "translations"
_TABLES: dict[str, dict[str, str]] = {}


def load():
    for code in LANGUAGES:
        with open(_DIR / f"{code}.json", "r", encoding="utf-8") as fh:
            _TABLES[code] = json.load(fh)


def table(lang: str) -> dict[str, str]:
    if not _TABLES:
        load()
    return {**_TABLES[DEFAULT_LANG], **_TABLES.get(lang, {})}


def translate(key: str, lang: str | None = None, **params) -> str:
    if not _TABLES:
        load()
    lang = lang or getattr(g, "lang", DEFAULT_LANG)
    text = _TABLES.get(lang, {}).get(key) or _TABLES[DEFAULT_LANG].get(key) or key
    if params:
        try:
            text = text.format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def select_language():
    requested = request.args.get("lang")
    if requested in LANGUAGES:
        session["lang"] = requested
    lang = session.get("lang")
    if lang not in LANGUAGES:
        lang = request.accept_languages.best_match(list(LANGUAGES)) or DEFAULT_LANG
    g.lang = lang
