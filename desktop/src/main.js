const statusEl = document.getElementById("status");
const hintEl = document.getElementById("hint");
const errorEl = document.getElementById("error");
const shellEl = document.getElementById("shell");

function showError(message) {
  shellEl.classList.add("is-error");
  statusEl.textContent = "本机服务未能启动";
  hintEl.hidden = true;
  errorEl.hidden = false;
  errorEl.textContent = message;
}

function showStatus(message) {
  statusEl.textContent = message || "正在启动本机服务…";
}

async function boot() {
  const tauri = window.__TAURI__;
  if (!tauri?.event?.listen) {
    showError("桌面壳未能加载 Tauri 运行时。请用 npm run tauri dev 启动，而不是直接打开 HTML。");
    return;
  }

  await tauri.event.listen("panel-status", (event) => {
    if (typeof event.payload === "string") {
      showStatus(event.payload);
    }
  });

  await tauri.event.listen("panel-error", (event) => {
    const message =
      typeof event.payload === "string"
        ? event.payload
        : "未知错误：请查看 PKU-All-in-Notion/panel.log。";
    showError(message);
  });
}

boot();
