document.querySelectorAll("[data-recipe-select]").forEach((select) => {
  const hint = select.parentElement.querySelector("[data-recipe-hint]");
  const update = () => { if (hint) hint.textContent = select.selectedOptions[0]?.dataset.hint || ""; };
  select.addEventListener("change", update);
  update();
});

const writingPage = document.querySelector("[data-writing-page]");
if (writingPage) {
  const editor = writingPage.querySelector("[data-writing-editor]");
  let dirty = editor?.dataset.retained === "yes";
  const updates = writingPage.querySelector("[data-writing-updates]");
  const generation = writingPage.querySelector("[data-writing-generation]");
  const runStatus = writingPage.querySelector("[data-writing-status]");
  const checkButton = writingPage.querySelector("[data-writing-check]");
  let checking = false;
  const notify = (text) => {
    updates.hidden = false;
    updates.querySelector("p").textContent = text;
  };
  editor?.addEventListener("input", () => {
    dirty = true;
    editor.querySelector("[data-writing-dirty]").textContent = "有未保存的修改，请先保存";
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  writingPage.addEventListener("submit", (event) => {
    if (event.target.matches("[data-requires-saved]") && dirty) {
      event.preventDefault();
      notify("请先保存编辑稿，再改写、切换版本或确认文案。你的输入不会被替换。");
      editor.querySelector("button").focus();
    } else if (event.target === generation) {
      if (writingPage.dataset.writingBusy === "yes") {
        event.preventDefault();
        notify("已有文案任务正在排队或运行，请等待结果。仍可保存编辑稿。");
      } else {
        writingPage.dataset.writingBusy = "yes";
        setTimeout(() => generation.querySelectorAll("button").forEach((button) => { button.disabled = true; }), 0);
      }
    } else if (event.target === editor) {
      dirty = false;
    }
  });
  writingPage.querySelector("[data-writing-reload]")?.addEventListener("click", () => {
    if (dirty) { notify("先保存修改，再查看新结果。你的输入仍在编辑区。"); return; }
    location.reload();
  });
  const poll = async () => {
    if (checking) return;
    checking = true;
    checkButton.disabled = true;
    try {
      const response = await fetch(writingPage.dataset.statusUrl, {cache: "no-store"});
      if (!response.ok) throw new Error("状态请求失败");
      const status = await response.json();
      const activity = status.activity;
      runStatus.dataset.state = activity.state;
      runStatus.querySelector("[data-writing-status-label]").textContent = activity.label;
      runStatus.querySelector("[data-writing-status-detail]").textContent = activity.detail;
      runStatus.querySelector("[data-writing-status-check]").textContent = `最近检查：${new Date().toLocaleTimeString("zh-CN")} · 每 5 秒自动更新`;
      writingPage.dataset.writingBusy = activity.active_count ? "yes" : "no";
      generation?.querySelectorAll("button").forEach((button) => {
        button.disabled = activity.active_count > 0 || generation.dataset.writerReady !== "yes";
      });
      if (status.latest_version !== Number(writingPage.dataset.latest)) {
        if (!dirty && writingPage.dataset.hasSelection === "no") { location.reload(); return; }
        notify("有新版本或候选文案。你的编辑稿保持不变，保存后可查看。");
      }
    } catch (_) {
      runStatus.querySelector("[data-writing-status-check]").textContent = "暂时无法检查状态，显示的是上次结果。请点击检查重试；编辑稿仍保留。";
    } finally {
      checking = false;
      checkButton.disabled = false;
    }
  };
  checkButton.addEventListener("click", poll);
  poll();
  setInterval(() => { if (document.visibilityState === "visible") poll(); }, 5000);
}
