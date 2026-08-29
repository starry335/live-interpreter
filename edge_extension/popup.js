const portInput = document.querySelector("#port");
const fontSizeInput = document.querySelector("#font-size");
const holdSecondsInput = document.querySelector("#hold-seconds");
const showSourceInput = document.querySelector("#show-source");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
const statusText = document.querySelector("#status");
const indicator = document.querySelector("#indicator");

function setStatus(text, active = false) {
  statusText.textContent = text;
  indicator.classList.toggle("active", active);
}

async function refresh() {
  const state = await chrome.storage.local.get({
    port: 8765,
    fontSize: 16,
    holdSeconds: 8,
    showSource: false,
    captureError: "",
    capturingTabId: null
  });
  portInput.value = state.port;
  fontSizeInput.value = state.fontSize;
  holdSecondsInput.value = state.holdSeconds;
  showSourceInput.checked = state.showSource;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const active = Boolean(tab && state.capturingTabId === tab.id);
  startButton.disabled = active;
  stopButton.disabled = !state.capturingTabId;
  setStatus(active ? "正在翻译当前标签页" : state.captureError || "准备就绪", active);
}

startButton.addEventListener("click", async () => {
  const port = Math.max(1024, Math.min(65535, Number(portInput.value) || 8765));
  await chrome.storage.local.set({ port, showSource: showSourceInput.checked, captureError: "" });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) {
    setStatus("请打开普通网页或视频页面");
    return;
  }
  startButton.disabled = true;
  setStatus("正在连接本地服务...");
  const result = await chrome.runtime.sendMessage({ type: "start-current-tab", tabId: tab.id });
  if (!result?.ok) {
    startButton.disabled = false;
    setStatus(result?.error || "启动失败");
    return;
  }
  await refresh();
});

stopButton.addEventListener("click", async () => {
  stopButton.disabled = true;
  await chrome.runtime.sendMessage({ type: "stop-current-tab" });
  await refresh();
});

showSourceInput.addEventListener("change", () => {
  chrome.storage.local.set({ showSource: showSourceInput.checked });
});

fontSizeInput.addEventListener("input", () => {
  const fontSize = Math.max(14, Math.min(48, Number(fontSizeInput.value) || 16));
  chrome.storage.local.set({ fontSize });
});

holdSecondsInput.addEventListener("input", () => {
  const holdSeconds = Math.max(2, Math.min(30, Number(holdSecondsInput.value) || 8));
  chrome.storage.local.set({ holdSeconds });
});

refresh();
