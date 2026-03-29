import Chart from "chart.js/auto";

const API = "/api";

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

// --- Today ---
async function loadToday() {
  try {
    const [todayRes, statusRes] = await Promise.all([
      fetch(`${API}/today`),
      fetch(`${API}/status`),
    ]);
    const data = await todayRes.json();
    const status = await statusRes.json();

    const el = document.getElementById("status");
    el.className = `status ${status.online ? "online" : "offline"}`;
    el.textContent = status.online
      ? `synced ${timeAgo(status.last_sync)}`
      : `offline — last sync ${status.last_sync ? timeAgo(status.last_sync) : "never"}`;

    document.getElementById("today-ml").textContent = data.total_ml;
    document.getElementById("today-sips").textContent = data.sip_count;
    document.getElementById("today-pct").textContent = data.goal_pct;
    document.getElementById("today-temp").textContent =
      data.last_temp_c != null ? data.last_temp_c : "—";
    document.getElementById("today-bar").style.width = `${data.goal_pct}%`;

    const maxMl = Math.max(...data.sips.map((s) => s.intake_ml), 1);
    const tbody = document.getElementById("sip-table");
    tbody.innerHTML = data.sips
      .slice()
      .reverse()
      .map((s) => {
        const t = new Date(s.timestamp).toLocaleTimeString();
        const pct = Math.round((s.intake_ml / maxMl) * 100);
        return `<tr>
          <td>${t}</td>
          <td>${s.intake_ml} ml</td>
          <td>${s.temp_c ?? "—"}°C</td>
          <td><div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div></td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    console.error("Failed to load today:", e);
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

    document.getElementById("hist-avg").textContent = data.avg_daily_ml;
    document.getElementById("hist-best").textContent = data.best_day_ml;
    document.getElementById("hist-streak").textContent = data.current_streak;

    // Bar chart
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
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
        },
        scales: {
          x: {
            ticks: { color: "#666", font: { size: 9, family: "monospace" } },
            grid: { display: false },
          },
          y: {
            ticks: { color: "#666", font: { size: 10, family: "monospace" } },
            grid: { color: "#1e1e1e" },
          },
        },
      },
    });

    // Heatmap
    buildHeatmap(data.days);
  } catch (e) {
    console.error("Failed to load history:", e);
  }
}

function buildHeatmap(days) {
  const container = document.getElementById("heatmap");
  const dayMap = {};
  for (const d of days) dayMap[d.date] = d.total_ml;

  const today = new Date();
  const start = new Date(today);
  start.setDate(start.getDate() - 364);
  // Align to Sunday
  start.setDate(start.getDate() - start.getDay());

  const cells = [];
  const cursor = new Date(start);
  while (cursor <= today) {
    const key = cursor.toISOString().slice(0, 10);
    const ml = dayMap[key] || 0;
    let level = "";
    if (ml > 0 && ml < 1000) level = "l1";
    else if (ml >= 1000 && ml < 2000) level = "l2";
    else if (ml >= 2000 && ml < 2500) level = "l3";
    else if (ml >= 2500) level = "l4";

    cells.push(`<div class="heatmap-cell ${level}" title="${key}: ${ml}ml"></div>`);
    cursor.setDate(cursor.getDate() + 1);
  }

  container.innerHTML = `<div class="heatmap-grid">${cells.join("")}</div>`;
}

// --- Utils ---
function timeAgo(iso) {
  if (!iso) return "never";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// --- Init ---
loadToday();
setInterval(loadToday, 10000);
