"""Application configuration loaded from environment / .env.

All secrets stay server-side. Nothing here is ever rendered into templates
except the explicit "is configured" booleans exposed on the settings page.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


def _get(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None else val


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default)).replace(",", "").strip()))
    except (ValueError, AttributeError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _get_list(name: str, default: str) -> List[str]:
    raw = _get(name, default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # Identity / access
    mc_uuid: str = field(default_factory=lambda: _get("MC_UUID").replace("-", "").strip())
    app_password: str = field(default_factory=lambda: _get("APP_PASSWORD"))
    secret_key: str = field(
        default_factory=lambda: _get("SECRET_KEY") or _get("APP_PASSWORD") or "change-me-dev-secret"
    )

    # Data source
    cofl_base_url: str = field(
        default_factory=lambda: _get("COFL_BASE_URL", "https://sky.coflnet.com").rstrip("/")
    )
    check_interval_seconds: int = field(default_factory=lambda: _get_int("CHECK_INTERVAL_SECONDS", 300))

    # Notifications
    discord_webhook: str = field(default_factory=lambda: _get("DISCORD_WEBHOOK"))
    pushover_user_key: str = field(default_factory=lambda: _get("PUSHOVER_USER_KEY"))
    pushover_app_token: str = field(default_factory=lambda: _get("PUSHOVER_APP_TOKEN"))
    pushover_sound: str = field(default_factory=lambda: _get("PUSHOVER_SOUND", "cashregister"))
    pushover_priority: int = field(default_factory=lambda: _get_int("PUSHOVER_PRIORITY", 1))

    # Economics
    ah_tax_rate: float = field(default_factory=lambda: _get_float("AH_TAX_RATE", 0.02))

    # Comparable engine
    relist_comparable_only: bool = field(default_factory=lambda: _get_bool("RELIST_COMPARABLE_ONLY", True))
    relist_comparable_pages: int = field(default_factory=lambda: _get_int("RELIST_COMPARABLE_PAGES", 8))
    relist_min_comparable_matches: int = field(
        default_factory=lambda: _get_int("RELIST_MIN_COMPARABLE_MATCHES", 2)
    )
    relist_min_comparable_score: int = field(
        default_factory=lambda: _get_int("RELIST_MIN_COMPARABLE_SCORE", 75)
    )
    relist_pet_level_tolerance: int = field(
        default_factory=lambda: _get_int("RELIST_PET_LEVEL_TOLERANCE", 5)
    )
    relist_star_tolerance: int = field(default_factory=lambda: _get_int("RELIST_STAR_TOLERANCE", 0))
    relist_gemstone_tolerance: float = field(
        default_factory=lambda: _get_float("RELIST_GEMSTONE_TOLERANCE", 0.80)
    )
    relist_undercut_percent: float = field(
        default_factory=lambda: _get_float("RELIST_UNDERCUT_PERCENT", 0.20)
    )
    relist_undercut_coins: int = field(default_factory=lambda: _get_int("RELIST_UNDERCUT_COINS", 10000))

    # RELIST vs HOLD gap / market thresholds
    relist_price_gap_percent: float = field(
        default_factory=lambda: _get_float("RELIST_PRICE_GAP_PERCENT", 5)
    )
    relist_price_gap_coins: int = field(
        default_factory=lambda: _get_int("RELIST_PRICE_GAP_COINS", 250000)
    )
    relist_decent_volume_per_day: float = field(
        default_factory=lambda: _get_float("RELIST_DECENT_VOLUME_PER_DAY", 3)
    )
    relist_strong_down_trend_24h: float = field(
        default_factory=lambda: _get_float("RELIST_STRONG_DOWN_TREND_24H", -3)
    )
    relist_strong_up_trend_24h: float = field(
        default_factory=lambda: _get_float("RELIST_STRONG_UP_TREND_24H", 5)
    )

    # Carry buy cost to a relisted auction (new UUID, same item)
    relist_carry_enabled: bool = field(default_factory=lambda: _get_bool("RELIST_CARRY_ENABLED", True))
    relist_carry_lookback_days: int = field(
        default_factory=lambda: _get_int("RELIST_CARRY_LOOKBACK_DAYS", 14)
    )
    relist_carry_min_score: int = field(default_factory=lambda: _get_int("RELIST_CARRY_MIN_SCORE", 85))
    relist_carry_auto_apply: bool = field(
        default_factory=lambda: _get_bool("RELIST_CARRY_AUTO_APPLY", False)
    )

    # Profit thresholds
    relist_min_profit_after_tax: int = field(
        default_factory=lambda: _get_int("RELIST_MIN_PROFIT_AFTER_TAX", 250000)
    )
    relist_min_profit_percent_after_tax: float = field(
        default_factory=lambda: _get_float("RELIST_MIN_PROFIT_PERCENT_AFTER_TAX", 2)
    )
    relist_alert_cooldown_minutes: int = field(
        default_factory=lambda: _get_int("RELIST_ALERT_COOLDOWN_MINUTES", 60)
    )
    relist_alert_decisions: List[str] = field(
        default_factory=lambda: _get_list("RELIST_ALERT_DECISIONS", "RELIST,CUT_LOSS,PROFIT_LOW,INCOMPARABLE")
    )

    # Undercut / cheaper comparable alerts. These are advisory-only and use the
    # comparable engine rather than raw same-tag LBIN.
    undercut_alerts: bool = field(default_factory=lambda: _get_bool("UNDERCUT_ALERTS", True))
    undercut_check_enabled: bool = field(default_factory=lambda: _get_bool("UNDERCUT_CHECK_ENABLED", True))
    undercut_min_gap_coins: int = field(default_factory=lambda: _get_int("UNDERCUT_MIN_GAP_COINS", 250000))
    undercut_min_gap_percent: float = field(default_factory=lambda: _get_float("UNDERCUT_MIN_GAP_PERCENT", 3))
    undercut_min_comparable_score: int = field(default_factory=lambda: _get_int("UNDERCUT_MIN_COMPARABLE_SCORE", 75))
    undercut_better_item_score: int = field(default_factory=lambda: _get_int("UNDERCUT_BETTER_ITEM_SCORE", 85))
    undercut_cooldown_minutes: int = field(default_factory=lambda: _get_int("UNDERCUT_COOLDOWN_MINUTES", 60))
    undercut_max_candidates_to_check: int = field(default_factory=lambda: _get_int("UNDERCUT_MAX_CANDIDATES_TO_CHECK", 60))
    undercut_include_possible: bool = field(default_factory=lambda: _get_bool("UNDERCUT_INCLUDE_POSSIBLE", False))
    undercut_notify_decisions: List[str] = field(
        default_factory=lambda: _get_list("UNDERCUT_NOTIFY_DECISIONS", "ACTIVE,HOLD,RELIST,PROFIT_LOW,INCOMPARABLE")
    )

    # Toggles
    # Master notification switch - when false, NO Discord/Pushover messages are sent.
    notifications_enabled: bool = field(default_factory=lambda: _get_bool("NOTIFICATIONS_ENABLED", True))
    # On the first successful sync of a fresh DB, never alert about already-sold auctions.
    first_sync_suppress_sold_alerts: bool = field(
        default_factory=lambda: _get_bool("FIRST_SYNC_SUPPRESS_SOLD_ALERTS", True)
    )
    sold_alerts: bool = field(default_factory=lambda: _get_bool("SOLD_ALERTS", True))
    relist_alerts: bool = field(default_factory=lambda: _get_bool("RELIST_ALERTS", True))
    startup_message: bool = field(default_factory=lambda: _get_bool("STARTUP_MESSAGE", False))

    # Consecutive missed syncs before a vanished ACTIVE auction is marked STALE.
    stale_after_missed_syncs: int = field(
        default_factory=lambda: _get_int("STALE_AFTER_MISSED_SYNCS", 2)
    )

    # Storage
    database_path: str = field(default_factory=lambda: _get("DATABASE_PATH", "data/app.db"))

    # Treat INCOMPARABLE items above this value as "expensive" for alerting.
    incomparable_alert_threshold: int = field(
        default_factory=lambda: _get_int("INCOMPARABLE_ALERT_THRESHOLD", 1000000)
    )

    @property
    def login_required(self) -> bool:
        return bool(self.app_password)

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_webhook)

    @property
    def pushover_configured(self) -> bool:
        return bool(self.pushover_user_key and self.pushover_app_token)

    def auction_url(self, uuid: str) -> str:
        return f"{self.cofl_base_url}/auction/{uuid}"


# Single shared settings instance.
settings = Settings()
