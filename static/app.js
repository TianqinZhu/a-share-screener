const state = {
  selectedSymbol: null,
  period: 1,
  rows: [],
  filter: "qualified",
};

const statusLabels = {
  buy_watch: "买入观察",
  watch: "关注等待",
  avoid: "暂不考虑",
  error: "数据错误",
  data_gap: "数据不足",
  confirmed: "已确认",
  pending_volume: "等量能",
  waiting: "等待",
  invalid: "失效",
  unknown: "未知",
};

const periodLabels = {
  1: "1分",
  5: "5分",
  15: "15分",
  60: "60分",
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(digits);
}

function formatAmount(value) {
  if (!value) return "-";
  if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(Math.round(value));
}

function formatMetricValue(item) {
  const value = item?.value;
  if (value === null || value === undefined || value === "") return "暂缺";
  if (["总市值", "流通市值", "成交额"].includes(item.label)) return formatAmount(value);
  if (typeof value === "number") return `${formatNumber(value)}${item.suffix || ""}`;
  return `${value}${item.suffix || ""}`;
}

function signedClass(value) {
  return Number(value) >= 0 ? "return-up" : "return-down";
}

function setStatus(text, tone = "") {
  const el = $("sourceStatus");
  el.textContent = text;
  el.style.color = tone === "error" ? "#b42318" : "#667085";
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  const data = await res.json();
  if (!res.ok || data.error) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function loadUniverse() {
  const data = await fetchJson("/api/universe");
  $("symbolsInput").value = data.symbols.join(",");
}

async function screen() {
  const symbols = $("symbolsInput").value.trim();
  const limit = $("limitInput").value || "10";
  setStatus(symbols ? "自定义股票池扫描中..." : "全A主流板块扫描中，可能需要几十秒...");
  $("screenButton").disabled = true;
  try {
    const qs = new URLSearchParams({ symbols, limit, deep_limit: "160" });
    const data = await fetchJson(`/api/screen?${qs}`);
    state.rows = data.rows;
    renderSummary(data);
    updateFilterTabs();
    const rows = visibleRows();
    renderTable(rows);
    setStatus(`已刷新 ${data.generated_at}`);
    if (rows.length) {
      await selectSymbol(rows[0].symbol);
    }
  } catch (err) {
    setStatus(`刷新失败：${err.message}`, "error");
  } finally {
    $("screenButton").disabled = false;
  }
}

async function backtest() {
  const symbols = $("symbolsInput").value.trim();
  const limit = $("limitInput").value || "10";
  const lookbackDays = $("backtestDaysInput").value || "30";
  setStatus(symbols ? "自定义股票池回测中..." : "全A主流板块快速回测中，可能需要几十秒...");
  $("backtestButton").disabled = true;
  try {
    const qs = new URLSearchParams({ symbols, limit, lookback_days: lookbackDays, deep_limit: "160" });
    const data = await fetchJson(`/api/backtest?${qs}`);
    renderBacktest(data);
    const meta = data.backtest_meta || {};
    setStatus(`回测完成 ${meta.as_of_date || "-"} 至 ${meta.latest_date || "-"}`);
  } catch (err) {
    setStatus(`回测失败：${err.message}`, "error");
  } finally {
    $("backtestButton").disabled = false;
  }
}

function renderBacktest(data) {
  const rows = data.rows || [];
  const meta = data.backtest_meta || {};
  const summary = data.summary || {};
  $("backtestAsOf").textContent = meta.as_of_date || "-";
  $("backtestLatest").textContent = meta.latest_date || "-";
  $("backtestAvg").innerHTML = `<span class="${signedClass(summary.avg_return_pct)}">${formatNumber(summary.avg_return_pct)}%</span>`;
  $("backtestWinRate").textContent = `${formatNumber(summary.win_rate_pct)}%`;
  $("backtestHint").textContent =
    `回测口径：${meta.entry_rule || "回测日收盘价"} 到 ${meta.exit_rule || "最新交易日收盘价"}；` +
    `样本 ${meta.prefiltered ?? "-"} 只，成功评分 ${meta.deep_scanned ?? "-"} 只，符合条件 ${meta.qualified ?? "-"} 只。`;

  const body = $("backtestBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="10" class="empty">没有可回测结果，可能是历史日线不足或行情源暂不可用。</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const status = statusLabels[row.signal?.status] || row.signal?.status || "-";
      return `
        <tr data-symbol="${escapeHtml(row.symbol)}">
          <td>${row.rank}</td>
          <td>${escapeHtml(row.symbol.toUpperCase())}</td>
          <td>${escapeHtml(row.name || "-")}</td>
          <td><strong>${row.signal?.score ?? "-"}</strong></td>
          <td><span class="badge ${escapeHtml(row.signal?.status || "")}">${escapeHtml(status)}</span></td>
          <td>${formatNumber(row.entry_price)}</td>
          <td>${formatNumber(row.latest_price)}</td>
          <td class="${signedClass(row.return_pct)}">${formatNumber(row.return_pct)}%</td>
          <td class="${signedClass(row.max_gain_pct)}">${formatNumber(row.max_gain_pct)}%</td>
          <td class="${signedClass(row.max_drawdown_pct)}">${formatNumber(row.max_drawdown_pct)}%</td>
        </tr>`;
    })
    .join("");
  body.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    tr.addEventListener("click", () => selectSymbol(tr.dataset.symbol));
  });
}

function renderSummary(data) {
  const rows = data.rows || [];
  const meta = data.scan_meta || {};
  $("metricTotal").textContent = meta.market_total ?? rows.length;
  $("metricBuyWatch").textContent = meta.deep_scanned ?? rows.length;
  $("metricWatch").textContent = meta.returned ?? rows.length;
  $("metricTime").textContent = meta.latest_trade_date || data.generated_at || "-";
}

function visibleRows() {
  if (state.filter === "all") return state.rows;
  return state.rows.filter((row) => ["buy_watch", "watch"].includes(row.signal.status));
}

function updateFilterTabs() {
  document.querySelectorAll(".filter-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === state.filter);
  });
}

function renderTable(rows) {
  const body = $("candidatesBody");
  const qualifiedCount = state.rows.filter((row) => ["buy_watch", "watch"].includes(row.signal.status)).length;
  $("tableHint").textContent =
    state.filter === "qualified"
      ? `符合要求 ${qualifiedCount} 只，按评分排序`
      : `全部 ${state.rows.length} 只，按评分排序`;
  if (!rows.length) {
    const message =
      state.filter === "qualified"
        ? "当前股票池没有买入观察或关注等待的标的，可切换到“全部”查看暂不考虑原因。"
        : "没有返回候选";
    body.innerHTML = `<tr><td colspan="9" class="empty">${message}</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const changeClass = Number(row.change_pct) >= 0 ? "up" : "down";
      return `
        <tr data-symbol="${row.symbol}">
          <td>${row.symbol.toUpperCase()}</td>
          <td>${row.name || "-"}</td>
          <td>${formatNumber(row.price)}</td>
          <td class="${changeClass}">${formatNumber(row.change_pct)}%</td>
          <td><strong>${row.signal.score}</strong></td>
          <td><span class="badge ${row.signal.status}">${statusLabels[row.signal.status] || row.signal.status}</span></td>
          <td>${formatNumber(row.signal.observe_price)}</td>
          <td>${formatNumber(row.signal.stop_price)}</td>
          <td>${row.signal.reason || "-"}</td>
        </tr>`;
    })
    .join("");
  body.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    tr.addEventListener("click", () => selectSymbol(tr.dataset.symbol));
  });
}

async function selectSymbol(symbol) {
  state.selectedSymbol = symbol;
  document.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    tr.classList.toggle("selected", tr.dataset.symbol === symbol);
  });
  setStatus(`读取 ${symbol.toUpperCase()} ${periodLabels[state.period] || `${state.period}分`}明细...`);
  try {
    const qs = new URLSearchParams({ symbol, period: String(state.period) });
    const data = await fetchJson(`/api/detail?${qs}`);
    renderDetail(data);
    setStatus(`明细已刷新 ${periodLabels[state.period] || `${state.period}分`} ${data.intraday.latest_time || ""}`);
  } catch (err) {
    setStatus(`明细失败：${err.message}`, "error");
  }
}

function renderDetail(data) {
  const quote = data.quote || {};
  const name = quote.name || data.daily.name || data.symbol;
  const periodLabel = periodLabels[state.period] || `${state.period}分`;
  $("detailTitle").textContent = `${name} ${data.symbol.toUpperCase()}`;
  const warning = data.intraday.warning ? `；${data.intraday.warning}` : "";
  const sourceNote = data.intraday.source === "sina_minline" ? "备用分时线不区分周期" : `${periodLabel}K线`;
  $("detailSubtitle").textContent = `报价时间 ${[quote.date, quote.time].filter(Boolean).join(" ") || "-"}，当前显示 ${sourceNote}${warning}`;
  $("periodDataMeta").textContent = `当前 ${periodLabel}，最后数据 ${data.intraday.latest_time || "-"}，来源 ${data.intraday.source || "-"}`;
  $("activePeriod").textContent = periodLabel;
  $("signalStatus").innerHTML = `<span class="badge ${data.signal.status}">${statusLabels[data.signal.status] || data.signal.status}</span>`;
  $("confirmStatus").innerHTML = `<span class="badge ${data.confirmation.status}">${statusLabels[data.confirmation.status] || data.confirmation.status}</span>`;
  $("lastPrice").textContent = formatNumber(data.confirmation.last_price || quote.price);
  renderAnalysis(data);
  drawDailyChart($("dailyChart"), data.daily.points || []);
  drawIntradayChart($("intradayChart"), data.intraday.points || []);
}

function renderKeyValues(targetId, items = []) {
  const target = $(targetId);
  if (!items.length) {
    target.innerHTML = `<div class="kv-row"><span>暂无</span><strong>数据源暂缺</strong></div>`;
    return;
  }
  target.innerHTML = items
    .map(
      (item) => `
        <div class="kv-row">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(formatMetricValue(item))}</strong>
        </div>`
    )
    .join("");
}

function renderList(targetId, items = []) {
  const target = $(targetId);
  const list = items.length ? items : ["暂无可展示项"];
  target.innerHTML = list.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function renderAnalysis(data) {
  const analysis = data.analysis || {};
  const technical = analysis.technical || {};
  const fundamental = analysis.fundamental || {};
  const risk = analysis.risk || {};
  $("technicalSummary").textContent = technical.summary || "暂无技术面结论";
  $("fundamentalSummary").textContent = fundamental.summary || "基本面字段暂缺";
  renderKeyValues("technicalList", technical.items || []);
  renderKeyValues("fundamentalList", fundamental.items || []);
  renderList("technicalPositives", technical.positives || []);
  renderList("riskList", risk.items || []);
  $("fundamentalWarning").textContent = data.fundamental_warning
    ? `基本面数据源提示：${data.fundamental_warning}`
    : "估值字段为交易软件快照口径，仍需结合财报、行业和公告核验。";
}

function yScale(min, max, top, bottom) {
  const spread = max - min || 1;
  return (value) => bottom - ((value - min) / spread) * (bottom - top);
}

function drawAxes(ctx, width, height, left, top, right, bottom, min, max) {
  ctx.strokeStyle = "#d9e0ea";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const y = top + ((bottom - top) * i) / 4;
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
  }
  ctx.stroke();
  ctx.fillStyle = "#667085";
  ctx.font = "12px Segoe UI, Arial";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const value = max - ((max - min) * i) / 4;
    const y = top + ((bottom - top) * i) / 4 + 4;
    ctx.fillText(value.toFixed(2), left - 8, y);
  }
}

function drawDailyChart(canvas, points) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  if (!points.length) return drawEmpty(ctx, width, height, "暂无日K数据");
  const data = points.slice(-120);
  const left = 56;
  const right = width - 18;
  const top = 18;
  const bottom = height - 58;
  const lows = data.map((p) => p.low);
  const highs = data.map((p) => p.high);
  const min = Math.min(...lows) * 0.99;
  const max = Math.max(...highs) * 1.01;
  const y = yScale(min, max, top, bottom);
  drawAxes(ctx, width, height, left, top, right, bottom, min, max);
  const step = (right - left) / data.length;
  data.forEach((p, i) => drawCandle(ctx, left + i * step + step / 2, step * 0.58, y, p));
  drawLine(ctx, data, "ma5", left, right, y, "#1768ac");
  drawLine(ctx, data, "ma10", left, right, y, "#167c72");
  drawLine(ctx, data, "ma20", left, right, y, "#9a6700");
  drawLine(ctx, data, "ma60", left, right, y, "#7a5af8");
  drawVolume(ctx, data, left, right, height - 46, height - 12);
  drawLegend(ctx, [
    ["MA5", "#1768ac"],
    ["MA10", "#167c72"],
    ["MA20", "#9a6700"],
    ["MA60", "#7a5af8"],
  ]);
}

function drawIntradayChart(canvas, points) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  if (!points.length) return drawEmpty(ctx, width, height, "暂无分钟K线数据");
  const data = points.slice(-240);
  const left = 56;
  const right = width - 18;
  const top = 18;
  const bottom = height - 72;
  const hasOhlc = "close" in data[0];
  const prices = hasOhlc
    ? data.flatMap((p) => [p.high, p.low])
    : data.map((p) => p.price);
  const min = Math.min(...prices) * 0.998;
  const max = Math.max(...prices) * 1.002;
  const y = yScale(min, max, top, bottom);
  drawAxes(ctx, width, height, left, top, right, bottom, min, max);
  const step = (right - left) / data.length;
  if (hasOhlc) {
    data.forEach((p, i) => drawCandle(ctx, left + i * step + step / 2, Math.max(2, step * 0.55), y, p));
  } else {
    drawGenericLine(ctx, data, (p) => p.price, left, right, y, "#1768ac");
    drawGenericLine(ctx, data, (p) => p.avg_price, left, right, y, "#9a6700");
  }
  drawVolume(ctx, data, left, right, height - 54, height - 14);
  ctx.fillStyle = "#667085";
  ctx.font = "12px Segoe UI, Arial";
  ctx.textAlign = "left";
  ctx.fillText(`${data[0].time} - ${data[data.length - 1].time}`, left, height - 4);
}

function drawCandle(ctx, x, w, y, p) {
  const up = p.close >= p.open;
  ctx.strokeStyle = up ? "#d92d20" : "#15803d";
  ctx.fillStyle = up ? "#fff1f0" : "#e9f6ef";
  ctx.beginPath();
  ctx.moveTo(x, y(p.high));
  ctx.lineTo(x, y(p.low));
  ctx.stroke();
  const top = y(Math.max(p.open, p.close));
  const bottom = y(Math.min(p.open, p.close));
  ctx.fillRect(x - w / 2, top, w, Math.max(1, bottom - top));
  ctx.strokeRect(x - w / 2, top, w, Math.max(1, bottom - top));
}

function drawLine(ctx, data, key, left, right, y, color) {
  drawGenericLine(ctx, data.filter((p) => p[key] !== null && p[key] !== undefined), (p) => p[key], left, right, y, color);
}

function drawGenericLine(ctx, data, getter, left, right, y, color) {
  if (data.length < 2) return;
  const step = (right - left) / data.length;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  data.forEach((p, i) => {
    const x = left + i * step + step / 2;
    const yy = y(Number(getter(p)));
    if (i === 0) ctx.moveTo(x, yy);
    else ctx.lineTo(x, yy);
  });
  ctx.stroke();
}

function drawVolume(ctx, data, left, right, top, bottom) {
  const maxVolume = Math.max(...data.map((p) => Number(p.volume) || 0), 1);
  const step = (right - left) / data.length;
  data.forEach((p, i) => {
    const volume = Number(p.volume) || 0;
    const h = ((bottom - top) * volume) / maxVolume;
    const up = "close" in p ? p.close >= p.open : true;
    ctx.fillStyle = up ? "rgba(217,45,32,0.28)" : "rgba(21,128,61,0.28)";
    ctx.fillRect(left + i * step + step * 0.18, bottom - h, Math.max(1, step * 0.64), h);
  });
}

function drawLegend(ctx, items) {
  ctx.font = "12px Segoe UI, Arial";
  ctx.textAlign = "left";
  items.forEach(([label, color], index) => {
    const x = 64 + index * 72;
    ctx.fillStyle = color;
    ctx.fillRect(x, 14, 18, 3);
    ctx.fillText(label, x + 24, 18);
  });
}

function drawEmpty(ctx, width, height, text) {
  ctx.fillStyle = "#667085";
  ctx.font = "16px Segoe UI, Arial";
  ctx.textAlign = "center";
  ctx.fillText(text, width / 2, height / 2);
}

function setupEvents() {
  $("screenButton").addEventListener("click", screen);
  $("backtestButton").addEventListener("click", backtest);
  $("loadUniverseButton").addEventListener("click", loadUniverse);
  document.querySelectorAll(".filter-tab").forEach((button) => {
    button.addEventListener("click", async () => {
      state.filter = button.dataset.filter;
      updateFilterTabs();
      const rows = visibleRows();
      renderTable(rows);
      if (rows.length) {
        await selectSymbol(rows[0].symbol);
      }
    });
  });
  document.querySelectorAll(".period-tabs .tab").forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(".period-tabs .tab").forEach((tab) => tab.classList.remove("active"));
      button.classList.add("active");
      state.period = Number(button.dataset.period);
      $("activePeriod").textContent = periodLabels[state.period] || `${state.period}分`;
      $("periodDataMeta").textContent = `正在切换到 ${periodLabels[state.period] || `${state.period}分`} 周期...`;
      if (state.selectedSymbol) await selectSymbol(state.selectedSymbol);
    });
  });
}

async function init() {
  setupEvents();
  $("symbolsInput").value = "";
}

init();
