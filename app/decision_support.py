"""Pure decision-support computations used by the analysis engine and the UI.

Everything here is read-only and side-effect free (no DB, no network) so it is
trivially testable: price rank, undercut amount/percent, price-wall detection,
liquidity / demand / competition scores, market-trend label, and a plain-English
explanation of what lowered confidence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .config import settings


# --------------------------------------------------------------------------
# Rank / undercut
# --------------------------------------------------------------------------

def price_rank(price: Optional[int], prices: List[int]) -> Tuple[Optional[int], int]:
    """1-based rank of ``price`` among ``prices`` (cheapest first) and the total.

    Example: rank 12 of 43 -> "#12 cheapest out of 43".
    """
    valid = sorted(p for p in prices if p and p > 0)
    if not valid:
        return None, 0
    if not price or price <= 0:
        return None, len(valid)
    rank = sum(1 for p in valid if p < price) + 1
    return rank, len(valid)


def undercut_amount(my_price: Optional[int], cheapest: Optional[int]) -> Tuple[int, float]:
    """Coins and percent the cheapest comparable sits below ``my_price``."""
    if not my_price or my_price <= 0 or not cheapest or cheapest <= 0:
        return 0, 0.0
    gap = my_price - cheapest
    pct = round(gap / my_price * 100.0, 1) if my_price else 0.0
    return gap, pct


# --------------------------------------------------------------------------
# Price walls
# --------------------------------------------------------------------------

def detect_price_walls(
    prices: List[int],
    window_percent: Optional[float] = None,
    min_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Find clusters of listings packed within ``window_percent`` of each other.

    A wall is ``min_count`` or more listings whose prices all fall within
    ``window_percent`` of the cluster's lowest price. Returns non-overlapping
    walls, cheapest first. Used to explain why an item may not sell.
    """
    window = settings.price_wall_window_percent if window_percent is None else window_percent
    need = settings.price_wall_min_listings if min_count is None else min_count
    valid = sorted(p for p in prices if p and p > 0)

    walls: List[Dict[str, Any]] = []
    i = 0
    n = len(valid)
    while i < n:
        base = valid[i]
        hi = base * (1.0 + window / 100.0)
        j = i
        while j < n and valid[j] <= hi:
            j += 1
        cluster = valid[i:j]
        if len(cluster) >= need:
            walls.append(
                {
                    "price": base,
                    "count": len(cluster),
                    "low": cluster[0],
                    "high": cluster[-1],
                    "window_percent": window,
                }
            )
            i = j  # skip the whole cluster (non-overlapping walls)
        else:
            i += 1
    return walls


# --------------------------------------------------------------------------
# Scores (0-100 with a Low / Medium / High band)
# --------------------------------------------------------------------------

def _band(score: int) -> str:
    if score >= 67:
        return "High"
    if score >= 34:
        return "Medium"
    return "Low"


def _scored(value: int) -> Dict[str, Any]:
    value = max(0, min(100, int(round(value))))
    return {"score": value, "label": _band(value)}


def liquidity_score(volume_per_day: Optional[float], comparable_count: int) -> Dict[str, Any]:
    """How easily the item trades: sales/day plus available comparables."""
    if volume_per_day is None:
        return {"score": 0, "label": "Unknown"}
    vol = min(1.0, max(0.0, volume_per_day) / 10.0)        # ~10 sales/day -> full
    comps = min(1.0, max(0, comparable_count) / 8.0)        # ~8 comparables -> full
    return _scored(100 * (0.75 * vol + 0.25 * comps))


def demand_score(volume_per_day: Optional[float], trend: Dict[str, Any]) -> Dict[str, Any]:
    """Demand / velocity: sales/day, nudged by the recent price trend."""
    if volume_per_day is None:
        return {"score": 0, "label": "Unknown"}
    base = min(1.0, max(0.0, volume_per_day) / 8.0)
    day = (trend or {}).get("day_pct")
    nudge = 0.0
    if day is not None:
        nudge = max(-0.2, min(0.2, day / 50.0))            # +/-20% from trend
    return _scored(100 * max(0.0, min(1.0, base + nudge)))


def competition_score(cheaper_count: int, total_listings: int, walls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How crowded the market is: cheaper similar listings + price walls."""
    if total_listings <= 0:
        return {"score": 0, "label": "Unknown"}
    crowd = min(1.0, cheaper_count / max(1, total_listings))
    wall_pressure = min(0.4, 0.1 * len(walls))
    return _scored(100 * min(1.0, crowd + wall_pressure))


# --------------------------------------------------------------------------
# Trend label / confidence explanation
# --------------------------------------------------------------------------

def trend_label(trend: Dict[str, Any]) -> str:
    """Summarise a trend dict as rising / falling / stable / volatile / unknown."""
    if not trend:
        return "unknown"
    volatility = trend.get("volatility")
    if volatility is not None and volatility >= 25:
        return "volatile"
    ref = trend.get("day_pct")
    if ref is None:
        ref = trend.get("week_pct")
    if ref is None:
        return "unknown"
    if ref >= 3:
        return "rising"
    if ref <= -3:
        return "falling"
    return "stable"


def _features_incomplete(features: Dict[str, Any]) -> bool:
    if not features:
        return True
    if features.get("is_pet"):
        pet = features.get("pet") or {}
        return pet.get("level") is None and not pet.get("tier")
    return not any(
        features.get(k)
        for k in ("rarity", "stars", "recombobulated", "attributes", "important_enchants", "skin")
    )


def confidence_explanation(
    *,
    comparable_count: int,
    features: Dict[str, Any],
    volume_per_day: Optional[float],
    trend: Dict[str, Any],
) -> List[str]:
    """Plain-English reasons confidence is lower than it could be."""
    notes: List[str] = []
    if comparable_count < max(2, settings.relist_min_comparable_matches):
        notes.append("Few comparable listings")
    if _features_incomplete(features):
        notes.append("Missing item NBT / features")
    if trend and trend.get("volatility") is not None and trend["volatility"] >= 25:
        notes.append("Volatile market")
    if volume_per_day is not None and volume_per_day < 1:
        notes.append("Low sales volume")
    return notes
