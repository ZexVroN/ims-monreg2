let incidentData = [];
let chartRegion = null;
let chartVendor = null;
let resolvedData = [];
let openActionId = null; // Terminal ID yang panel aksinya sedang terbuka
const actionDrafts = {}; // draft komentar yang belum disimpan
let chartSla = null;
let chartTrend = null;
let currentAvailability = 100.0;
let totalMachines = 848;
let filterTimer = null;

const REFRESH_MS = 60000; // refresh tiap 1 menit (hanya baca SQLite lokal)
const STALE_AFTER_SECONDS = 900; // data dianggap basi jika sync > 15 menit lalu

window.onload = function () {
  lucide.createIcons();
  applySavedTheme();
  initClocks();
  initCharts();
  initActionHandlers();
  fetchLiveIncidents();
  setInterval(fetchLiveIncidents, REFRESH_MS);
};

/* ---------- Util ---------- */

function esc(s) {
  return String(s ?? "-").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

function pad(n) {
  return String(n).padStart(2, "0");
}

/* ---------- Clock ---------- */

function initClocks() {
  const tick = () => {
    const now = new Date();
    document.getElementById("realtimeClock").innerText =
      `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())} WIB`;
  };
  tick();
  setInterval(tick, 1000);
}

/* ---------- Theme ---------- */

function themeColors() {
  const light = document.getElementById("appBody").classList.contains("light");
  return {
    tick: light ? "#475569" : "#94A3B8",
    grid: light ? "#E2E8F0" : "#1E293B",
    surface: light ? "#FFFFFF" : "#0A0F1D",
  };
}

function applyChartTheme() {
  const c = themeColors();
  [chartRegion, chartTrend].forEach((ch) => {
    if (!ch) return;
    ch.options.scales.x.ticks.color = c.tick;
    ch.options.scales.y.ticks.color = c.tick;
    ch.options.scales.y.grid.color = c.grid;
    if (ch.options.plugins.legend.labels)
      ch.options.plugins.legend.labels.color = c.tick;
    ch.update();
  });
  if (chartSla) {
    chartSla.options.plugins.legend.labels.color = c.tick;
    chartSla.data.datasets[0].borderColor = c.surface;
    chartSla.update();
  }
  if (chartVendor) {
    chartVendor.options.scales.x.ticks.color = c.tick;
    chartVendor.options.scales.x.grid.color = c.grid;
    chartVendor.options.scales.y.ticks.color = c.tick;
    chartVendor.update();
  }
}

function setTheme(isLight) {
  const body = document.getElementById("appBody");
  body.classList.toggle("light", isLight);
  body.classList.toggle("dark", !isLight);
  document.documentElement.classList.toggle("light", isLight); // <-- tambah ini
  try {
    localStorage.setItem("dashTheme", isLight ? "light" : "dark");
  } catch (e) {
    /* abaikan jika storage diblokir */
  }
}

function applySavedTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem("dashTheme");
  } catch (e) {
    /* abaikan */
  }
  if (saved === "light") setTheme(true);
}

function toggleTheme() {
  const isLight = !document
    .getElementById("appBody")
    .classList.contains("light");
  setTheme(isLight);
  applyChartTheme();
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch((e) => console.log(e));
  } else if (document.exitFullscreen) {
    document.exitFullscreen();
  }
}

/* ---------- Sync status ---------- */

function setSyncStatus(text, color) {
  const el = document.getElementById("syncStatusText");
  el.innerText = text;
  ["text-emerald-400", "text-amber-400", "text-red-400"].forEach((c) =>
    el.classList.remove(c),
  );
  el.classList.add(color);
}

/* ---------- Fetch ---------- */

async function fetchLiveIncidents() {
  try {
    const response = await fetch("/api/incidents");
    const result = await response.json();

    if (response.ok && Array.isArray(result.data)) {
      incidentData = result.data;
      currentAvailability =
        typeof result.availability === "number" ? result.availability : 100.0;
      totalMachines = result.total_machines ?? totalMachines;

      document.getElementById("lastSyncTime").innerText =
        result.synced_at || "-";

      const age = result.sync_age_seconds;
      if (result.message) {
        setSyncStatus("DB BELUM TERISI", "text-amber-400");
      } else if (age === null || age === undefined) {
        setSyncStatus("MENUNGGU SYNC PERTAMA", "text-amber-400");
      } else if (age > STALE_AFTER_SECONDS) {
        setSyncStatus("DATA BASI (STALE)", "text-red-400");
      } else {
        setSyncStatus("CONNECTED (LIVE)", "text-emerald-400");
      }

            // Jika insiden yang sedang dibuka sudah selesai, tutup panelnya
      if (
        openActionId &&
        !incidentData.some((i) => String(i.ObjectId) === openActionId)
      ) {
        openActionId = null;
      }

      updateKPIs();
      updateCharts();
      // Jangan render ulang tabel saat panel komentar sedang dipakai
      if (!openActionId) renderIncidentTable();
      renderPriorityTable();
      fetchHistory();
      fetchResolved();
    } else {
      setSyncStatus("SYNC ERROR", "text-red-400");
    }
  } catch (err) {
    console.error("Gagal mengambil data live:", err);
    setSyncStatus("DISCONNECTED", "text-red-400");
  }
}

async function fetchHistory() {
  try {
    const response = await fetch("/api/history");
    const h = await response.json();
    if (response.ok && chartTrend) {
      chartTrend.data.labels = h.labels || [];
      chartTrend.data.datasets[0].data = h.normal || [];
      chartTrend.data.datasets[1].data = h.warning || [];
      chartTrend.data.datasets[2].data = h.critical || [];
      chartTrend.update();
    }
  } catch (err) {
    console.error("Gagal mengambil riwayat:", err);
  }
}

/* ---------- Charts ---------- */
const noDataPlugin = {
  id: "noData",
  afterDraw(chart) {
    if (chart.data.datasets.some((d) => d.data.length > 0)) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.fillStyle = "#94A3B8";
    ctx.font = "12px JetBrains Mono, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(
      "Belum ada data riwayat",
      (chartArea.left + chartArea.right) / 2,
      (chartArea.top + chartArea.bottom) / 2,
    );
    ctx.restore();
  },
};
function initCharts() {
  const c = themeColors();

  const baseScales = {
    x: { ticks: { color: c.tick, font: { size: 10 } }, grid: { display: false } },
    y: {
      beginAtZero: true,
      ticks: { color: c.tick, font: { size: 10 }, precision: 0 },
      grid: { color: c.grid },
    },
  };

  chartRegion = new Chart(
    document.getElementById("chartRegionDistribution").getContext("2d"),
    {
      type: "bar",
      data: {
        labels: [],
        datasets: [
          {
            data: [],
            backgroundColor: "rgba(56, 189, 248, 0.75)",
            borderColor: "#38BDF8",
            borderWidth: 1,
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
                scales: (() => {
          const s = JSON.parse(JSON.stringify(baseScales));
          s.x.ticks.autoSkip = true;
          s.x.ticks.maxTicksLimit = 8;
          return s;
        })(),
      },
    },
  );

    chartVendor = new Chart(
    document.getElementById("chartVendorDistribution").getContext("2d"),
    {
      type: "bar",
      data: {
        labels: [],
        datasets: [
          {
            data: [],
            backgroundColor: "rgba(129, 140, 248, 0.75)",
            borderColor: "#818CF8",
            borderWidth: 1,
            borderRadius: 6,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { color: c.tick, font: { size: 10 }, precision: 0 },
            grid: { color: c.grid },
          },
          y: {
            ticks: { color: c.tick, font: { size: 10 }, autoSkip: false },
            grid: { display: false },
          },
        },
      },
    },
  );

  chartSla = new Chart(
    document.getElementById("chartSlaMatrix").getContext("2d"),
    {
      type: "doughnut",
      data: {
        labels: ["<= 30 Menit", "30 Menit - 1 Jam", "> 1 Jam"],
        datasets: [
          {
            data: [0, 0, 0],
            backgroundColor: ["#10B981", "#F59E0B", "#EF4444"],
            borderWidth: 2,
            borderColor: c.surface,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: c.tick,
              font: { size: 10, family: "JetBrains Mono" },
            },
          },
        },
        cutout: "68%",
      },
    },
  );

  chartTrend = new Chart(
    document.getElementById("chartTimelineTrend").getContext("2d"),
    {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "≤ 30 Menit",
            data: [],
            borderColor: "#10B981",
            backgroundColor: "rgba(16, 185, 129, 0.08)",
            fill: false,
            tension: 0.4,
            pointRadius: 2,
          },
          {
            label: "30 Menit - 1 Jam",
            data: [],
            borderColor: "#F97316",
            backgroundColor: "rgba(249, 115, 22, 0.08)",
            fill: false,
            tension: 0.4,
            pointRadius: 2,
          },
          {
            label: "> 1 Jam",
            data: [],
            borderColor: "#EF4444",
            backgroundColor: "rgba(239, 68, 68, 0.08)",
            fill: false,
            tension: 0.4,
            pointRadius: 2,
          },
        ],
      },
      options: {
        plugins: [noDataPlugin],
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: {
              color: c.tick,
              boxWidth: 10,
              font: { size: 10, family: "JetBrains Mono" },
            },
          },
        },
        scales: JSON.parse(JSON.stringify(baseScales)),
      },
    },
  );
}

function updateCharts() {
  if (!chartSla || !chartRegion) return;

  const count = (s) => incidentData.filter((i) => i.Status === s).length;
  chartSla.data.datasets[0].data = [
    count("NORMAL"),
    count("WARNING"),
    count("CRITICAL"),
  ];
  chartSla.update();

  const areaCounts = {};
  incidentData.forEach((i) => {
    const name = i.Area || "Unknown";
    areaCounts[name] = (areaCounts[name] || 0) + 1;
  });
    const sorted = Object.entries(areaCounts).sort(
    (a, b) => a[1] - b[1] || a[0].localeCompare(b[0]),
  );
  chartRegion.data.labels = sorted.map((e) => e[0]);
  chartRegion.data.datasets[0].data = sorted.map((e) => e[1]);
  chartRegion.update();

    // Distribusi per vendor FLM (semua vendor, terbanyak di atas)
  if (chartVendor) {
    const vendorCounts = {};
    incidentData.forEach((i) => {
      const name = String(i.FLMVENDOR ?? "").trim() || "Unknown";
      vendorCounts[name] = (vendorCounts[name] || 0) + 1;
    });
        const vSorted = Object.entries(vendorCounts).sort(
      (a, b) => a[1] - b[1] || a[0].localeCompare(b[0]),
    );

    // Tinggi menyesuaikan jumlah vendor (minimal 256px, 28px per vendor)
    document.getElementById("vendorChartWrap").style.height =
      Math.max(256, vSorted.length * 28) + "px";

    chartVendor.data.labels = vSorted.map((e) => e[0]);
    chartVendor.data.datasets[0].data = vSorted.map((e) => e[1]);
    chartVendor.resize();
    chartVendor.update();
  }
}

/* ---------- Table ---------- */

function getFilteredIncidents() {
  const searchVal = document.getElementById("searchInput").value.toLowerCase();
  const slaVal = document.getElementById("slaFilter").value;

  const filtered = incidentData.filter((item) => {
    const haystack = [item.IncidentId, item.ObjectId, item.SourceProblemName]
      .map((v) => String(v ?? "").toLowerCase())
      .join(" ");
    const matchesSearch = haystack.includes(searchVal);
    const matchesSla = slaVal === "ALL" || item.Status === slaVal;
    return matchesSearch && matchesSla;
  });

  // Urutkan: aging terlama di atas
  filtered.sort((a, b) => {
    const ma = (a.AgeHours || 0) * 60 + (a.AgeMinutes || 0);
    const mb = (b.AgeHours || 0) * 60 + (b.AgeMinutes || 0);
    return mb - ma;
  });
  return filtered;
}

// Daftar area prioritas. Ubah/tambah nama area di sini jika perlu.
const PRIORITY_AREAS = ["113 / Palembang Arief", "112 / Palembang Sudirman"];

// Normalisasi: huruf kecil, tanda baca/spasi ganda diratakan,
// jadi "PALEMBANG  ARIEF" atau "Palembang-Arief" tetap cocok.
function normArea(s) {
  return String(s ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

// Cocokkan nama area prioritas terhadap kolom Area + KANWIL (kata utuh)
function isPriorityArea(item, name) {
  const hay = " " + normArea(`${item.Area ?? ""} ${item.KANWIL ?? ""}`) + " ";
  return hay.includes(" " + normArea(name) + " ");
}

function slaBadgeHtml(status) {
  if (status === "CRITICAL")
    return `<span class="px-2 py-0.5 text-[10px] bg-red-600 text-white font-black rounded animate-pulse">&gt;1 JAM</span>`;
  if (status === "WARNING")
    return `<span class="px-2 py-0.5 text-[10px] bg-orange-500/20 text-orange-300 rounded border border-orange-500/40">30 MNT - 1 JAM</span>`;
  return `<span class="px-2 py-0.5 text-[10px] bg-emerald-500/20 text-emerald-400 rounded border border-emerald-500/40">&le;30 MNT</span>`;
}

function renderPriorityTable() {
  const tbody = document.getElementById("priorityTableBody");
  if (!tbody) return;

    const items = incidentData.filter((i) =>
    PRIORITY_AREAS.some((name) => isPriorityArea(i, name)),
  );

  // Aging terlama di atas
  items.sort((a, b) => {
    const ma = (a.AgeHours || 0) * 60 + (a.AgeMinutes || 0);
    const mb = (b.AgeHours || 0) * 60 + (b.AgeMinutes || 0);
    return mb - ma;
  });

  document.getElementById("priorityCountBadge").innerText =
    `${items.length} Active`;

  // Ringkasan per area
  document.getElementById("priorityAreaSummary").innerHTML = PRIORITY_AREAS.map(
    (name) => {
      const list = items.filter((i) => isPriorityArea(i, name));
      const red = list.filter((i) => i.Status === "CRITICAL").length;
      const cls =
        red > 0
          ? "border-red-500/60 text-red-400"
          : list.length > 0
            ? "border-orange-500/40 text-orange-300"
            : "border-emerald-500/40 text-emerald-400";
      return `<span class="px-3 py-1 rounded-lg border ${cls} text-xs font-mono">${esc(name)}: <b>${list.length}</b> insiden${red > 0 ? ` (${red} &gt;1 jam)` : ""}</span>`;
    },
  ).join("");

  // Bingkai panel berkedip jika ada tiket merah
  const hasRed = items.some((i) => i.Status === "CRITICAL");
  document
    .getElementById("priorityPanel")
    .classList.toggle("animate-glow-red", hasRed);

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-emerald-400">Semua area prioritas aman, tidak ada insiden aktif.</td></tr>`;
    return;
  }

  const frag = document.createDocumentFragment();
  items.forEach((item, index) => {
    const tr = document.createElement("tr");
    tr.className =
      item.Status === "CRITICAL"
        ? "bg-red-950/30 border-l-4 border-l-red-500 hover:bg-red-900/40 transition duration-150"
        : "hover:bg-slate-800/40 transition duration-150";
    tr.innerHTML = `
      <td class="py-3 px-4 font-bold text-sky-400 text-center">${index + 1}</td>
      <td class="py-3 px-4 text-slate-200 font-semibold">${esc(item.ObjectId)}</td>
      <td class="py-3 px-4 text-slate-300">${esc(item.Area)}</td>
      <td class="py-3 px-4 text-amber-300">${esc(item.SourceProblemName)}</td>
      <td class="py-3 px-4 text-slate-300">${esc(item.FLMVENDOR)}</td>
      <td class="py-3 px-4 text-slate-200 font-mono">${esc(item.AgingStr || "00d 00h 00m")}</td>
      <td class="py-3 px-4">${slaBadgeHtml(item.Status)}</td>
    `;
    frag.appendChild(tr);
  });
  tbody.innerHTML = "";
  tbody.appendChild(frag);
}

function renderIncidentTable() {
  const tbody = document.getElementById("incidentTableBody");
  const filtered = getFilteredIncidents();

  tbody.innerHTML = "";
  document.getElementById("incidentCountBadge").innerText =
    `${filtered.length} Active`;

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-6 text-slate-400">Tidak ada insiden yang cocok dengan filter.</td></tr>`;
    return;
  }

  const frag = document.createDocumentFragment();

  filtered.forEach((item, index) => {
    const oid = String(item.ObjectId ?? "");
    const isCritical = item.Status === "CRITICAL";
    const handled = item.ActionStatus === "HANDLED";

    const tr = document.createElement("tr");
    tr.className = isCritical
      ? "bg-red-950/30 border-l-4 border-l-red-500 hover:bg-red-900/40 transition duration-150"
      : "hover:bg-slate-800/40 transition duration-150";

    const slaBadge = slaBadgeHtml(item.Status);

    const actionBtn = handled
      ? `<button data-action="toggle" data-oid="${esc(oid)}" class="px-2.5 py-1 rounded-md text-[10px] font-bold bg-emerald-600 hover:bg-emerald-500 text-white whitespace-nowrap">✓ DITANGANI</button>`
      : `<button data-action="toggle" data-oid="${esc(oid)}" class="px-2.5 py-1 rounded-md text-[10px] font-bold bg-sky-600 hover:bg-sky-500 text-white">AKSI</button>`;

    tr.innerHTML = `
      <td data-label="No" class="py-3 px-4 font-bold text-sky-400 text-center">${index + 1}</td>
      <td data-label="Terminal ID" class="py-3 px-4 text-slate-200 font-semibold">${esc(item.ObjectId)}</td>
      <td data-label="Area" class="py-3 px-4 text-slate-300">${esc(item.Area)}</td>
      <td data-label="Error Type" class="py-3 px-4 text-amber-300">${esc(item.SourceProblemName)}</td>
      <td data-label="Vendor FLM" class="py-3 px-4 text-slate-300">${esc(item.FLMVENDOR)}</td>
      <td data-label="Aging" class="py-3 px-4 text-slate-200 font-mono">${esc(item.AgingStr || "00d 00h 00m")}</td>
      <td data-label="Status" class="py-3 px-4">${slaBadge}</td>
      <td data-label="Aksi" class="py-3 px-4">${actionBtn}</td>
    `;
    frag.appendChild(tr);

    // Panel komentar & status di bawah baris yang dibuka
    if (openActionId === oid) {
      const detail = document.createElement("tr");
      detail.dataset.detailFor = oid;
      detail.innerHTML = `<td colspan="8" class="p-0">${actionPanelHtml(item)}</td>`;
      frag.appendChild(detail);
    }
  });

  tbody.appendChild(frag);
}

function filterIncidents() {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(renderIncidentTable, 200);
}

/* ---------- Aksi: komentar, status, WhatsApp ---------- */

// Kontak WhatsApp per vendor FLM.
// Nama vendor harus sama dengan kolom VENDOR FLM (huruf besar/kecil tidak masalah).
// Nomor format 62xxxxxxxxxx (tanpa tanda + dan tanpa 0 di depan).

function actionInfoText(item) {
  function buildWaText(item, comment) {
  return [
    "*[IMS Incident]*",
    `Terminal: ${item?.ObjectId ?? "-"}`,
    `Incident ID: ${item?.IncidentId ?? "-"}`,
    `Area: ${item?.Area ?? "-"}`,
    `Masalah: ${item?.SourceProblemName ?? "-"}`,
    `Aging: ${item?.AgingStr ?? "-"}`,
    "",
    `Komentar: ${comment}`,
  ].join("\n");
}

function copyText(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(ta);
  if (!ok && navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => {});
    ok = true;
  }
  return ok;
}
  const parts = [];
  if (item.ActionUpdatedAt)
    parts.push(`Diperbarui: ${esc(String(item.ActionUpdatedAt).slice(5, 16))}`);
  if (item.ActionWaAt)
    parts.push(
      `WhatsApp dibuka ke ${esc(item.ActionWaTo)} (${esc(String(item.ActionWaAt).slice(5, 16))})`,
    );
  return parts.join(" · ");
}

function actionPanelHtml(item) {
  const oid = String(item.ObjectId ?? "");
  const handled = item.ActionStatus === "HANDLED";
  const draft =
    actionDrafts[oid] !== undefined ? actionDrafts[oid] : item.ActionComment || "";
  const waReady = item.WaReady === true;

  const statusBadge = handled
    ? `<span class="px-2 py-0.5 text-[10px] font-bold rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">SUDAH DITANGANI</span>`
    : `<span class="px-2 py-0.5 text-[10px] font-bold rounded bg-red-500/20 text-red-400 border border-red-500/40">BELUM DITANGANI</span>`;

  const waInfo = waReady
    ? `<span class="text-emerald-400">Kontak WhatsApp vendor: terdaftar</span>`
    : `<span class="text-amber-300">Kontak WhatsApp vendor belum terdaftar (isi di contacts.json)</span>`;

  const btn = "px-3 py-1.5 rounded-md text-[11px] font-bold transition";
  const sendCls = waReady
    ? "bg-emerald-600 hover:bg-emerald-500 text-white"
    : "bg-slate-800 text-slate-500 border border-slate-700 opacity-60 cursor-not-allowed";

  return `
    <div class="text-left p-3 m-2 rounded-lg bg-slate-900/70 border border-slate-800 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="text-xs text-slate-300 font-mono">TINDAKAN · ${esc(oid)} · ${esc(item.FLMVENDOR)}</div>
        <div class="flex items-center gap-2">
          <span class="text-[10px] text-slate-400 font-mono">STATUS</span>${statusBadge}
        </div>
      </div>

      <textarea data-role="comment" data-oid="${esc(oid)}" rows="3" maxlength="1000"
        placeholder="Tulis komentar / instruksi untuk vendor..."
        class="w-full bg-slate-900 border border-slate-700 text-slate-200 text-xs rounded-lg p-2 focus:outline-none focus:border-sky-500 font-mono placeholder:text-slate-500">${esc(draft)}</textarea>

      <div class="text-[11px] font-mono">${waInfo}</div>

      <div class="flex flex-wrap items-center gap-2">
        <button data-action="save" data-oid="${esc(oid)}" class="${btn} bg-sky-600 hover:bg-sky-500 text-white">SIMPAN</button>
        <button data-action="send" data-oid="${esc(oid)}" ${waReady ? "" : "disabled"} class="${btn} ${sendCls}">SIMPAN &amp; KIRIM WHATSAPP</button>
        ${handled ? `<button data-action="clear" data-oid="${esc(oid)}" class="${btn} bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700">HAPUS KOMENTAR</button>` : ""}
        <button data-action="close" data-oid="${esc(oid)}" class="${btn} bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700">TUTUP</button>
      </div>

      <div data-role="msg" class="text-[11px] text-slate-400 font-mono min-h-[1rem]">${actionInfoText(item)}</div>
    </div>
  `;
}

function findDetailRow(oid) {
  return [
    ...document.querySelectorAll("#incidentTableBody tr[data-detail-for]"),
  ].find((tr) => tr.dataset.detailFor === oid);
}

function setPanelMsg(oid, html, isError) {
  const row = findDetailRow(oid);
  const el = row && row.querySelector('[data-role="msg"]');
  if (!el) return;
  el.innerHTML = html;
  el.classList.toggle("text-red-400", !!isError);
  el.classList.toggle("text-slate-400", !isError);
}

function initActionHandlers() {
  const tbody = document.getElementById("incidentTableBody");

  tbody.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn || btn.disabled) return;
    const oid = btn.dataset.oid;

    switch (btn.dataset.action) {
      case "toggle":
        openActionId = openActionId === oid ? null : oid;
        renderIncidentTable();
        break;
      case "close":
        openActionId = null;
        renderIncidentTable();
        break;
      case "save":
        submitAction(oid, false);
        break;
      case "send":
        submitAction(oid, true);
        break;
      case "clear":
        clearAction(oid);
        break;
    }
  });

  // Simpan draft komentar supaya tidak hilang saat tabel di-render ulang
  tbody.addEventListener("input", (e) => {
    if (e.target.matches('textarea[data-role="comment"]')) {
      actionDrafts[e.target.dataset.oid] = e.target.value;
    }
  });
}

async function submitAction(oid, sendWa) {
  const item = incidentData.find((i) => String(i.ObjectId) === oid);
  const row = findDetailRow(oid);
  if (!item || !row) return;

  const comment = row.querySelector('[data-role="comment"]').value.trim();
  if (!comment) return setPanelMsg(oid, "Komentar belum diisi.", true);

  // Buka tab dulu saat tombol diklik supaya tidak diblokir browser
  const waWin = sendWa ? window.open("about:blank", "_blank") : null;
  setPanelMsg(oid, "Menyimpan...", false);

  try {
    const res = await fetch("/api/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        object_id: oid,
        incident_id: item.IncidentId ?? null,
        comment,
        send_wa: sendWa,
      }),
    });
    const r = await res.json();
    if (!res.ok || !r.ok) throw new Error(r.message || "Gagal menyimpan");

    item.ActionComment = comment;
    item.ActionStatus = "HANDLED";
    item.ActionUpdatedAt = r.updated_at;
    if (sendWa) {
      item.ActionWaTo = item.FLMVENDOR;
      item.ActionWaAt = r.updated_at;
    }
    delete actionDrafts[oid];
    renderIncidentTable();

    if (sendWa) {
      const url = `/api/wa?oid=${encodeURIComponent(oid)}`;
      if (waWin) {
        waWin.location.href = url;
      } else {
        setPanelMsg(
          oid,
          `Tersimpan. Pop-up diblokir, <a href="${esc(url)}" target="_blank" rel="noopener" class="text-sky-400 underline">klik di sini untuk membuka WhatsApp</a>.`,
          false,
        );
      }
    } else {
      setPanelMsg(oid, "Tersimpan.", false);
    }
  } catch (err) {
    if (waWin) waWin.close();
    setPanelMsg(oid, `Gagal: ${esc(err.message)}`, true);
  }
}

async function clearAction(oid) {
  const item = incidentData.find((i) => String(i.ObjectId) === oid);
  if (!item) return;
  if (!confirm("Hapus komentar dan kembalikan status ke BELUM DITANGANI?")) return;

  try {
    const res = await fetch("/api/actions", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ object_id: oid }),
    });
    const r = await res.json();
    if (!res.ok || !r.ok) throw new Error(r.message || "Gagal menghapus");

    [
      "ActionComment",
      "ActionStatus",
      "ActionUpdatedAt",
      "ActionWaTo",
      "ActionWaAt",
    ].forEach((k) => {
      item[k] = null;
    });
    delete actionDrafts[oid];
    renderIncidentTable();
  } catch (err) {
    setPanelMsg(oid, `Gagal: ${esc(err.message)}`, true);
  }
}

/* ---------- Solved Incidents ---------- */

function fmtDuration(min) {
  if (min === null || min === undefined) return "-";
  const d = Math.floor(min / 1440);
  const h = Math.floor((min % 1440) / 60);
  const m = min % 60;
  return d > 0 ? `${d}d ${pad(h)}h ${pad(m)}m` : `${h}h ${pad(m)}m`;
}

// Kategori lama penyelesaian: pakai batas yang sama dengan aging
function durationStatus(min) {
  if (min === null || min === undefined) return "NORMAL";
  return min > 60 ? "CRITICAL" : min > 30 ? "WARNING" : "NORMAL";
}

async function fetchResolved() {
  const days = document.getElementById("resolvedPeriod").value;
  try {
    const response = await fetch(
      `/api/resolved?days=${encodeURIComponent(days)}`,
    );
    const r = await response.json();
    if (response.ok && Array.isArray(r.data)) {
      resolvedData = r.data;
      renderResolvedTable();
    }
  } catch (err) {
    console.error("Gagal mengambil data solved:", err);
  }
}

function renderResolvedTable() {
  const tbody = document.getElementById("resolvedTableBody");
  if (!tbody) return;

  const q = document.getElementById("resolvedSearch").value.toLowerCase();
  const items = resolvedData.filter((i) =>
    [i.ObjectId, i.Area, i.SourceProblemName, i.FLMVENDOR, i.IncidentId]
      .map((v) => String(v ?? "").toLowerCase())
      .join(" ")
      .includes(q),
  );

  document.getElementById("resolvedCountBadge").innerText =
    `${items.length} Solved`;

  const withDur = items.filter((i) => typeof i.DurationMinutes === "number");
  document.getElementById("resolvedAvg").innerText = withDur.length
    ? fmtDuration(
        Math.round(
          withDur.reduce((sum, i) => sum + i.DurationMinutes, 0) /
            withDur.length,
        ),
      )
    : "-";

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-6 text-slate-400">Belum ada insiden selesai pada periode ini.</td></tr>`;
    return;
  }

  const frag = document.createDocumentFragment();
  items.forEach((item, index) => {
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-800/40 transition duration-150";

    const s = String(item.ResolvedAt || "");
    const doneAt =
      s.length >= 16
        ? `${s.slice(8, 10)}/${s.slice(5, 7)} ${s.slice(11, 16)}`
        : "-";

    tr.innerHTML = `
      <td data-label="No" class="py-3 px-4 font-bold text-sky-400 text-center">${index + 1}</td>
      <td data-label="Terminal ID" class="py-3 px-4 text-slate-200 font-semibold">${esc(item.ObjectId)}</td>
      <td data-label="Area" class="py-3 px-4 text-slate-300">${esc(item.Area)}</td>
      <td data-label="Error Type" class="py-3 px-4 text-amber-300">${esc(item.SourceProblemName)}</td>
      <td data-label="Vendor FLM" class="py-3 px-4 text-slate-300">${esc(item.FLMVENDOR)}</td>
      <td data-label="Selesai" class="py-3 px-4 text-slate-200">${esc(doneAt)}</td>
      <td data-label="Lama" class="py-3 px-4 text-slate-200">${esc(fmtDuration(item.DurationMinutes))}</td>
      <td data-label="Kategori" class="py-3 px-4">${slaBadgeHtml(durationStatus(item.DurationMinutes))}</td>
    `;
    frag.appendChild(tr);
  });
  tbody.innerHTML = "";
  tbody.appendChild(frag);
}

/* ---------- KPI ---------- */

function updateKPIs() {
  const total = incidentData.length;
  const criticalBreaches = incidentData.filter(
    (i) => i.Status === "CRITICAL",
  ).length;
  const uniqueAreas = new Set(
    incidentData.map((i) => i.Area).filter((a) => a),
  ).size;

  // Rata-rata aging (menit -> "Xh Ym")
  let avgText = "0h 0m";
  if (total > 0) {
    const sumMinutes = incidentData.reduce(
      (acc, i) => acc + (i.AgeHours || 0) * 60 + (i.AgeMinutes || 0),
      0,
    );
    const avg = Math.round(sumMinutes / total);
    avgText = `${Math.floor(avg / 60)}h ${avg % 60}m`;
  }

  document.getElementById("kpiTotalIncidents").innerText = total;
  document.getElementById("kpiImpactedAreas").innerText = uniqueAreas;
  document.getElementById("kpiAvgAging").innerText = avgText;
  document.getElementById("kpiCriticalBreaches").innerText = criticalBreaches;
  document.getElementById("kpiProblemSub").innerText = total;
  document.getElementById("kpiTotalMachines").innerText = totalMachines;

  // Availability: ganti hanya warna/animasi, jangan timpa class ukuran font
  const availElement = document.getElementById("kpiAvailability");
  availElement.innerText = `${Number(currentAvailability).toFixed(2)}%`;
  const below = currentAvailability < 99.0;
  availElement.classList.toggle("text-red-400", below);
  availElement.classList.toggle("animate-pulse", below);
  availElement.classList.toggle("text-emerald-400", !below);

  const card = document.getElementById("criticalSlaCard");
  card.classList.toggle("animate-glow-red", criticalBreaches > 0);
}

/* ---------- Export ---------- */

function exportToCSV() {
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [
    ["Incident ID", "Terminal ID", "Area", "Problem", "Vendor FLM", "Aging", "Status"],
  ];
  // Mengekspor sesuai filter & urutan yang sedang tampil di tabel
  getFilteredIncidents().forEach((i) =>
    rows.push([
      i.IncidentId,
      i.ObjectId,
      i.Area,
      i.KANWIL,
      i.SourceProblemName,
      i.FLMVENDOR,
      i.AgingStr,
      i.Status,
    ]),
  );

  const csv = "\uFEFF" + rows.map((r) => r.map(q).join(",")).join("\n");
  const url = URL.createObjectURL(
    new Blob([csv], { type: "text/csv;charset=utf-8;" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `IMS_Incidents_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}