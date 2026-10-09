let statusTimer;

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
