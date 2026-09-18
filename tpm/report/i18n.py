"""Report i18n: JSON dictionaries in tpm/report/i18n/<lang>.json. Missing keys fall back to English, then to the key."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

I18N_DIR = Path(__file__).resolve().parent / "i18n"
DEFAULT_LANG = "en"


def available_languages() -> list[str]:
    return sorted(p.stem for p in I18N_DIR.glob("*.json"))


@lru_cache(maxsize=8)
def _load(lang: str) -> dict[str, str]:
    p = I18N_DIR / f"{lang}.json"
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_lang(lang: str | None) -> str:
    lang = (lang or DEFAULT_LANG).lower().strip()[:2]
    return lang if (I18N_DIR / f"{lang}.json").exists() else DEFAULT_LANG


class Translator:
    """t("key", n=3) -> translated string with {n} substituted; unknown keys return the key itself."""

    def __init__(self, lang: str):
        self.lang = normalize_lang(lang)
        self._d = _load(self.lang)
        self._en = _load(DEFAULT_LANG)

    def __call__(self, key: str, **kwargs: Any) -> str:
        s = self._d.get(key) or self._en.get(key) or key
        if kwargs:
            try:
                return s.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return s
        return s

    def has(self, key: str) -> bool:
        return key in self._d or key in self._en

    def role(self, role: str | None) -> str:
        return self(f"role_{role}") if role and self.has(f"role_{role}") else (role or self("unknown"))

    def cause(self, cause: str | None) -> str:
        return self(f"cause_{cause}") if cause and self.has(f"cause_{cause}") else (cause or self("unknown"))

    def kind(self, kind: str | None) -> str:
        return self(f"kind_{kind}") if kind and self.has(f"kind_{kind}") else (kind or self("unknown"))

    def cat(self, cat: str | None) -> str:
        return self(f"cat_{cat}") if cat and self.has(f"cat_{cat}") else (cat or self("unknown"))

    def act(self, action: str | None) -> str:
        return self(f"act_{action}") if action and self.has(f"act_{action}") else (action or "")

    def verdict(self, v: str | None) -> str:
        return self(f"verdict_{v}") if v and self.has(f"verdict_{v}") else (v or self("unknown"))

    def status(self, s: str | None) -> str:
        return self(s) if s in ("pass", "warn", "fail") else (s or "")
