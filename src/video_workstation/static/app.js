let statusTimer;

document.querySelector("[data-back-button]")?.addEventListener("click", () => {
  let localPrevious = false;
  try { localPrevious = new URL(document.referrer).origin === location.origin; } catch (_) {}
  if (history.length > 1 && localPrevious) history.back();
  else location.assign("/create");
});

function showStatus(message, isError = false) {
  const status = document.getElementById("app-status");
  if (!status) return;
  clearTimeout(statusTimer);
  status.hidden = false;
  status.classList.toggle("is-error", isError);
  status.setAttribute("role", isError ? "alert" : "status");
  status.setAttribute("aria-live", isError ? "assertive" : "polite");
  status.textContent = message;
  statusTimer = setTimeout(() => {
    status.hidden = true;
    status.textContent = "";
  }, 5000);
}

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-toggle]");
  if (!trigger) return;
  const target = document.getElementById(trigger.dataset.toggle);
  if (!target) return;
  target.classList.toggle("is-collapsed");
  const expanded = !target.classList.contains("is-collapsed");
  target.hidden = !expanded;
  document.querySelectorAll("[data-toggle]").forEach((candidate) => {
    if (candidate.dataset.toggle === trigger.dataset.toggle) candidate.setAttribute("aria-expanded", String(expanded));
  });
  if (expanded) target.querySelector("input, textarea")?.focus();
  else trigger.focus();
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest(".demo-enqueue");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在入队…";
  const payload = Object.fromEntries(new FormData(form));
  const csrfToken = payload.csrf_token || "";
  delete payload.csrf_token;
  for (const key of ["duration_seconds", "priority", "estimated_temp_bytes", "seed"]) payload[key] = Number(payload[key]);
  try {
    const response = await fetch(`/api/shots/${form.dataset.shot}/enqueue`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "入队失败");
    button.textContent = "已入队";
    showStatus("任务已加入队列");
    setTimeout(() => location.reload(), 500);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showStatus(error.message, true);
  }
});

const thermalStrip = document.querySelector("[data-thermal-strip]");
if (thermalStrip) {
  const updateThermal = async () => {
    try {
      const response = await fetch("/api/thermal/status", {cache: "no-store"});
      if (!response.ok) throw new Error("monitor unavailable");
      const status = await response.json();
      thermalStrip.className = `thermal-strip thermal-${status.level}`;
      thermalStrip.querySelector("[data-thermal-temperature]").textContent =
        status.temperature_c == null ? "温度未知" : `${status.temperature_c}℃`;
      thermalStrip.querySelector("[data-thermal-message]").textContent = status.message || "状态未知";
      const detail = document.querySelector("[data-thermal-detail]");
      if (detail) detail.textContent = status.temperature_c == null ? "温度未知" : `${status.temperature_c}℃`;
      const message = document.querySelector("[data-thermal-detail-message]");
      if (message) message.textContent = status.message || "状态未知";
      const sampled = document.querySelector("[data-thermal-sampled]");
      if (sampled) sampled.textContent = `最近采样：${status.last_sample_at || "暂无"}`;
      const dailyMax = document.querySelector("[data-thermal-daily-max]");
      if (dailyMax) dailyMax.textContent = status.daily_max_c == null ? "暂无数据" : `${status.daily_max_c}℃`;
      const chart = document.querySelector("[data-thermal-chart]");
      if (chart) {
        chart.replaceChildren();
        const samples = Array.isArray(status.samples) ? status.samples.slice(-720) : [];
        let points = [];
        let previousTime = null;
        const flush = () => {
          if (points.length > 1) {
            const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
            line.setAttribute("points", points.join(" "));
            line.setAttribute("fill", "none");
            line.setAttribute("stroke", "#a63f36");
            line.setAttribute("stroke-width", "2");
            chart.append(line);
          }
          points = [];
        };
        samples.forEach((sample, index) => {
          const time = Date.parse(sample.at);
          const degrees = sample.temperature_c;
          if (!Number.isFinite(time) || !Number.isFinite(degrees) ||
              (previousTime != null && time - previousTime > 10000)) flush();
          if (Number.isFinite(time) && Number.isFinite(degrees)) {
            const x = samples.length < 2 ? 0 : index * 719 / (samples.length - 1);
            const y = Math.max(0, Math.min(100, (100 - degrees) * 100 / 70));
            points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
          } else flush();
          previousTime = Number.isFinite(time) ? time : null;
        });
        flush();
      }
      const resume = document.querySelector("[data-thermal-resume]");
      if (resume) resume.disabled = !status.recovery_ready;
    } catch {
      thermalStrip.className = "thermal-strip thermal-unavailable";
      thermalStrip.querySelector("[data-thermal-temperature]").textContent = "温度未知";
      thermalStrip.querySelector("[data-thermal-message]").textContent = "监测状态无法连接";
    }
  };
  updateThermal();
  setInterval(updateThermal, 5000);
  document.querySelector("[data-thermal-resume]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await fetch("/api/thermal/resume", {
        method: "POST", headers: {"X-CSRF-Token": button.dataset.csrf || ""},
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "恢复请求失败");
      showStatus(result.message);
      setTimeout(updateThermal, 1000);
    } catch (error) {
      showStatus(error.message, true);
      updateThermal();
    }
  });
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest(".compose-form");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button");
  const original = button.textContent;
  const data = new FormData(form);
  const payload = {
    asset_ids: data.getAll("asset_ids"),
    subtitle_asset_id: data.get("subtitle_asset_id") || null,
    aspect_ratio: data.get("aspect_ratio"),
    priority: Number(data.get("priority")),
    upscale: data.get("upscale") === "1",
  };
  button.disabled = true;
  button.textContent = "正在入队…";
  try {
    const response = await fetch(`/api/projects/${form.dataset.project}/compose`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": data.get("csrf_token") || ""},
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "合成任务入队失败");
    button.textContent = "已加入队列";
    showStatus("合成任务已加入队列");
    setTimeout(() => location.reload(), 500);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showStatus(error.message, true);
  }
});
