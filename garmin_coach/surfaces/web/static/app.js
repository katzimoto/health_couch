"use strict";

// Dependency-free dashboard: fetch the JSON API and draw inline SVG line charts.
// Kept small and framework-free so it works offline with no build step.

const SVG = "http://www.w3.org/2000/svg";

// Preserve a ?token=... so the API calls stay authorized when the dashboard is
// protected by DASHBOARD_TOKEN.
const TOKEN = new URLSearchParams(location.search).get("token");
function withToken(url) {
  if (!TOKEN) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}

const state = { days: 30 };

async function getJSON(url) {
  const res = await fetch(withToken(url));
  if (!res.ok) throw new Error(url + " -> " + res.status);
  return res.json();
}

// ── Formatting helpers ─────────────────────────────────────────────────────────

function fmtNum(v, digits = 0) {
  if (v === null || v === undefined) return "–";
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function shortDate(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ── SVG line chart ──────────────────────────────────────────────────────────────

function drawChart(container, series, unit) {
  container.innerHTML = "";
  const points = series.filter((p) => p.value !== null && p.value !== undefined);
  if (points.length < 2) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = points.length ? "Not enough data yet" : "No data yet";
    container.appendChild(empty);
    return;
  }

  const W = 480, H = 160, padL = 40, padR = 12, padT = 14, padB = 24;
  const values = points.map((p) => Number(p.value));
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const range = max - min;
  min -= range * 0.1;
  max += range * 0.1;

  const x = (i) => padL + (i / (points.length - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);

  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");

  // Horizontal gridlines + y labels (min, mid, max).
  [min, (min + max) / 2, max].forEach((val) => {
    const gy = y(val);
    const grid = document.createElementNS(SVG, "line");
    grid.setAttribute("x1", padL); grid.setAttribute("x2", W - padR);
    grid.setAttribute("y1", gy); grid.setAttribute("y2", gy);
    grid.setAttribute("stroke", "#2c3846"); grid.setAttribute("stroke-width", "1");
    svg.appendChild(grid);

    const label = document.createElementNS(SVG, "text");
    label.setAttribute("x", padL - 6); label.setAttribute("y", gy + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", "9"); label.setAttribute("fill", "#8b98a9");
    label.textContent = fmtNum(val, range < 5 ? 1 : 0);
    svg.appendChild(label);
  });

  // Area under the line.
  const linePath = values.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" ");
  const area = document.createElementNS(SVG, "path");
  area.setAttribute(
    "d",
    `${linePath} L${x(points.length - 1)},${H - padB} L${x(0)},${H - padB} Z`
  );
  area.setAttribute("fill", "#4ea1ff"); area.setAttribute("fill-opacity", "0.08");
  svg.appendChild(area);

  // The line itself.
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", linePath);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "#4ea1ff");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute("stroke-linecap", "round");
  svg.appendChild(path);

  // Last point marker + value.
  const li = points.length - 1;
  const dot = document.createElementNS(SVG, "circle");
  dot.setAttribute("cx", x(li)); dot.setAttribute("cy", y(values[li]));
  dot.setAttribute("r", "3"); dot.setAttribute("fill", "#4ea1ff");
  svg.appendChild(dot);

  // X labels (first + last).
  [0, li].forEach((i) => {
    const t = document.createElementNS(SVG, "text");
    t.setAttribute("x", i === 0 ? padL : W - padR);
    t.setAttribute("y", H - 8);
    t.setAttribute("text-anchor", i === 0 ? "start" : "end");
    t.setAttribute("font-size", "9"); t.setAttribute("fill", "#8b98a9");
    t.textContent = shortDate(points[i].day);
    svg.appendChild(t);
  });

  // Native tooltip with the latest value.
  const title = document.createElementNS(SVG, "title");
  title.textContent = `${fmtNum(values[li], range < 5 ? 1 : 0)} ${unit} on ${points[li].day}`;
  svg.appendChild(title);

  container.appendChild(svg);
}

// ── Cards + flags ────────────────────────────────────────────────────────────────

const CARD_SPECS = [
  { key: "sleep_hours", label: "Sleep", unit: "h", digits: 1, betterUp: true },
  { key: "sleep_score", label: "Sleep score", unit: "", digits: 0, betterUp: true },
  { key: "hrv", label: "HRV", unit: "ms", digits: 0, betterUp: true },
  { key: "resting_hr", label: "Resting HR", unit: "bpm", digits: 0, betterUp: false },
  { key: "steps", label: "Steps", unit: "", digits: 0, betterUp: true },
  { key: "weight_kg", label: "Weight", unit: "kg", digits: 1, betterUp: false },
  { key: "body_fat", label: "Body fat", unit: "%", digits: 1, betterUp: false },
  { key: "avg_stress", label: "Stress", unit: "", digits: 0, betterUp: false },
];

function renderCards(report) {
  const el = document.getElementById("cards");
  el.innerHTML = "";
  const latest = report.latest || {};
  const trends = report.trends || {};
  for (const spec of CARD_SPECS) {
    const val = latest[spec.key];
    const card = document.createElement("div");
    card.className = "card";

    const delta = trends[spec.key] ? trends[spec.key].delta : null;
    let deltaHtml = "";
    if (delta !== null && delta !== undefined && delta !== 0) {
      const improving = spec.betterUp ? delta > 0 : delta < 0;
      const cls = improving ? "up" : "down";
      const arrow = delta > 0 ? "▲" : "▼";
      deltaHtml = `<div class="delta ${cls}">${arrow} ${fmtNum(Math.abs(delta), spec.digits)} vs 28d</div>`;
    } else {
      deltaHtml = `<div class="delta flat">— vs 28d</div>`;
    }

    card.innerHTML =
      `<div class="label">${spec.label}</div>` +
      `<div class="value">${fmtNum(val, spec.digits)}<span class="unit">${spec.unit}</span></div>` +
      deltaHtml;
    el.appendChild(card);
  }
}

function renderFlags(report) {
  const el = document.getElementById("flags");
  el.innerHTML = "";
  const flags = report.flags || [];
  if (!flags.length) {
    const ok = document.createElement("div");
    ok.className = "flag ok";
    ok.innerHTML = "✅ No flags — you're in good shape.";
    el.appendChild(ok);
    return;
  }
  for (const f of flags) {
    const div = document.createElement("div");
    div.className = "flag";
    div.innerHTML = `<span>🚩</span><span>${f}</span>`;
    el.appendChild(div);
  }
}

// ── Load + refresh ───────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const report = await getJSON("/api/report");
    const asOf = document.getElementById("as-of");
    if (!report.available) {
      asOf.textContent = "No data yet — run a Garmin pull or backfill.";
      renderFlags({ flags: [] });
      return;
    }
    asOf.textContent = "As of " + report.as_of;
    renderFlags(report);
    renderCards(report);

    const charts = document.querySelectorAll(".chart");
    await Promise.all(
      Array.from(charts).map(async (c) => {
        const metric = c.dataset.metric;
        try {
          const series = await getJSON(`/api/metric/${metric}?days=${state.days}`);
          drawChart(c, series, c.dataset.unit || "");
        } catch (e) {
          c.innerHTML = `<div class="empty">Failed to load</div>`;
        }
      })
    );
  } catch (e) {
    document.getElementById("as-of").textContent = "Error loading data: " + e.message;
  }
}

document.querySelectorAll(".range button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.days = parseInt(btn.dataset.days, 10);
    refresh();
  });
});

refresh();
// Auto-refresh every 10 minutes so an always-open tab stays current.
setInterval(refresh, 10 * 60 * 1000);
