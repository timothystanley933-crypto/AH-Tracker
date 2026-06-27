"""Offline tests for notification diagnostics + the test-notification endpoint.

No network: the disabled / missing-channel paths return before any HTTP call.
Settings are mutated on the shared instance and restored after each test so the
changes never leak into other test modules.

Run with:  python -m pytest tests/ -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import notifications  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


# Attributes any of these tests may touch; snapshot/restore to isolate tests.
_GUARDED = (
    "app_password",
    "notifications_enabled",
    "sold_alerts",
    "relist_alerts",
    "undercut_alerts",
    "startup_message",
    "first_sync_suppress_sold_alerts",
    "discord_webhook",
    "pushover_user_key",
    "pushover_app_token",
)


def _snapshot():
    return {name: getattr(settings, name) for name in _GUARDED}


def _restore(snap):
    for name, value in snap.items():
        setattr(settings, name, value)


def _client():
    # No APP_PASSWORD => login not required, so /api is reachable without a session.
    settings.app_password = ""
    return TestClient(app)


# --------------------------------------------------------------------------
# Diagnostics redaction
# --------------------------------------------------------------------------

def test_notification_diagnostics_redacts_secrets():
    snap = _snapshot()
    try:
        settings.discord_webhook = "https://discord.com/api/webhooks/999/DISCORDSECRETTOKEN"
        settings.pushover_user_key = "PUSHOVERUSERKEYSECRET"
        settings.pushover_app_token = "PUSHOVERAPPTOKENSECRET"

        diag = notifications.diagnostics()
        blob = json.dumps(diag)

        # The "is configured" booleans are exposed...
        assert diag["discord_configured"] is True
        assert diag["pushover_configured"] is True
        # ...but none of the actual secret values may appear anywhere.
        assert "DISCORDSECRETTOKEN" not in blob
        assert "PUSHOVERUSERKEYSECRET" not in blob
        assert "PUSHOVERAPPTOKENSECRET" not in blob
        # And the diagnostic keys that must be present are present.
        for key in (
            "notifications_enabled",
            "sold_alerts",
            "relist_alerts",
            "undercut_alerts",
            "startup_message",
            "first_sync_suppress_sold_alerts",
            "database_path",
            "check_interval_seconds",
            "last_run",
            "last_stats",
        ):
            assert key in diag
    finally:
        _restore(snap)


# --------------------------------------------------------------------------
# Test-notification endpoint
# --------------------------------------------------------------------------

def test_test_notification_endpoint_reports_disabled():
    snap = _snapshot()
    try:
        settings.notifications_enabled = False
        settings.discord_webhook = "https://discord.com/api/webhooks/1/x"
        settings.pushover_user_key = "u"
        settings.pushover_app_token = "t"

        resp = _client().post("/api/notifications/test")
        assert resp.status_code == 200
        data = resp.json()

        assert data["notifications_enabled"] is False
        assert data["sent_discord"] is False
        assert data["sent_pushover"] is False
        assert data["errors"]  # an explanatory error is returned
        assert any("NOTIFICATIONS_ENABLED" in e for e in data["errors"])
    finally:
        _restore(snap)


def test_test_notification_endpoint_reports_missing_channels():
    snap = _snapshot()
    try:
        settings.notifications_enabled = True
        settings.discord_webhook = ""
        settings.pushover_user_key = ""
        settings.pushover_app_token = ""

        resp = _client().post("/api/notifications/test")
        assert resp.status_code == 200
        data = resp.json()

        assert data["notifications_enabled"] is True
        assert data["discord_configured"] is False
        assert data["pushover_configured"] is False
        assert data["sent_discord"] is False
        assert data["sent_pushover"] is False
        assert any("channel" in e.lower() for e in data["errors"])
    finally:
        _restore(snap)


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
