import Chart from "chart.js/auto";

const API = "/api";
const POLL_INTERVAL = 10000;
let pollTimer = null;

// --- Navigation ---
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`page-${btn.dataset.page}`).classList.add("active");

    if (btn.dataset.page === "history" && !historyLoaded) loadHistory();
  });
});

// --- Visibility-based polling ---
function startPolling() {
  stopPolling();
  pollTimer = setInterval(loadToday, POLL_INTERVAL);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    loadToday();
    startPolling();
  }
});

// --- Loading helpers ---
function showSkeleton() {
  document.querySelectorAll(".stat-value").forEach((el) => el.classList.add("skeleton"));
  const tbody = document.getElementById("sip-table");
  tbody.innerHTML = Array.from({ length: 3 }, () =>
    `<tr class="skeleton-row">
      <td><span class="skeleton">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span></td>
      <td><span class="skeleton">&nbsp;&nbsp;&nbsp;&nbsp;</span></td>
      <td><span class="skeleton">&nbsp;&nbsp;&nbsp;</span></td>
      <td><div class="bar"><div class="bar-fill skeleton" style="width:60%"></div></div></td>
    </tr>`
  ).join("");
}

function hideSkeleton() {
  document.querySelectorAll(".stat-value").forEach((el) => el.classList.remove("skeleton"));
}

function animateValue(el, value) {
  el.classList.add("updating");
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (typeof value === "string") {
        el.textContent = value;
      } else {
        el.innerHTML = value;
      }
      el.classList.remove("updating");
    });
  });
}

// --- Today ---
let firstLoad = true;

async function loadToday() {
  if (firstLoad) showSkeleton();

  try {
    const [todayRes, statusRes] = await Promise.all([
      fetch(`${API}/today`),
      fetch(`${API}/status`),
    ]);
    const data = await todayRes.json();
    const status = await statusRes.json();

    hideSkeleton();

    const el = document.getElementById("status");
    if (status.state === "connected") {
      el.className = "status online";
      el.textContent = `connected — ${timeAgo(status.last_seen)}`;
    } else if (status.state === "scanning") {
      el.className = "status online";
      el.textContent = `scanning — ${status.detail || timeAgo(status.last_seen)}`;
    } else if (status.online) {
      el.className = "status online";
      el.textContent = `synced ${timeAgo(status.last_seen)}`;
    } else {
      el.className = "status offline";
      el.textContent = `offline — ${status.state || "unknown"} ${timeAgo(status.last_seen)}`;
    }

    const mlEl = document.getElementById("today-ml");
    const sipsEl = document.getElementById("today-sips");
    const pctEl = document.getElementById("today-pct");
    const tempEl = document.getElementById("today-temp");

    if (firstLoad) {
      mlEl.textContent = data.total_ml;
      sipsEl.textContent = data.sip_count;
      pctEl.textContent = data.goal_pct;
      tempEl.textContent = data.last_temp_c != null ? data.last_temp_c : "—";
    } else {
      animateValue(mlEl, String(data.total_ml));
      animateValue(sipsEl, String(data.sip_count));
      animateValue(pctEl, String(data.goal_pct));
      animateValue(tempEl, data.last_temp_c != null ? String(data.last_temp_c) : "—");
    }

    document.getElementById("today-bar").style.width = `${data.goal_pct}%`;

    const tbody = document.getElementById("sip-table");

    if (data.sips.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">no sips recorded yet today</td></tr>`;
    } else {
      const maxMl = Math.max(...data.sips.map((s) => s.intake_ml), 1);
      tbody.innerHTML = data.sips
        .slice()
        .reverse()
        .map((s, i) => {
          const t = new Date(s.timestamp).toLocaleTimeString();
          const pct = Math.round((s.intake_ml / maxMl) * 100);
          return `<tr style="animation-delay:${i * 0.04}s">
            <td>${t}</td>
            <td>${s.intake_ml} ml</td>
            <td>${s.temp_c ?? "—"}°C</td>
            <td><div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div></td>
          </tr>`;
        })
        .join("");
    }

    firstLoad = false;
  } catch (e) {
    console.error("Failed to load today:", e);
    hideSkeleton();
    if (firstLoad) {
      document.getElementById("sip-table").innerHTML =
        `<tr><td colspan="4" class="error-state">failed to load — retrying</td></tr>`;
    }
  }
}

// --- History ---
let historyLoaded = false;
let historyChart = null;

async function loadHistory() {
  try {
    const res = await fetch(`${API}/history?days=90`);
    const data = await res.json();
    historyLoaded = true;

    if (data.days.length === 0) {
      document.getElementById("hist-avg").textContent = "—";
      document.getElementById("hist-best").textContent = "—";
      document.getElementById("hist-streak").textContent = "—";
      document.getElementById("heatmap").innerHTML =
        `<div class="empty-state">no history yet — drink some water!</div>`;
      return;
    }

    document.getElementById("hist-avg").textContent = data.avg_daily_ml;
    document.getElementById("hist-best").textContent = data.best_day_ml;
    document.getElementById("hist-streak").textContent = data.current_streak;

    // Bar chart with goal line
    const ctx = document.getElementById("history-chart").getContext("2d");
    if (historyChart) historyChart.destroy();

    const last30 = data.days.slice(-30);
    historyChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: last30.map((d) => {
          const dt = new Date(d.date);
          return `${dt.getMonth() + 1}/${dt.getDate()}`;
        }),
        datasets: [
          {
            label: "Intake (ml)",
            data: last30.map((d) => d.total_ml),
            backgroundColor: last30.map((d) =>
              d.total_ml >= 2500
                ? "rgba(102, 187, 106, 0.7)"
                : "rgba(79, 195, 247, 0.7)"
            ),
            borderRadius: 3,
            order: 1,
          },
          {
            label: "Goal",
            type: "line",
            data: last30.map(() => 2500),
            borderColor: "rgba(136, 136, 136, 0.4)",
            borderDash: [6, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            pointHitRadius: 0,
            fill: false,
            order: 0,
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) =>
                ctx.dataset.label === "Goal" ? null : `${ctx.raw} ml`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#888", font: { size: 9, family: "monospace" } },
            grid: { display: false },
          },
          y: {
            ticks: { color: "#888", font: { size: 10, family: "monospace" } },
            grid: { color: "#1e1e1e" },
          },
        },
      },
    });

    // Heatmap
    buildHeatmap(data.days);
  } catch (e) {
    console.error("Failed to load history:", e);
    document.getElementById("heatmap").innerHTML =
      `<div class="error-state">failed to load history</div>`;
  }
}

function buildHeatmap(days) {
  const container = document.getElementById("heatmap");
  const dayMap = {};
  for (const d of days) dayMap[d.date] = d.total_ml;

  const today = new Date();
  const start = new Date(today.getFullYear(), 0, 1); // Jan 1 of current year
  // Align to Sunday
  start.setDate(start.getDate() - start.getDay());

  const end = new Date(today.getFullYear(), 11, 31); // Dec 31 of current year

  // Build month labels
  const months = [];
  const cursor = new Date(start);
  let weekIndex = 0;
  let lastMonth = -1;

  while (cursor <= end) {
    if (cursor.getDay() === 0) {
      const m = cursor.getMonth();
      if (m !== lastMonth) {
        months.push({ index: weekIndex, label: cursor.toLocaleString("en", { month: "short" }) });
        lastMonth = m;
      }
      weekIndex++;
    }
    cursor.setDate(cursor.getDate() + 1);
  }

  const cellSize = 12;
  const gap = 3;
  const colWidth = cellSize + gap;
  const totalWeeks = weekIndex;

  const monthLabels = months.map((m) =>
    `<span style="position:absolute;left:${m.index * colWidth}px">${m.label}</span>`
  ).join("");

  // Build cells
  const cells = [];
  const cursor2 = new Date(start);
  while (cursor2 <= end) {
    const key = cursor2.toISOString().slice(0, 10);
    const ml = dayMap[key] || 0;
    let level = "";
    if (ml > 0 && ml < 1000) level = "l1";
    else if (ml >= 1000 && ml < 2000) level = "l2";
    else if (ml >= 2000 && ml < 2500) level = "l3";
    else if (ml >= 2500) level = "l4";

    cells.push(`<div class="heatmap-cell ${level}"><span class="tip">${key}: ${ml}ml</span></div>`);
    cursor2.setDate(cursor2.getDate() + 1);
  }

  const legend = `
    <div class="heatmap-legend">
      <span>less</span>
      <div class="swatch" style="background:var(--border)"></div>
      <div class="swatch" style="background:rgba(79,195,247,0.2)"></div>
      <div class="swatch" style="background:rgba(79,195,247,0.4)"></div>
      <div class="swatch" style="background:rgba(79,195,247,0.6)"></div>
      <div class="swatch" style="background:var(--accent)"></div>
      <span>more</span>
    </div>`;

  container.innerHTML = `
    <div style="position:relative;height:16px;margin-bottom:4px;min-width:${totalWeeks * colWidth}px" class="heatmap-months">${monthLabels}</div>
    <div class="heatmap-grid">${cells.join("")}</div>
    ${legend}`;
}

// --- Utils ---
function timeAgo(iso) {
  if (!iso) return "never";
  const diff = (Date.now() - new Date(iso + "Z").getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// --- Init ---
loadToday();
startPolling();
