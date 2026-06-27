"""Central, fee-aware profit math — the single source of truth for tax/fee formulas.

Two distinct charges apply on the Hypixel auction house:
  * sales tax   - taken from the SALE price when an item actually sells (1%).
  * listing fee - taken UP FRONT every time you list / relist an item (2.5%).

True profit therefore deducts: the buy cost, sales tax, EVERY listing fee already
paid (accumulated across all relists), any NEW relist fee (only when relisting),
and manual extra costs. Nothing here assumes a single relist.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import db
from .config import settings
from .formatting import format_coins, format_profit, round_clean_price


# --------------------------------------------------------------------------
# Row access (works for sqlite3.Row and plain dict)
# --------------------------------------------------------------------------

def _field(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        if isinstance(row, dict):
            val = row.get(key, default)
        else:
            val = row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        val = default
    return default if val is None else val


# --------------------------------------------------------------------------
# Atomic charges
# --------------------------------------------------------------------------

def sales_tax(price: Optional[int]) -> int:
    """Sales tax taken from the sale price when an item sells."""
    if not price or price <= 0:
        return 0
    return int(round(price * settings.ah_sales_tax_rate))


def listing_fee(price: Optional[int]) -> int:
    """Up-front fee charged each time an item is listed / relisted."""
    if not price or price <= 0:
        return 0
    return int(round(price * settings.ah_listing_fee_rate))


def total_listing_fees_for_auction(uuid: str) -> int:
    """All listing fees recorded against an auction (authoritative, from the ledger)."""
    return db.total_listing_fees_for_auction(uuid)


# --------------------------------------------------------------------------
# Profit
# --------------------------------------------------------------------------

def profit_if_current_sells(row: Any) -> Optional[int]:
    """True profit if the CURRENT listing sells at its current price.

    No new relist fee is added — the item is already listed, so that fee is part
    of the accumulated fees already paid.
    """
    buy = _field(row, "buy_cost")
    if buy is None:
        return None
    sale = int(_field(row, "listing_price", 0) or 0)
    acc = int(_field(row, "accumulated_listing_fees", 0) or 0)
    manual = int(_field(row, "manual_extra_costs", 0) or 0)
    return int(sale - buy - sales_tax(sale) - acc - manual)


def profit_after_relist(row: Any, suggested_price: Optional[int]) -> Optional[int]:
    """True profit if the item is relisted at ``suggested_price`` and then sells.

    Adds a NEW listing fee for the relist on top of fees already paid.
    """
    buy = _field(row, "buy_cost")
    if buy is None or not suggested_price or suggested_price <= 0:
        return None
    acc = int(_field(row, "accumulated_listing_fees", 0) or 0)
    manual = int(_field(row, "manual_extra_costs", 0) or 0)
    return int(
        suggested_price
        - buy
        - sales_tax(suggested_price)
        - acc
        - listing_fee(suggested_price)
        - manual
    )


def profit_breakdown(row: Any, sale_price: Optional[int], include_new_relist_fee: bool = False) -> Dict[str, Any]:
    """Full, line-by-line profit breakdown for display / notifications."""
    sale = int(sale_price or 0)
    buy = _field(row, "buy_cost")
    acc = int(_field(row, "accumulated_listing_fees", 0) or 0)
    manual = int(_field(row, "manual_extra_costs", 0) or 0)
    relist_count = int(_field(row, "relist_count", 0) or 0)
    tax = sales_tax(sale)
    new_fee = listing_fee(sale) if include_new_relist_fee else 0
    true_profit = None
    if buy is not None:
        true_profit = int(sale - buy - tax - acc - new_fee - manual)
    return {
        "sale_price": sale,
        "buy_cost": buy,
        "sales_tax": tax,
        "sales_tax_rate": settings.ah_sales_tax_rate,
        "listing_fees_paid": acc,
        "new_relist_fee": int(new_fee),
        "listing_fee_rate": settings.ah_listing_fee_rate,
        "manual_extra_costs": manual,
        "relist_count": relist_count,
        "includes_new_relist_fee": bool(include_new_relist_fee),
        "true_profit": true_profit,
    }


# --------------------------------------------------------------------------
# Suggested relist price options (Fast / Balanced / Greedy)
# --------------------------------------------------------------------------

def fee_aware_lines(row: Any, *, sale_price: Optional[int], relist_price: Optional[int] = None) -> list:
    """Notification lines describing fee-aware profit. Empty when no buy cost.

    Always states the sales-tax and listing-fee rates so the recipient can see
    the true profit already nets out every fee.
    """
    if _field(row, "buy_cost") is None:
        return []
    cur = profit_breakdown(row, sale_price, include_new_relist_fee=False)
    out = [f"Profit if current sells: {format_profit(cur['true_profit'])}"]
    if relist_price:
        rb = profit_breakdown(row, relist_price, include_new_relist_fee=True)
        out.append(f"Profit after relist to {format_coins(relist_price)}: {format_profit(rb['true_profit'])}")
    out.append(f"Relists counted: {cur['relist_count']}")
    out.append(f"Listing fees paid: {format_coins(cur['listing_fees_paid'])}")
    out.append(
        f"Sales tax: {cur['sales_tax_rate'] * 100:g}% · Listing fee: {cur['listing_fee_rate'] * 100:g}%"
    )
    return out


def _attractive_price(value: float) -> int:
    """A clean, psychological price just under a round number (e.g. 224,999,999)."""
    clean = round_clean_price(value)
    return max(1, clean - 1)


def build_relist_options(row: Any, cheapest_comparable: Optional[int]) -> list:
    """Fast / Balanced / Greedy relist prices, each with fee-aware profit.

    Fast undercuts the most (sells fastest, lowest price); Greedy undercuts the
    least (highest price, slowest). Every option's profit already deducts sales
    tax, accumulated listing fees, the new relist fee, and manual extra costs.
    """
    if not cheapest_comparable or cheapest_comparable <= 0:
        return []
    specs = (("Fast", 0.015), ("Balanced", 0.006), ("Greedy", 0.001))
    options = []
    for name, frac in specs:
        price = _attractive_price(cheapest_comparable * (1.0 - frac))
        profit = profit_after_relist(row, price)
        options.append(
            {
                "name": name,
                "price": price,
                "price_fmt": format_coins(price),
                "profit": profit,
                "profit_fmt": format_profit(profit) if profit is not None else "—",
            }
        )
    return options
