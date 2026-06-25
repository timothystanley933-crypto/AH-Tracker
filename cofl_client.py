"""Read-only client for the public SkyCofl / CoflNet HTTP API.

This module ONLY reads market data. It never authenticates, never posts to
Hypixel, and never performs any auction action. All endpoints are GET requests.

Defensive by design: every call returns a safe default (None / []) on failure
so a single bad response cannot crash a sync or analysis run.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

log = logging.getLogger("cofl")

# Limit concurrent detail fetches so we never hammer the API.
_semaphore = asyncio.Semaphore(4)

# Simple in-process TTL cache: key -> (expires_at, value)
_cache: Dict[str, tuple[float, Any]] = {}

_DETAIL_TTL = 120  # seconds
_BIN_TTL = 90
_HISTORY_TTL = 600

_REQUEST_TIMEOUT = 15.0
_MAX_RETRIES = 2


def _cache_get(key: str) -> Optional[Any]:
    item = _cache.get(key)
    if not item:
        return None
    expires_at, value = item
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl: float) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


async def _get_json(path: str, *, params: Optional[dict] = None) -> Optional[Any]:
    """GET a JSON resource with retries. Returns parsed JSON or None."""
    url = f"{settings.cofl_base_url}{path}"
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with _semaphore:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.get(
                        url,
                        params=params,
                        headers={"Accept": "application/json", "User-Agent": "skycofl-relist-dashboard"},
                    )
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                # Rate limited - back off briefly and retry.
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow & retry
            last_err = exc
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.6 * (attempt + 1))
    log.warning("API request failed: %s (%s)", path, last_err)
    return None


async def get_player_auctions(uuid: str, page: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Fetch one page of a player's auctions.

    Returns a list (possibly empty) on a successful response, or None when the
    request failed. Distinguishing the two matters: a failed fetch must NOT make
    the sync think every auction has disappeared.
    """
    data = await _get_json(f"/api/player/{uuid}/auctions", params={"page": page})
    if data is None:
        return None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        # Some deployments wrap results.
        for key in ("auctions", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [d for d in inner if isinstance(d, dict)]
    return []


async def get_all_player_auctions(uuid: str, max_pages: int = 5) -> Optional[List[Dict[str, Any]]]:
    """Page through a player's auctions.

    Returns the combined list on success, or None if the very first page failed
    (i.e. we could not confirm the player's auction state this cycle).
    """
    out: List[Dict[str, Any]] = []
    for page in range(max_pages):
        page_items = await get_player_auctions(uuid, page=page)
        if page_items is None:
            if page == 0:
                return None  # could not confirm state at all
            break  # a later page failed; use what we have
        if not page_items:
            break
        out.extend(page_items)
        if len(page_items) < 10:  # heuristic: short page == last page
            break
    return out


async def get_auction_detail(auction_uuid: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch full auction detail (cached briefly)."""
    cache_key = f"detail:{auction_uuid}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    data = await _get_json(f"/api/auction/{auction_uuid}")
    if isinstance(data, dict):
        _cache_set(cache_key, data, _DETAIL_TTL)
        return data
    return None


async def get_active_bins(item_tag: str, page: int = 0, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Fetch one page of active BIN listings for a tag."""
    cache_key = f"bin:{item_tag}:{page}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    data = await _get_json(f"/api/auctions/tag/{item_tag}/active/bin", params={"page": page})
    result: List[Dict[str, Any]] = []
    if isinstance(data, list):
        result = [d for d in data if isinstance(d, dict)]
    elif isinstance(data, dict):
        for key in ("auctions", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                result = [d for d in inner if isinstance(d, dict)]
                break
    _cache_set(cache_key, result, _BIN_TTL)
    return result


async def get_active_bins_pages(item_tag: str, pages: int) -> List[Dict[str, Any]]:
    """Fetch several pages of active BINs (sequential to be gentle on the API)."""
    out: List[Dict[str, Any]] = []
    for page in range(max(1, pages)):
        items = await get_active_bins(item_tag, page=page)
        if not items:
            break
        out.extend(items)
    return out


async def get_price_history(item_tag: str, span: str = "day") -> List[Dict[str, Any]]:
    """Fetch price history points. span in {day, week}."""
    span = "week" if span == "week" else "day"
    cache_key = f"hist:{item_tag}:{span}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = await _get_json(f"/api/item/price/{item_tag}/history/{span}")
    result: List[Dict[str, Any]] = []
    if isinstance(data, list):
        result = [d for d in data if isinstance(d, dict)]
    _cache_set(cache_key, result, _HISTORY_TTL)
    return result
