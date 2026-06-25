# 💰 SkyCofl Smart Relist Dashboard

A private, mobile-friendly web dashboard that watches your **SkyCofl / CoflNet**
auctions and gives you **smart relist advice** based on *genuinely comparable*
listings — not raw lowest-BIN.

> The old approach compares a 5m upgraded **[Lvl 100] Silverfish** pet to a random
> 40k low-level Silverfish and tells you to relist at 40k. **This app refuses to do
> that.** When it can't find safe comparable listings it says **`INCOMPARABLE`**
> instead of handing you a dangerous price.

## ⚠️ What this is (and is NOT)

This is a **market-analysis, paper-trading, notification, and manual decision-support
tool**. It is **advisory only**.

It **does**:
- Read your public auctions and market data from the CoflNet HTTP API (read-only).
- Compare each item by its real features (rarity, pet level/tier, stars, recomb,
  enchants, gemstones, attributes, reforge, skins…).
- Recommend `HOLD`, `RELIST`, `INCOMPARABLE`, `PROFIT_LOW`, `CUT_LOSS`, or `SOLD`.
- Notify your phone (Pushover) and/or Discord when items sell or need attention.

It **never**:
- Logs into Minecraft or Hypixel, uses a session token, or touches your account.
- Buys, lists, cancels, or clicks anything in-game.
- Automates gameplay or runs any macro.

**Every final decision and action is yours, done manually in-game.**

---

## 1. Local setup

Requirements: Python 3.11+.

```bash
# 1. Clone / open the project folder
cd "Cofl AH Helper"

# 2. (recommended) create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env      # (Windows: copy .env.example .env)
#   -> edit .env and set at least MC_UUID

# 5. Run
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. If `APP_PASSWORD` is set you'll get a login page.

The SQLite database and tables are created automatically on first start at
`DATABASE_PATH` (default `data/app.db`).

---

## 2. Railway setup

This repo is Railway-ready (Docker).

1. Push the project to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**. Railway detects the
   `Dockerfile` (and `railway.json`).
3. Add a **Volume** mounted at `/app/data` so your database survives redeploys.
4. Set environment **Variables** (see section 3). At minimum:
   - `MC_UUID`
   - `APP_PASSWORD`
   - `SECRET_KEY` (a long random string)
   - `DATABASE_PATH=/app/data/app.db`
   - Optional: `DISCORD_WEBHOOK`, `PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN`
5. Deploy. Railway sets `$PORT` automatically; the app binds to it.

The background loop runs **inside** the web service, so no separate worker is
needed for a single-user dashboard.

---

## 3. Environment variables

| Variable | Default | Description |
|---|---|---|
| `MC_UUID` | — | Your Minecraft UUID (dashes optional). Read-only lookups. |
| `APP_PASSWORD` | — | If set, dashboard requires login. |
| `SECRET_KEY` | = APP_PASSWORD | Signs the session cookie. Set a random value in prod. |
| `COFL_BASE_URL` | `https://sky.coflnet.com` | CoflNet API base. |
| `CHECK_INTERVAL_SECONDS` | `300` | Background sync/analyse interval. |
| `DISCORD_WEBHOOK` | — | Optional Discord webhook URL. |
| `PUSHOVER_USER_KEY` / `PUSHOVER_APP_TOKEN` | — | Optional Pushover phone alerts. |
| `PUSHOVER_SOUND` | `cashregister` | Pushover sound. |
| `PUSHOVER_PRIORITY` | `1` | Pushover priority. |
| `AH_TAX_RATE` | `0.02` | AH tax used in profit math. |
| `RELIST_COMPARABLE_ONLY` | `true` | Only price from comparable listings. |
| `RELIST_COMPARABLE_PAGES` | `8` | BIN pages scanned per item. |
| `RELIST_MIN_COMPARABLE_MATCHES` | `2` | Min comparable listings to price. |
| `RELIST_MIN_COMPARABLE_SCORE` | `75` | Min comparability score (0-100). |
| `RELIST_PET_LEVEL_TOLERANCE` | `5` | Allowed pet-level difference. |
| `RELIST_STAR_TOLERANCE` | `0` | Allowed star difference. |
| `RELIST_GEMSTONE_TOLERANCE` | `0.80` | Gemstone similarity needed. |
| `RELIST_UNDERCUT_PERCENT` | `0.20` | Undercut percent component. |
| `RELIST_UNDERCUT_COINS` | `10000` | Minimum undercut in coins. |
| `RELIST_MIN_PROFIT_AFTER_TAX` | `250000` | Default per-item min profit. |
| `RELIST_MIN_PROFIT_PERCENT_AFTER_TAX` | `2` | Min profit percent (advisory). |
| `RELIST_ALERT_COOLDOWN_MINUTES` | `60` | Per-item alert cooldown. |
| `RELIST_ALERT_DECISIONS` | `RELIST,CUT_LOSS,PROFIT_LOW,INCOMPARABLE` | Which decisions alert. |
| `INCOMPARABLE_ALERT_THRESHOLD` | `1000000` | Only alert INCOMPARABLE above this value. |
| `NOTIFICATIONS_ENABLED` | `true` | Master switch. `false` = send no Discord/Pushover at all (safe for local dev). |
| `FIRST_SYNC_SUPPRESS_SOLD_ALERTS` | `true` | Never alert about already-sold auctions on the first sync of a fresh DB. |
| `SOLD_ALERTS` | `true` | Send sold alerts. |
| `RELIST_ALERTS` | `true` | Send relist/decision alerts. |
| `STARTUP_MESSAGE` | `false` | Send a "dashboard online" message at boot. |
| `STALE_AFTER_MISSED_SYNCS` | `2` | Missed syncs before a vanished active auction becomes STALE. |
| `DATABASE_PATH` | `data/app.db` | SQLite file path. |

---

## 4. How to use the app

1. **Sync** — click **↻ Sync now** (or wait for the background loop). Your active
   auctions appear as cards.
2. **Enter buy cost** — type what you paid into each card's *Buy cost* field
   (commas / `5m` / `500k` all work) and hit **Save**. Analysis needs this.
3. **Pick a min profit** — use the quick buttons (250k / 500k / 1m / 2m / 5m).
4. **Analyse** — click **⚡ Analyse**. The card shows a colour-coded decision,
   suggested relist price, expected profit after tax, confidence, comparable
   count, trend and volume.
5. **Decide manually** — open the item on SkyCofl, and relist/hold yourself.
6. **Get notified** — when an item sells, or a tracked item hits an important
   decision, you get a Discord/Pushover alert.

Filters (All / Missing buy cost / Relist / Hold / Incomparable / Sold / Ignored)
and sort options live above the grid. Click an item title for the **detail page**
showing extracted features, comparables used, *rejected* candidates and why,
price-history summary, past analyses and notification history.

### Decision badges
- 🟠 **RELIST** — overpriced vs comparable market; safe profitable relist exists.
- 🟢 **HOLD** — already competitive, or trend favours waiting.
- ⚪ **INCOMPARABLE** — not enough safe comparables; *no price guessed*.
- 🟡 **PROFIT_LOW** — relist works but below your min profit.
- 🔴 **CUT_LOSS** — *optional, risky*; market is below cost and trending down.
- 🔵 **SOLD** — item sold.

---

## 5. How buy-cost tracking works

- Buy cost is **per auction** and entered by you. It is stored as an integer
  number of coins.
- Inputs are parsed leniently: `5,000,000`, `5000000`, `5m`, `500k`, even
  `£5,000,000` all become `5000000`.
- Values are always shown back with thousands separators.
- Profit math uses AH tax:
  `profit_after_tax = sell_price − (sell_price × AH_TAX_RATE) − buy_cost`.
- The **profit floor** is the lowest sell price that still clears
  `buy_cost + min_profit` *after* tax. Suggested relist prices never drop below
  this floor unless the decision is `CUT_LOSS`.
- Items without a buy cost are flagged: *"Enter buy cost to enable profit/relist
  analysis."*

---

## 6. Safety note

This dashboard is **advisory only**. It performs **no automation** of any kind.
It reads public market data over HTTP and sends you notifications. It does not
log into Hypixel, does not use account credentials or session tokens, and never
buys, lists, cancels, or clicks anything in-game. You make every trade yourself.

---

## 7. Troubleshooting

- **No auctions show up** — check `MC_UUID` is correct (try with and without
  dashes), then click **↻ Sync now**. New accounts/items can take a moment to
  appear on CoflNet.
- **Everything is INCOMPARABLE** — that's by design when the market has too few
  matching items. Lower `RELIST_MIN_COMPARABLE_MATCHES` or
  `RELIST_MIN_COMPARABLE_SCORE` cautiously, or increase `RELIST_COMPARABLE_PAGES`.
- **No notifications** — confirm `DISCORD_WEBHOOK` and/or both Pushover keys are
  set (see the Settings page for ✅/—), and that `SOLD_ALERTS` / `RELIST_ALERTS`
  are `true`. Cooldown is `RELIST_ALERT_COOLDOWN_MINUTES`.
- **Login loops / "unauthorized"** — set a stable `SECRET_KEY`; if it changes,
  existing sessions are invalidated.
- **Database resets on Railway** — mount a volume at `/app/data` and set
  `DATABASE_PATH=/app/data/app.db`.
- **API rate limits / timeouts** — the client retries and caches; reduce
  `RELIST_COMPARABLE_PAGES` or increase `CHECK_INTERVAL_SECONDS` if you see
  frequent warnings in the logs.
- **Run the quick tests** — `python -m pytest tests/ -q` (or `python tests/test_basic.py`).

---

## 8. Auction states

The player auctions endpoint returns *recent* auctions, not only your live ones,
so every auction is classified into a state instead of being assumed active:

- **ACTIVE** — BIN (when the flag is present), no winning bid, end time in the
  future, and `startingBid > 0`. These are the only ones shown by default.
- **SOLD** — has a winning bid (`highestBid > 0`). The sold price comes from the
  highest bid; the listing price always comes from `startingBid`.
- **EXPIRED** — end time passed with no bids.
- **STALE** — was ACTIVE in the DB but missing from `STALE_AFTER_MISSED_SYNCS`
  (default 2) consecutive successful syncs. Hidden by default.

The dashboard defaults to **Active** only. Use the **Sold / Expired / Stale /
Ignored / All** filter chips to see the rest.

**Sold notifications fire only for a real ACTIVE → SOLD transition the app
actually observed.** Auctions that were already sold/expired when first seen (or
on the first sync of a fresh DB) are recorded but flagged handled, so they never
notify — running locally will not blast your phone with old sales. Set
`NOTIFICATIONS_ENABLED=false` to silence everything while developing.

## 9. Resetting the database

The database holds your tracked auctions, **buy costs**, notes and analysis
history. Resetting it wipes all of that. The app recreates the schema on next
start.

**Local (deletes saved buy costs):**
```bash
# Stop the app first, then delete the SQLite files:
rm -f data/app.db data/app.db-wal data/app.db-shm      # macOS/Linux
del data\app.db data\app.db-wal data\app.db-shm        # Windows cmd
Remove-Item data\app.db*                                # PowerShell
```
Restart the app and run **↻ Sync now**. The first sync after a wipe marks any
already-sold/expired auctions as handled — **no notifications are sent**.

**Railway (only if you want to wipe saved buy costs):**
Your DB lives on the mounted volume at `/app/data/app.db`. To wipe it, open a
Railway shell on the service and remove that file, or detach/recreate the
volume, then redeploy. Do this **only** if you deliberately want to discard your
saved buy costs and history — a normal redeploy keeps the volume intact.

> Tip: you don't need to reset just to clear old SOLD/EXPIRED clutter — they're
> already hidden by default and only appear under their filter chips.

---

Made for a single private user. Keep your `.env` secret. Happy (manual) flipping!
