const outletId = new URLSearchParams(location.search).get("store") || "ST1008";
document.getElementById("outletId").textContent = outletId;

const dataTimeline = { footfall: [], convRate: [], sales: [], queueLen: [], dropout: [] };
const TIMELINE_CAP = 30;

function appendReading(key, val) {
  const arr = dataTimeline[key];
  arr.push(Number(val) || 0);
  if (arr.length > TIMELINE_CAP) arr.shift();
}
function generateTrend(arr) {
  if (!arr.length) return "";
  const lo = Math.min(...arr), hi = Math.max(...arr);
  const span = hi - lo || 1;
  return arr.map((v, i) => {
    const x = (i / (TIMELINE_CAP - 1)) * 100;
    const y = 30 - ((v - lo) / span) * 28;
    return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
}
function drawTrend(id, arr) {
  const path = document.querySelector(`#${id} path`);
  if (path) path.setAttribute("d", generateTrend(arr));
}

function formatPct(n) {
  if (n === null || n === undefined) return "—";
  return Math.min(100, Number(n)).toFixed(1);
}

function processStream(data) {
  const m = data.metrics;
  const f = data.funnel;
  const h = data.heatmap;
  const a = data.anomalies;
  const hp = data.health;

  // Status pill
  const pill = document.getElementById("statusPill");
  const sysStatus = hp?.status || "down";
  pill.textContent = sysStatus.toUpperCase();
  pill.className = "health-pill " + (sysStatus === "ok" ? "ok" : sysStatus === "degraded" ? "degraded" : "down");

  // DOM diff cache for smooth updates
  window._renderCache = window._renderCache || {};
  const patchIfChanged = (id, html) => {
    if (window._renderCache[id] !== html) {
      document.getElementById(id).innerHTML = html;
      window._renderCache[id] = html;
    }
  };

  // KPIs
  if (m) {
    document.getElementById("metricFootfall").textContent = m.unique_visitors ?? 0;
    document.getElementById("metricConvRate").textContent  = formatPct(m.conversion_rate);
    document.getElementById("metricSales").textContent     = m.pos_transactions ?? 0;
    document.getElementById("metricQueueLen").textContent  = m.current_queue_depth ?? 0;
    document.getElementById("metricDropout").textContent   = formatPct(m.abandonment_rate);

    appendReading("footfall", m.unique_visitors);
    appendReading("convRate", m.conversion_rate);
    appendReading("sales",    m.pos_transactions);
    appendReading("queueLen", m.current_queue_depth);
    appendReading("dropout",  m.abandonment_rate);
    drawTrend("trendFootfall", dataTimeline.footfall);
    drawTrend("trendConvRate", dataTimeline.convRate);
    drawTrend("trendSales",    dataTimeline.sales);
    drawTrend("trendQueueLen", dataTimeline.queueLen);
    drawTrend("trendDropout",  dataTimeline.dropout);

    const dwellInfo = Object.entries(m.avg_dwell_per_zone_ms || {})
      .map(([k, v]) => `${k.replace(/^ZONE_/, "")}: ${(v/1000).toFixed(1)}s`)
      .join(" · ");
    document.getElementById("metricFootfallSub").textContent = dwellInfo || "today";

    // Sales Insights
    const renderBarChart = (dict, elemId) => {
      const keys = Object.keys(dict || {});
      if (!keys.length) return;
      const peak = Math.max(...Object.values(dict));
      const html = keys.map(k => `
        <div class="funnel-row slide-in">
          <div class="stage" style="width: 140px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${k}">${k}</div>
          <div class="funnel-bar"><span style="width:${(dict[k]/peak)*100}%;"></span></div>
          <div class="count" style="width: 50px">${dict[k]}</div>
        </div>
      `).join("");
      patchIfChanged(elemId, html);
    };
    renderBarChart(m.top_departments, "deptChart");
    renderBarChart(m.top_brands, "brandChart");
  }

  // Funnel
  if (f?.stages) {
    const peak = Math.max(1, ...f.stages.map(s => s.count));
    document.getElementById("funnelTag").textContent = `${f.total_sessions} sessions`;
    const html = f.stages.map(s => `
      <div class="funnel-row slide-in">
        <div class="stage">${s.stage}</div>
        <div class="funnel-bar"><span style="width:${(s.count/peak)*100}%;"></span></div>
        <div class="count">${s.count}</div>
        <div class="drop ${s.drop_off_from_prev_pct > 0 ? '' : 'zero'}">${s.drop_off_from_prev_pct > 0 ? '↓ ' + s.drop_off_from_prev_pct.toFixed(1) + '%' : '—'}</div>
      </div>
    `).join("");
    patchIfChanged("pipelineView", html);
  }

  // Zone intensity
  if (h?.zones) {
    document.getElementById("intensityTag").textContent =
      `${h.total_sessions} sessions · ${h.data_confidence || "?"}`;
    const html = h.zones.map(z => `
      <div class="heat-cell slide-in" style="--i:${(z.intensity||0)/100};">
        <div class="bg"></div>
        <div class="intensity">${z.intensity.toFixed(0)}</div>
        <div class="zone">${z.zone_id.replace(/^ZONE_/, "")}</div>
        <div class="stat">${z.visit_count} visits</div>
        <div class="stat">${(z.avg_dwell_ms/1000).toFixed(1)}s avg dwell</div>
      </div>
    `).join("");
    patchIfChanged("intensityGrid", html);
  }

  // Alerts
  if (a?.anomalies) {
    document.getElementById("alertTag").textContent = `${a.count} active`;
    const rows = a.anomalies.length ? a.anomalies.map(x => `
      <tr class="slide-in">
        <td><strong>${x.type.replace(/_/g, " ")}</strong></td>
        <td><span class="sev ${x.severity}">${x.severity}</span></td>
        <td><code style="font-size:11px;color:var(--text-muted);">${Object.entries(x.detail).map(([k,v]) => `${k}: ${JSON.stringify(v)}`).join(" · ")}</code></td>
        <td style="color:var(--text-muted);">${x.suggested_action || ""}</td>
      </tr>
    `).join("") : `<tr><td colspan="4" style="color:var(--text-light); padding:24px 14px;">✓ No active alerts</td></tr>`;
    patchIfChanged("alertRows", rows);
  }

  // Camera stale indicator
  if (hp?.stores) {
    const outlet0 = hp.stores.find(s => s.store_id === outletId);
    if (outlet0) {
      const ts = new Date(outlet0.last_event_timestamp);
      const ageMin = ((Date.now() - ts.getTime()) / 60000).toFixed(0);
      document.querySelectorAll(".cam .meta").forEach(el => {
        el.textContent = ageMin + "m ago";
        const dot = el.parentElement.querySelector(".dot");
        if (outlet0.stale) { dot.style.background = "var(--color-warn)"; }
        else { dot.style.background = "var(--color-ok)"; }
      });
    }
  }
}

function initLiveConnection() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${location.host}/ws/stores/${outletId}`;

  const pill = document.getElementById("statusPill");
  pill.textContent = "CONNECTING...";
  pill.className = "health-pill degraded";

  const ws = new WebSocket(wsUrl);

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      processStream(data);
    } catch (e) {
      console.error("Error parsing websocket data", e);
    }
  };

  ws.onclose = () => {
    console.log("WebSocket disconnected. Reconnecting in 3s...");
    const pill = document.getElementById("statusPill");
    pill.textContent = "DISCONNECTED";
    pill.className = "health-pill down";
    setTimeout(initLiveConnection, 3000);
  };
}

initLiveConnection();

// Smooth clock ticker
setInterval(() => {
  document.getElementById("wallClock").textContent = new Date().toLocaleTimeString("en-US", { hour12: false });
}, 1000);
