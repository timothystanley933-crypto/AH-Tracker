"""Coin parsing / formatting helpers.

These are intentionally permissive: a human typing buy costs should be able to
write `5,000,000`, `5000000`, `5m`, `500k`, or even `£5,000,000`.
"""
from __future__ import annotations

import re
from typing import Optional

_SUFFIX_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}

# Characters we silently drop before parsing (currency symbols, words, spaces).
_STRIP_CHARS = re.compile(r"[,\s£$€¥]|coins?", re.IGNORECASE)


def parse_coins(value) -> Optional[int]:
    """Parse a human coin amount into an integer number of coins.

    Returns None when the value is empty / cannot be understood.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None

    text = str(value).strip().lower()
    if not text:
        return None

    text = _STRIP_CHARS.sub("", text)
    if not text:
        return None

    multiplier = 1
    if text and text[-1] in _SUFFIX_MULTIPLIERS:
        multiplier = _SUFFIX_MULTIPLIERS[text[-1]]
        text = text[:-1]

    if not text:
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    if number < 0:
        return None

    return int(round(number * multiplier))


def format_coins(value: Optional[int]) -> str:
    """Format coins with thousands separators. None -> em dash."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return "—"


def format_profit(value: Optional[int]) -> str:
    """Format a signed profit value, e.g. +500,000 or -120,000."""
    if value is None:
        return "—"
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        return "—"
    sign = "+" if ivalue >= 0 else "-"
    return f"{sign}{abs(ivalue):,}"


def round_clean_price(price: float) -> int:
    """Round a price to a clean, market-friendly value.

    < 1m       -> nearest 1k
    1m - 10m   -> nearest 10k
    >= 10m     -> nearest 100k
    """
    if price is None:
        return 0
    price = max(0.0, float(price))
    if price < 1_000_000:
        step = 1_000
    elif price < 10_000_000:
        step = 10_000
    else:
        step = 100_000
    return int(round(price / step) * step)
