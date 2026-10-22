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
    } else if (event.target === editor) {
      dirty = false;
    }
  });
  writingPage.querySelector("[data-writing-reload]")?.addEventListener("click", () => {
    if (dirty) { notify("先保存修改，再查看新结果。你的输入仍在编辑区。"); return; }
    location.reload();
  });
  const poll = async () => {
    try {
      const response = await fetch(writingPage.dataset.statusUrl, {cache: "no-store"});
      if (!response.ok) return;
      const status = await response.json();
      if (status.latest_version !== Number(writingPage.dataset.latest)) {
        if (!dirty && writingPage.dataset.hasSelection === "no") { location.reload(); return; }
        notify("有新版本或候选文案。你的编辑稿保持不变，保存后可查看。");
      } else if (status.tasks.some((task) => task.status === "terminal_failed")) {
        notify("有生成任务未完成。原有文案已保留，可刷新查看原因后重试。");
      }
    } catch (_) { /* A closed or restarting local service must not erase the editor. */ }
  };
  setInterval(() => { if (document.visibilityState === "visible") poll(); }, 5000);
}
