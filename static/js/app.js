// SkyCofl Relist Dashboard - lightweight vanilla JS.
// Every action is an explicit user click. Nothing here automates gameplay.

function showToast(msg, kind) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.className = 'toast show ' + (kind || '');
    clearTimeout(window.__toastTimer);
    window.__toastTimer = setTimeout(() => { t.className = 'toast'; }, 2600);
}

async function postJSON(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    return data;
}

async function getJSON(url) {
    const resp = await fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' } });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    return data;
}

function busy(btn, on, label) {
    if (!btn) return;
    if (on) {
        btn.dataset.label = btn.textContent;
        btn.disabled = true;
        btn.textContent = label || '…';
    } else {
        btn.disabled = false;
        if (btn.dataset.label) btn.textContent = btn.dataset.label;
    }
}

async function saveBuyCost(uuid, el) {
    const input = document.getElementById('buy-' + uuid);
    const value = input ? input.value : '';
    const btn = (el && el.tagName === 'BUTTON') ? el : null;
    busy(btn, true, 'Saving…');
    try {
        const data = await postJSON('/api/auctions/' + uuid + '/buy-cost', { value });
        showToast(data.buy_cost != null ? ('Buy cost saved: ' + data.buy_cost_fmt) : 'Buy cost cleared', 'ok');
        // Re-analyse so the recommendation reflects the new cost.
        await analyse(uuid, null, false, true);
    } catch (e) {
        busy(btn, false);
        showToast('Could not save: ' + e.message, 'err');
    }
}

async function setMinProfit(uuid, value, el) {
    // visual selection
    const group = document.getElementById('profit-' + uuid);
    if (group) group.querySelectorAll('.qbtn').forEach(b => b.classList.remove('active'));
    if (el) el.classList.add('active');
    try {
        await postJSON('/api/auctions/' + uuid + '/min-profit', { value: String(value) });
        showToast('Min profit set', 'ok');
        await analyse(uuid, null, false, true);
    } catch (e) {
        showToast('Could not set min profit: ' + e.message, 'err');
    }
}

async function analyse(uuid, el, reload, silent) {
    const btn = (el && el.tagName === 'BUTTON') ? el : null;
    busy(btn, true, 'Analysing…');
    try {
        const data = await postJSON('/api/auctions/' + uuid + '/analyse', {});
        if (!silent) showToast('Analysis: ' + data.decision + ' (' + data.confidence + '%)', 'ok');
        setTimeout(() => window.location.reload(), 600);
    } catch (e) {
        busy(btn, false);
        showToast('Analyse failed: ' + e.message, 'err');
    }
}

async function carryCost(uuid, oldUuid, el) {
    busy(el, true, 'Carrying…');
    try {
        const data = await postJSON('/api/auctions/' + uuid + '/carry/' + oldUuid, {});
        showToast('Buy cost carried: ' + (data.buy_cost_fmt || data.buy_cost || ''), 'ok');
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Carry failed: ' + e.message, 'err');
    }
}

async function ignoreCarry(uuid, el) {
    busy(el, true, 'Ignoring…');
    try {
        await postJSON('/api/auctions/' + uuid + '/carry-ignore', {});
        const box = document.getElementById('carry-box-' + uuid);
        if (box) box.remove();
        showToast('Carry suggestion ignored', 'ok');
        busy(el, false);
    } catch (e) {
        busy(el, false);
        showToast('Ignore failed: ' + e.message, 'err');
    }
}

async function findPreviousBuyCost(uuid, el) {
    busy(el, true, 'Finding…');
    try {
        const data = await getJSON('/api/auctions/' + uuid + '/carry-suggestions?include_manual=true');
        const count = data.suggestions ? data.suggestions.length : 0;
        if (count > 0) {
            showToast('Found ' + count + ' previous buy cost candidate' + (count === 1 ? '' : 's'), 'ok');
            setTimeout(() => window.location.reload(), 400);
        } else {
            showToast('No previous same-item buy cost found', '');
            busy(el, false);
        }
    } catch (e) {
        busy(el, false);
        showToast('Lookup failed: ' + e.message, 'err');
    }
}

async function checkUndercut(uuid, el) {
    busy(el, true, 'Checking…');
    try {
        const data = await postJSON('/api/auctions/' + uuid + '/check-undercut', {});
        if (data.undercut) {
            showToast('Undercut found: ' + data.confidence + '% confidence', 'ok');
        } else {
            showToast('No meaningful undercut found', 'ok');
        }
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Undercut check failed: ' + e.message, 'err');
    }
}

async function toggleIgnore(uuid, el) {
    busy(el, true, '…');
    try {
        const data = await postJSON('/api/auctions/' + uuid + '/ignore', {});
        showToast(data.ignored ? 'Ignored' : 'Unignored', 'ok');
        setTimeout(() => window.location.reload(), 400);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function markSold(uuid, el) {
    const price = prompt('Mark as sold. Sale price (optional, e.g. 5,000,000 or 5m):', '');
    if (price === null) return; // cancelled
    busy(el, true, '…');
    try {
        await postJSON('/api/auctions/' + uuid + '/sold', { value: price });
        showToast('Marked as sold', 'ok');
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function saveNotes(uuid, el) {
    const input = document.getElementById('notes-' + uuid);
    busy(el, true, 'Saving…');
    try {
        await postJSON('/api/auctions/' + uuid + '/notes', { value: input ? input.value : '' });
        showToast('Note saved', 'ok');
        busy(el, false);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function sendTestNotification(el) {
    const box = document.getElementById('test-notif-result');
    busy(el, true, 'Sending…');
    try {
        const data = await postJSON('/api/notifications/test', {});
        const channels = [];
        if (data.discord_configured) channels.push('Discord ' + (data.sent_discord ? '✅ sent' : '❌ failed'));
        if (data.pushover_configured) channels.push('Pushover ' + (data.sent_pushover ? '✅ sent' : '❌ failed'));
        const sentAny = data.sent_discord || data.sent_pushover;
        const errs = (data.errors && data.errors.length) ? data.errors.join(' · ') : '';
        let summary;
        if (sentAny) {
            summary = 'Test notification sent — ' + channels.join(', ');
        } else if (!data.notifications_enabled) {
            summary = 'Not sent: notifications are disabled (NOTIFICATIONS_ENABLED=false).';
        } else if (!data.discord_configured && !data.pushover_configured) {
            summary = 'Not sent: no Discord or Pushover channel is configured.';
        } else {
            summary = 'Send failed — ' + (channels.join(', ') || 'no channel responded');
        }
        if (errs) summary += '  (' + errs + ')';
        if (box) {
            box.style.display = '';
            box.innerHTML = '<span class="k">Result</span><span class="v">' + summary + '</span>';
        }
        showToast(sentAny ? 'Test notification sent' : 'Test notification not sent', sentAny ? 'ok' : 'err');
    } catch (e) {
        if (box) {
            box.style.display = '';
            box.innerHTML = '<span class="k">Result</span><span class="v">Request failed: ' + e.message + '</span>';
        }
        showToast('Test failed: ' + e.message, 'err');
    } finally {
        busy(el, false);
    }
}

async function addManualFee(uuid, el) {
    const input = document.getElementById('manfee-' + uuid);
    const value = input ? input.value : '';
    busy(el, true, 'Adding…');
    try {
        await postJSON('/api/auctions/' + uuid + '/fees/manual-fee', { value });
        showToast('Manual listing fee added', 'ok');
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function addExtraCost(uuid, el) {
    const input = document.getElementById('extracost-' + uuid);
    const value = input ? input.value : '';
    busy(el, true, 'Adding…');
    try {
        await postJSON('/api/auctions/' + uuid + '/fees/extra-cost', { value });
        showToast('Extra cost added', 'ok');
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function resetFees(uuid, el) {
    if (!confirm('Reset the fee ledger for this item? This clears all recorded listing fees, relist count and manual costs.')) return;
    busy(el, true, 'Resetting…');
    try {
        await postJSON('/api/auctions/' + uuid + '/fees/reset', {});
        showToast('Fee ledger reset', 'ok');
        setTimeout(() => window.location.reload(), 500);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

async function viewFeeBreakdown(uuid, el) {
    busy(el, true, 'Loading…');
    try {
        const d = await getJSON('/api/auctions/' + uuid + '/fees');
        const b = d.breakdown_current || {};
        const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString());
        const msg =
            'Fee breakdown\n' +
            '— Relists counted: ' + (d.relist_count || 0) + '\n' +
            '— Listing fees paid: ' + fmt(d.accumulated_listing_fees) + '\n' +
            '— Manual extra costs: ' + fmt(d.manual_extra_costs) + '\n' +
            '— Sales tax: ' + (d.sales_tax_rate * 100) + '%  ·  Listing fee: ' + (d.listing_fee_rate * 100) + '%\n' +
            '— Profit if current sells: ' + fmt(b.true_profit);
        alert(msg);
        busy(el, false);
    } catch (e) {
        busy(el, false);
        showToast('Failed: ' + e.message, 'err');
    }
}

// ---- Flip Checker ----
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}
function coins(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
function signed(n) { return (n == null) ? '—' : (n >= 0 ? '+' : '') + Number(n).toLocaleString(); }

const FLIP_LABELS = { BUY: 'BUY', MAYBE: 'MAYBE', DO_NOT_BUY: 'DO NOT BUY', INCOMPARABLE: 'INCOMPARABLE / MANUAL CHECK' };

async function runFlipCheck(btn) {
    const input = (document.getElementById('flip-input') || {}).value || '';
    const buy = (document.getElementById('flip-buy') || {}).value || '';
    const min = (document.getElementById('flip-min') || {}).value || '';
    const errBox = document.getElementById('flip-error');
    const out = document.getElementById('flip-result');
    if (errBox) errBox.style.display = 'none';
    if (out) out.innerHTML = '';
    busy(btn, true, 'Checking…');
    try {
        const data = await postJSON('/api/flip-check', {
            auction_url_or_uuid: input, buy_price: buy, min_profit: min
        });
        renderFlipResult(data);
    } catch (e) {
        if (errBox) { errBox.style.display = ''; errBox.innerHTML = '<span>⚠ ' + esc(e.message) + '</span>'; }
        showToast('Flip check failed: ' + e.message, 'err');
    } finally {
        busy(btn, false);
    }
}

function renderFlipOptionRow(o) {
    if (!o) return '';
    return '<tr><td>' + esc(o.name) + '</td><td>' + esc(o.price_fmt) + '</td>' +
        '<td class="' + ((o.profit != null && o.profit >= 0) ? 'ok' : 'bad') + '">' + coins(o.profit) + '</td>' +
        '<td>' + (o.roi_percent == null ? '—' : o.roi_percent + '%') + '</td>' +
        '<td>' + esc(o.sale_chance) + '</td><td>' + esc(o.time_to_sell) + '</td><td>' + esc(o.risk) + '</td></tr>';
}

function renderFlipResult(d) {
    const out = document.getElementById('flip-result');
    if (!out) return;
    const s = d.suggested || {};
    const ms = d.max_safe_buy_prices || {};
    const sc = d.scores || {};
    const wall = (d.price_walls && d.price_walls.length) ? d.price_walls[0] : null;

    let html = '<div class="panel flip-card">';
    html += '<div class="flip-head"><span class="badge ' + esc(d.decision) + ' flip-badge">' + esc(FLIP_LABELS[d.decision] || d.decision) + '</span>' +
        '<div class="flip-headline">' + esc(d.headline) + '</div></div>';

    // Summary
    html += '<div class="flip-summary">';
    html += '<div class="fs"><span>Item</span><b>' + esc(d.item_name) + '</b></div>';
    html += '<div class="fs"><span>Buy price</span><b>' + coins(d.buy_price) + '</b></div>';
    html += '<div class="fs"><span>Current auction price</span><b>' + coins(d.current_price) + '</b></div>';
    html += '<div class="fs"><span>True profit (first listing)</span><b class="' + ((d.expected_profit != null && d.expected_profit >= 0) ? 'pos' : 'neg') + '">' + signed(d.expected_profit) + '</b></div>';
    html += '<div class="fs"><span>Profit after one relist</span><b class="' + ((d.profit_after_one_relist != null && d.profit_after_one_relist >= 0) ? 'pos' : 'neg') + '">' + signed(d.profit_after_one_relist) + '</b></div>';
    html += '<div class="fs"><span>Profit after two relists</span><b class="' + ((d.profit_after_two_relists != null && d.profit_after_two_relists >= 0) ? 'pos' : 'neg') + '">' + signed(d.profit_after_two_relists) + '</b></div>';
    html += '<div class="fs"><span>ROI</span><b>' + (d.roi_percent == null ? '—' : d.roi_percent + '%') + '</b></div>';
    html += '<div class="fs"><span>Confidence</span><b>' + esc(d.confidence) + '%</b></div>';
    html += '<div class="fs"><span>Overall risk</span><b>' + esc(d.risk_level) + '</b></div>';
    html += '<div class="fs"><span>Breakeven sale price</span><b>' + coins(d.breakeven_sale_price) + '</b></div>';
    html += '</div>';

    // Reasons
    if (d.reasons && d.reasons.length) {
        html += '<div class="reason-block"><div class="reason-title">Why ' + esc(FLIP_LABELS[d.decision] || d.decision) + '</div><ul>';
        d.reasons.forEach(r => { html += '<li>' + esc(r) + '</li>'; });
        html += '</ul></div>';
    }

    // Suggested options
    if (s.fast || s.balanced || s.greedy) {
        html += '<h3>Suggested relist options</h3><table class="tbl"><thead><tr><th>Option</th><th>List price</th><th>Profit after fees</th><th>ROI</th><th>Sale chance</th><th>Time to sell</th><th>Risk</th></tr></thead><tbody>';
        html += renderFlipOptionRow(s.fast) + renderFlipOptionRow(s.balanced) + renderFlipOptionRow(s.greedy);
        html += '</tbody></table>';
    }

    // Max safe buy prices
    html += '<h3>Max safe buy price</h3><div class="flip-summary">';
    html += '<div class="fs"><span>For 2m profit</span><b>' + coins(ms.for_2m_profit) + '</b></div>';
    html += '<div class="fs"><span>For 5m profit</span><b>' + coins(ms.for_5m_profit) + '</b></div>';
    html += '<div class="fs"><span>For 10m profit</span><b>' + coins(ms.for_10m_profit) + '</b></div>';
    html += '<div class="fs"><span>For your min profit</span><b>' + coins(ms.for_min_profit) + '</b></div>';
    html += '<div class="fs"><span>Min profit, surviving one relist</span><b>' + coins(ms.for_min_profit_after_relist) + '</b></div>';
    html += '</div>';

    // Market context
    html += '<h3>Market context</h3><div class="flip-summary">';
    html += '<div class="fs"><span>Volume/day</span><b>' + (d.volume_per_day == null ? 'unknown' : d.volume_per_day) + '</b></div>';
    html += '<div class="fs"><span>Trend</span><b>' + esc(d.trend_label) + '</b></div>';
    html += '<div class="fs"><span>Raw same-tag LBIN</span><b>' + coins((d.market_context || {}).raw_same_tag_lbin) + '</b></div>';
    html += '<div class="fs"><span>Price rank</span><b>' + (d.price_rank ? ('#' + d.price_rank + ' of ' + d.price_rank_total) : '—') + '</b></div>';
    html += '<div class="fs"><span>Price wall</span><b>' + (wall ? (wall.count + ' within ' + wall.window_percent + '% of ' + coins(wall.price)) : 'none') + '</b></div>';
    html += '<div class="fs"><span>Liquidity / Demand / Competition</span><b>' + esc((sc.liquidity || {}).label) + ' · ' + esc((sc.demand || {}).label) + ' · ' + esc((sc.competition || {}).label) + '</b></div>';
    html += '</div>';

    // Feature summary
    html += '<div class="reason" style="margin-top:10px"><b>Features:</b> ' + esc(d.feature_summary) + '</div>';
    if (d.confidence_notes && d.confidence_notes.length) {
        html += '<div class="reason muted">Confidence reduced by: ' + esc(d.confidence_notes.join(', ')) + '</div>';
    }

    // Comparable table
    html += '<h3>Comparable proof (' + (d.comparables ? d.comparables.length : 0) + ')</h3>';
    if (d.comparables && d.comparables.length) {
        html += '<table class="tbl"><thead><tr><th>Price</th><th>Item</th><th>Score</th><th>Verdict</th><th>Why</th></tr></thead><tbody>';
        d.comparables.forEach(c => {
            html += '<tr><td class="ok">' + coins(c.price) + '</td><td>' + esc(c.item_name) + '</td><td>' + esc(c.score) +
                '</td><td>' + esc(c.verdict) + '</td><td>' + esc((c.reasons || []).join('; ')) +
                (c.url ? ' · <a href="' + esc(c.url) + '" target="_blank" rel="noopener">open ↗</a>' : '') + '</td></tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<p class="muted">No safe comparable listings passed the quality threshold — this is why raw LBIN is not used.</p>';
    }

    // Rejected
    if (d.rejected && d.rejected.length) {
        html += '<h3>Rejected comparables (' + d.rejected.length + ')</h3><table class="tbl"><thead><tr><th>Price</th><th>Item</th><th>Rejected because</th></tr></thead><tbody>';
        d.rejected.forEach(r => {
            html += '<tr><td class="bad">' + coins(r.price) + '</td><td>' + esc(r.item_name) + '</td><td>' + esc((r.rejections || [r.reason]).join('; ')) + '</td></tr>';
        });
        html += '</tbody></table>';
        if (d.rejection_counts) {
            const parts = Object.keys(d.rejection_counts).map(k => k + ': ' + d.rejection_counts[k]);
            if (parts.length) html += '<p class="muted" style="margin-top:6px">' + esc(parts.join(' · ')) + '</p>';
        }
    }

    html += '</div>';
    out.innerHTML = html;
    out.scrollIntoView({ behavior: 'smooth', block: 'start' });
    showToast('Flip checked: ' + (FLIP_LABELS[d.decision] || d.decision), d.decision === 'BUY' ? 'ok' : (d.decision === 'DO_NOT_BUY' ? 'err' : ''));
}

async function syncNow(el) {
    busy(el, true, 'Syncing…');
    try {
        const data = await postJSON('/api/auctions/sync', {});
        const s = data.stats && data.stats.synced ? data.stats.synced : {};
        showToast('Synced ' + (s.seen || 0) + ' auctions · ' + (s.sold || 0) + ' sold', 'ok');
        setTimeout(() => window.location.reload(), 800);
    } catch (e) {
        busy(el, false);
        showToast('Sync failed: ' + e.message, 'err');
    }
}
