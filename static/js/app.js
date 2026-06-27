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
