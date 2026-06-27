"""FastAPI application: routes, auth, templates, background loop wiring."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import analysis, auth, carry, db, notifications, scheduler, undercut, views
from .config import settings
from .formatting import format_coins, format_profit, parse_coins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("app")

_background_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Database ready at %s", settings.database_path)
    if settings.startup_message:
        try:
            await notifications.send_startup_message()
        except Exception as exc:  # noqa: BLE001
            log.warning("Startup message failed: %s", exc)
    global _background_task
    _background_task = asyncio.create_task(scheduler.background_loop())
    log.info("SkyCofl Smart Relist Dashboard started.")
    try:
        yield
    finally:
        scheduler.stop()
        if _background_task:
            _background_task.cancel()


app = FastAPI(title="SkyCofl Smart Relist Dashboard", lifespan=lifespan)


# --- Auth middleware (added BEFORE SessionMiddleware so session is outermost) ---
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (
        not settings.login_required
        or path.startswith("/static")
        or path in ("/login", "/logout", "/healthz")
    ):
        return await call_next(request)
    if not auth.is_authenticated(request):
        if path.startswith("/api"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="skycofl_session",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["coins"] = format_coins
templates.env.filters["profit"] = format_profit


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _read_value(request: Request, form_value: Optional[str]) -> Optional[str]:
    """Read a 'value' field from form OR JSON body."""
    if form_value is not None:
        return form_value
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body.get("value")
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not settings.login_required or auth.is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form("")):
    if auth.check_password(password):
        auth.login(request)
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Incorrect password."}, status_code=401
    )


@app.get("/logout")
async def logout(request: Request):
    auth.logout(request)
    return RedirectResponse(url="/login", status_code=303)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, filter: str = "active", sort: str = "recent"):
    rows = db.list_auctions(include_inactive=True)
    analyses = db.latest_analyses_map()
    cards = views.build_cards(rows, analyses)
    views.attach_undercuts(cards, db.latest_undercuts_map())
    carry_suggestions = carry.pending_for_cards()
    for card in cards:
        if card["missing_buy_cost"]:
            card["carry_suggestions"] = carry_suggestions.get(card["uuid"], [])
    summary = views.compute_summary(cards, scheduler.last_run)

    visible = views.filter_cards(cards, filter)
    # The Sold tab always shows newest sold first, regardless of the sort dropdown.
    if filter == "sold":
        visible = views.sort_sold(visible)
    else:
        visible = views.sort_cards(visible, sort)

    for card in visible:
        if card["missing_buy_cost"] and not card.get("carry_suggestions"):
            try:
                card["carry_suggestions"] = await carry.get_suggestions(card["uuid"])
            except Exception as exc:  # noqa: BLE001
                log.warning("carry suggestion build failed for %s: %s", card["uuid"], exc)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "cards": visible,
            "summary": summary,
            "filter": filter,
            "sort": sort,
            "settings": settings,
            "min_profit_default": settings.relist_min_profit_after_tax,
        },
    )


@app.get("/auction/{uuid}", response_class=HTMLResponse)
async def auction_detail(request: Request, uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return RedirectResponse(url="/", status_code=303)

    analysis_row = db.latest_analysis(uuid)
    card = views.build_card(row, analysis_row)

    features = comparables = rejected = trend = {}
    if analysis_row:
        try:
            features = json.loads(analysis_row["item_features_json"] or "{}")
        except (ValueError, TypeError):
            features = {}
        try:
            comparables = json.loads(analysis_row["comparable_prices_json"] or "[]")
        except (ValueError, TypeError):
            comparables = []
        try:
            rejected = json.loads(analysis_row["rejected_json"] or "[]")
        except (ValueError, TypeError):
            rejected = []
        try:
            trend = json.loads(analysis_row["trend_json"] or "{}")
        except (ValueError, TypeError):
            trend = {}

    history = db.analysis_history(uuid, limit=10)
    notifs = db.notification_history(uuid, limit=20)
    carry_link = db.get_accepted_carry_link(uuid)
    undercut_history = db.undercut_history(uuid, limit=20)

    return templates.TemplateResponse(
        "auction_detail.html",
        {
            "request": request,
            "card": card,
            "row": row,
            "features": features,
            "comparables": comparables,
            "rejected": rejected,
            "trend": trend,
            "history": history,
            "notifications": notifs,
            "carry_link": carry_link,
            "undercut_history": undercut_history,
            "settings": settings,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    info = {
        "mc_uuid_set": bool(settings.mc_uuid),
        "mc_uuid_masked": (settings.mc_uuid[:8] + "…") if settings.mc_uuid else "(not set)",
        "login_required": settings.login_required,
        "discord_configured": settings.discord_configured,
        "pushover_configured": settings.pushover_configured,
        "cofl_base_url": settings.cofl_base_url,
        "check_interval_seconds": settings.check_interval_seconds,
        "ah_tax_rate": settings.ah_tax_rate,
        "comparable_only": settings.relist_comparable_only,
        "comparable_pages": settings.relist_comparable_pages,
        "min_comparable_matches": settings.relist_min_comparable_matches,
        "min_comparable_score": settings.relist_min_comparable_score,
        "pet_level_tolerance": settings.relist_pet_level_tolerance,
        "star_tolerance": settings.relist_star_tolerance,
        "min_profit_after_tax": settings.relist_min_profit_after_tax,
        "alert_decisions": ", ".join(settings.relist_alert_decisions),
        "alert_cooldown_minutes": settings.relist_alert_cooldown_minutes,
        "sold_alerts": settings.sold_alerts,
        "relist_alerts": settings.relist_alerts,
        "undercut_alerts": settings.undercut_alerts,
        "startup_message": settings.startup_message,
        "first_sync_suppress_sold_alerts": settings.first_sync_suppress_sold_alerts,
        "database_path": settings.database_path,
        "last_run": scheduler.last_run,
        "last_stats": scheduler.last_stats,
    }
    return templates.TemplateResponse(
        "settings.html", {"request": request, "info": info, "settings": settings}
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/auctions")
async def api_auctions():
    rows = db.list_auctions(include_inactive=True)
    analyses = db.latest_analyses_map()
    cards = views.build_cards(rows, analyses)
    summary = views.compute_summary(cards, scheduler.last_run)
    return {"summary": summary, "auctions": cards}


@app.post("/api/auctions/{uuid}/buy-cost")
async def api_set_buy_cost(uuid: str, request: Request, value: Optional[str] = Form(None)):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await _read_value(request, value)
    if raw is None or str(raw).strip() == "":
        db.set_buy_cost(uuid, None)
        return {"ok": True, "buy_cost": None, "buy_cost_fmt": ""}
    parsed = parse_coins(raw)
    if parsed is None:
        return JSONResponse({"error": "could not parse coin value"}, status_code=400)
    db.set_buy_cost(uuid, parsed)
    return {"ok": True, "buy_cost": parsed, "buy_cost_fmt": format_coins(parsed)}


@app.post("/api/auctions/{uuid}/min-profit")
async def api_set_min_profit(uuid: str, request: Request, value: Optional[str] = Form(None)):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await _read_value(request, value)
    parsed = parse_coins(raw)
    if parsed is None:
        parsed = settings.relist_min_profit_after_tax
    db.set_min_profit(uuid, parsed)
    return {"ok": True, "min_profit": parsed, "min_profit_fmt": format_coins(parsed)}


@app.post("/api/auctions/{uuid}/notes")
async def api_set_notes(uuid: str, request: Request, value: Optional[str] = Form(None)):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await _read_value(request, value)
    db.set_notes(uuid, (raw or "").strip())
    return {"ok": True}


@app.post("/api/auctions/{uuid}/analyse")
async def api_analyse(uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    result = await analysis.analyse_auction(uuid)
    if result is None:
        return JSONResponse({"error": "analysis failed"}, status_code=500)
    # Fire a decision alert if it qualifies (manual analyse can still notify).
    try:
        fresh = db.get_auction(uuid)
        if fresh is not None:
            await notifications.notify_decision(fresh, result)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify after analyse failed: %s", exc)
    return {
        "ok": True,
        "decision": result.decision,
        "suggested_price": result.suggested_price,
        "expected_profit": result.expected_profit,
        "confidence": result.confidence,
        "comparable_count": result.comparable_count,
        "reasons": result.reasons,
    }


@app.get("/api/auctions/{uuid}/carry-suggestions")
async def api_carry_suggestions(uuid: str, include_manual: bool = False):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    suggestions = await carry.get_suggestions(uuid, include_manual=include_manual)
    return {"auction_uuid": uuid, "suggestions": suggestions}


@app.post("/api/auctions/{uuid}/carry/{old_uuid}")
async def api_carry(uuid: str, old_uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    result = await carry.carry(uuid, old_uuid)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "carry failed")}, status_code=400)
    return result


@app.post("/api/auctions/{uuid}/carry-ignore")
async def api_carry_ignore(uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    carry.ignore(uuid)
    return {"ok": True, "ignored": True}


@app.post("/api/auctions/{uuid}/check-undercut")
async def api_check_undercut(uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    result = await undercut.check_auction(uuid, notify=False)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "undercut check failed")}, status_code=400)
    return result


@app.get("/api/auctions/{uuid}/undercuts")
async def api_undercuts(uuid: str):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    latest = db.latest_undercut_for_auction(uuid)
    history = db.undercut_history(uuid, limit=20)
    return {
        "auction_uuid": uuid,
        "latest": dict(latest) if latest else None,
        "history": [dict(r) for r in history],
    }


@app.post("/api/auctions/sync")
async def api_sync():
    stats = await scheduler.run_once()
    return {"ok": True, "stats": stats}


@app.post("/api/notifications/test")
async def api_test_notification():
    """Send a test notification through the real notification system.

    Requires normal dashboard auth (enforced by the auth middleware for /api).
    Returns a secret-free diagnostics payload proving whether this service can
    reach Discord / Pushover and which alert toggles are active.
    """
    result = await notifications.send_test_notification()
    return result


@app.post("/api/auctions/{uuid}/ignore")
async def api_ignore(uuid: str, request: Request, ignored: Optional[str] = Form(None)):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await _read_value(request, ignored)
    if raw is None:
        new_state = not bool(row["ignored"])  # toggle
    else:
        new_state = str(raw).lower() in ("1", "true", "yes", "on")
    db.set_ignored(uuid, new_state)
    return {"ok": True, "ignored": new_state}


@app.post("/api/auctions/{uuid}/sold")
async def api_sold(uuid: str, request: Request, sold_price: Optional[str] = Form(None)):
    row = db.get_auction(uuid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await _read_value(request, sold_price)
    price = parse_coins(raw) if raw else None
    db.mark_sold(uuid, price)
    fresh = db.get_auction(uuid)
    if fresh is not None and settings.sold_alerts:
        try:
            await notifications.notify_sold(fresh, price or fresh["listing_price"])
        except Exception as exc:  # noqa: BLE001
            log.warning("sold notify failed: %s", exc)
    return {"ok": True, "sold": True}


@app.get("/api/auctions/{uuid}/analysis")
async def api_get_analysis(uuid: str):
    analysis_row = db.latest_analysis(uuid)
    if analysis_row is None:
        return JSONResponse({"error": "no analysis yet"}, status_code=404)
    return {
        "decision": analysis_row["decision"],
        "suggested_price": analysis_row["suggested_price"],
        "expected_profit": analysis_row["expected_profit"],
        "confidence": analysis_row["confidence"],
        "comparable_count": analysis_row["comparable_count"],
        "comparable_prices": json.loads(analysis_row["comparable_prices_json"] or "[]"),
        "reasons": json.loads(analysis_row["reasons_json"] or "[]"),
        "trend": json.loads(analysis_row["trend_json"] or "{}"),
        "volume_per_day": analysis_row["volume_per_day"],
        "sell_estimate": json.loads((analysis_row["sell_estimate_json"] if "sell_estimate_json" in analysis_row.keys() else None) or "{}"),
        "market_context": json.loads((analysis_row["market_context_json"] if "market_context_json" in analysis_row.keys() else None) or "{}"),
        "created_at": analysis_row["created_at"],
    }
