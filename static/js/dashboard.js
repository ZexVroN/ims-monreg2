
      let incidentData = [];
      let chartRegion = null;
      let chartSla = null;
      let chartTrend = null;

      window.onload = function () {
        lucide.createIcons();
        initClocks();

        // Inisialisasi Canvas Chart lebih dulu
        initCharts();

        // Tarik data API
        fetchLiveIncidents();

        // Auto refresh setiap 2 menit (120000ms)
        setInterval(fetchLiveIncidents, 300000);
      };

      function initClocks() {
        setInterval(() => {
          const now = new Date();
          const hours = String(now.getHours()).padStart(2, "0");
          const minutes = String(now.getMinutes()).padStart(2, "0");
          const seconds = String(now.getSeconds()).padStart(2, "0");
          document.getElementById("realtimeClock").innerText =
            `${hours}:${minutes}:${seconds} WIB`;
        }, 1000);
      }

      async function fetchLiveIncidents() {
        try {
          const response = await fetch("/api/incidents");
          const result = await response.json();

          if (response.ok && result.data) {
            incidentData = result.data;

            const now = new Date();
            document.getElementById("lastSyncTime").innerText =
              `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
            document.getElementById("syncStatusText").innerText =
              "CONNECTED (LIVE)";

            updateKPIs();
            updateCharts(); // Render ulang grafik dengan data baru
            renderIncidentTable();
          } else {
            document.getElementById("syncStatusText").innerText = "SYNC ERROR";
          }
        } catch (err) {
          console.error("Gagal mengambil data live:", err);
          document.getElementById("syncStatusText").innerText = "DISCONNECTED";
        }
      }

      function initCharts() {
        const ctxRegion = document
          .getElementById("chartRegionDistribution")
          .getContext("2d");
        chartRegion = new Chart(ctxRegion, {
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
            scales: {
              x: {
                ticks: { color: "#94A3B8", font: { size: 10 } },
                grid: { display: false },
              },
              y: {
                ticks: { color: "#94A3B8", font: { size: 10 } },
                grid: { color: "#1E293B" },
              },
            },
          },
        });

        const ctxSla = document
          .getElementById("chartSlaMatrix")
          .getContext("2d");
        chartSla = new Chart(ctxSla, {
          type: "doughnut",
          data: {
            labels: ["< 2H Normal", "2-6H Warning", "> 6H Critical"],
            datasets: [
              {
                data: [0, 0, 0],
                backgroundColor: ["#10B981", "#F59E0B", "#EF4444"],
                borderWidth: 2,
                borderColor: "#0A0F1D",
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
                  color: "#94A3B8",
                  font: { size: 10, family: "JetBrains Mono" },
                },
              },
            },
            cutout: "68%",
          },
        });

        const ctxTrend = document
          .getElementById("chartTimelineTrend")
          .getContext("2d");
        chartTrend = new Chart(ctxTrend, {
          type: "line",
          data: {
            labels: [
              "00:00",
              "04:00",
              "08:00",
              "12:00",
              "16:00",
              "20:00",
              "NOW",
            ],
            datasets: [
              {
                label: "Active Alarms",
                data: [0, 0, 0, 0, 0, 0, 0],
                borderColor: "#10B981",
                backgroundColor: "rgba(16, 185, 129, 0.12)",
                fill: true,
                tension: 0.4,
                pointRadius: 4,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: {
                ticks: { color: "#94A3B8", font: { size: 10 } },
                grid: { display: false },
              },
              y: {
                ticks: { color: "#94A3B8", font: { size: 10 } },
                grid: { color: "#1E293B" },
              },
            },
          },
        });
      }

      function updateCharts() {
        if (!chartSla || !chartRegion || !chartTrend) return;

        // Hitung berdasarkan status dari Flask (i.Status)
        const criticalCount = incidentData.filter(
          (i) => i.Status === "CRITICAL",
        ).length;
        const warningCount = incidentData.filter(
          (i) => i.Status === "WARNING",
        ).length;
        const normalCount = incidentData.filter(
          (i) => i.Status === "NORMAL",
        ).length;

        // 1. Update Donut SLA Matrix
        chartSla.data.datasets[0].data = [
          normalCount,
          warningCount,
          criticalCount,
        ];
        chartSla.update();

        // 2. Update Bar Region
        const areaCounts = {};
        incidentData.forEach((i) => {
          const areaName = i.Area || "Unknown";
          areaCounts[areaName] = (areaCounts[areaName] || 0) + 1;
        });

        chartRegion.data.labels = Object.keys(areaCounts);
        chartRegion.data.datasets[0].data = Object.values(areaCounts);
        chartRegion.update();

        // 3. Update Line Timeline Trend
        chartTrend.data.datasets[0].data = [
          5,
          8,
          12,
          19,
          21,
          24,
          incidentData.length,
        ];
        chartTrend.update();
      }

      function renderIncidentTable() {
        const tbody = document.getElementById("incidentTableBody");
        const searchVal = document
          .getElementById("searchInput")
          .value.toLowerCase();
        const slaVal = document.getElementById("slaFilter").value;

        tbody.innerHTML = "";

        let filtered = incidentData.filter((item) => {
          const matchesSearch =
            String(item.IncidentId || "")
              .toLowerCase()
              .includes(searchVal) ||
            String(item.ObjectId || "")
              .toLowerCase()
              .includes(searchVal) ||
            String(item.SourceProblemName || "")
              .toLowerCase()
              .includes(searchVal);
          const matchesSla = slaVal === "ALL" || item.Status === slaVal;
          return matchesSearch && matchesSla;
        });

        document.getElementById("incidentCountBadge").innerText =
          `${filtered.length} Active`;

        if (filtered.length === 0) {
          tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400">Tidak ada insiden yang cocok dengan filter.</td></tr>`;
          return;
        }

        filtered.forEach((item) => {
          const tr = document.createElement("tr");
          const isCritical = item.Status === "CRITICAL";

          tr.className = isCritical
            ? "bg-red-950/30 border-l-4 border-l-red-500 hover:bg-red-900/40 transition duration-150"
            : "hover:bg-slate-800/40 transition duration-150";

          const slaBadge = isCritical
            ? `<span class="px-2 py-0.5 text-[10px] bg-red-600 text-white font-black rounded animate-pulse">&gt;6H SLA BREACH</span>`
            : item.Status === "WARNING"
              ? `<span class="px-2 py-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded border border-amber-500/40">2H - 6H WARNING</span>`
              : `<span class="px-2 py-0.5 text-[10px] bg-emerald-500/20 text-emerald-400 rounded border border-emerald-500/40">&lt;2H NORMAL</span>`;

          tr.innerHTML = `
          <td class="py-3 px-4 font-bold text-sky-400">#${item.IncidentId || "-"}</td>
          <td class="py-3 px-4 text-slate-200 font-semibold">${item.ObjectId || "-"}</td>
          <td class="py-3 px-4 text-slate-300">${item.Area || "-"} / ${item.KANWIL || "-"}</td>
          <td class="py-3 px-4 text-amber-300">${item.SourceProblemName || "-"}</td>
          <td class="py-3 px-4 text-slate-300">${item.FLMVENDOR || "-"}</td>
          <td class="py-3 px-4 text-slate-200 font-mono">${item.AgingStr || "00d 00h 00m"}</td>
          <td class="py-3 px-4">${slaBadge}</td>
        `;
          tbody.appendChild(tr);
        });
      }

      function filterIncidents() {
        renderIncidentTable();
      }

      function updateKPIs() {
        const total = incidentData.length;
        const criticalBreaches = incidentData.filter(
          (i) => i.Status === "CRITICAL",
        ).length;
        const uniqueAreas = new Set(incidentData.map((i) => i.Area)).size;

        document.getElementById("kpiTotalIncidents").innerText = total;
        document.getElementById("kpiImpactedAreas").innerText = uniqueAreas;
        document.getElementById("kpiImpactedMachines").innerText = total;
        document.getElementById("kpiCriticalBreaches").innerText =
          criticalBreaches;

        const card = document.getElementById("criticalSlaCard");
        if (criticalBreaches > 0) {
          card.classList.add("animate-glow-red");
        } else {
          card.classList.remove("animate-glow-red");
        }
      }

      function toggleTheme() {
        const body = document.getElementById("appBody");
        body.classList.toggle("dark");
        body.classList.toggle("light");
      }

      function toggleFullscreen() {
        if (!document.fullscreenElement) {
          document.documentElement
            .requestFullscreen()
            .catch((e) => console.log(e));
        } else {
          if (document.exitFullscreen) document.exitFullscreen();
        }
      }

      function exportToCSV() {
        let csvContent =
          "data:text/csv;charset=utf-8,Incident ID,Terminal ID,Area,Problem,Aging,Status\n";
        incidentData.forEach((i) => {
          csvContent += `${i.IncidentId},${i.ObjectId},${i.Area},"${i.SourceProblemName}",${i.AgingStr},${i.Status}\n`;
        });
        const encodedUri = encodeURI(csvContent);
        const link = document.createElement("a");
        link.setAttribute("href", encodedUri);
        link.setAttribute(
          "download",
          `IMS_Incidents_${new Date().toISOString().slice(0, 10)}.csv`,
        );
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }