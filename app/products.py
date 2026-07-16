import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PRODUCT_ALIASES_PATH = Path(__file__).resolve().parents[1] / "config" / "product_aliases.yaml"


def _lookup_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


@lru_cache(maxsize=8)
def load_product_aliases(path: str = str(DEFAULT_PRODUCT_ALIASES_PATH)) -> dict[str, tuple[str, ...]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    products = payload.get("products") or []
    aliases: dict[str, tuple[str, ...]] = {}
    seen: dict[str, str] = {}
    for item in products:
        canonical = str(item.get("code") or "").strip()
        if not canonical:
            raise ValueError("product alias entry requires code")
        values = tuple(dict.fromkeys([canonical, *(str(value).strip() for value in item.get("aliases") or [])]))
        for value in values:
            key = _lookup_key(value)
            if not key:
                raise ValueError(f"empty product alias for {canonical}")
            prior = seen.get(key)
            if prior is not None and prior != canonical:
                raise ValueError(f"product alias collision: {value} maps to both {prior} and {canonical}")
            seen[key] = canonical
        aliases[canonical] = values
    return aliases


@lru_cache(maxsize=8)
def _alias_index(path: str = str(DEFAULT_PRODUCT_ALIASES_PATH)) -> dict[str, str]:
    return {
        _lookup_key(value): canonical
        for canonical, values in load_product_aliases(path).items()
        for value in values
    }


def canonical_product_code(value: str | None, path: str = str(DEFAULT_PRODUCT_ALIASES_PATH)) -> str:
    stripped = str(value or "").strip()
    if not stripped:
        return ""
    return _alias_index(path).get(_lookup_key(stripped), stripped.upper())


def product_codes_match(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return canonical_product_code(left) == canonical_product_code(right)


def find_product_codes(text: str, path: str = str(DEFAULT_PRODUCT_ALIASES_PATH)) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for canonical, values in load_product_aliases(path).items():
        for value in values:
            for match in re.finditer(
                rf"(?<![A-Z0-9]){re.escape(value)}(?![A-Z0-9])",
                text,
                flags=re.IGNORECASE,
            ):
                matches.append((match.start(), match.end(), canonical))
    # Prefer the longest alias at the same/overlapping position. This keeps
    # "N823(99%)" from also being interpreted as the shorter N823 -> 98% alias.
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    accepted: list[tuple[int, int, str]] = []
    for start, end, canonical in matches:
        if any(start < prior_end and end > prior_start for prior_start, prior_end, _ in accepted):
            continue
        accepted.append((start, end, canonical))
    return list(dict.fromkeys(canonical for _, _, canonical in accepted))


def find_product_code(text: str, path: str = str(DEFAULT_PRODUCT_ALIASES_PATH)) -> str | None:
    codes = find_product_codes(text, path)
    return codes[0] if codes else None


def product_text_key(code: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", canonical_product_code(code).casefold()).strip("_")
