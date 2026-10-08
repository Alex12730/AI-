document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-toggle]");
  if (!trigger) return;
  const target = document.getElementById(trigger.dataset.toggle);
  if (!target) return;
  target.classList.toggle("is-collapsed");
  if (!target.classList.contains("is-collapsed")) target.querySelector("input, textarea")?.focus();
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
  for (const key of ["duration_seconds", "priority", "estimated_temp_bytes", "seed"]) payload[key] = Number(payload[key]);
  try {
    const response = await fetch(`/api/shots/${form.dataset.shot}/enqueue`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "入队失败");
    button.textContent = "已入队";
    setTimeout(() => location.reload(), 500);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    window.alert(error.message);
  }
});
