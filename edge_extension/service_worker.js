const DEFAULT_PORT = 8765;

async function settings() {
  const values = await chrome.storage.local.get({ port: DEFAULT_PORT });
  return { port: Number(values.port) || DEFAULT_PORT };
}

async function backendUrl() {
  const { port } = await settings();
  return `http://127.0.0.1:${port}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 3000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function withTimeout(promise, timeoutMs = 2000) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Operation timed out.")), timeoutMs);
      })
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function cleanupOverlay(tabId) {
  if (!tabId) return;
  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      window.__liveInterpreterOverlay?.destroy?.();
      document.getElementById("live-interpreter-edge-overlay")?.remove();
    }
  }).catch(() => {});
}

async function ensureOffscreenDocument() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capture audio and optional visual context from the selected tab."
  });
}

function mediaStreamId(tabId) {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      const error = chrome.runtime.lastError;
      if (error || !streamId) reject(new Error(error?.message || "Unable to capture this tab."));
      else resolve(streamId);
    });
  });
}

async function stopCapture() {
  const { capturingTabId } = await chrome.storage.local.get("capturingTabId");
  await chrome.storage.local.remove("capturingTabId");
  await withTimeout(
    chrome.runtime.sendMessage({ target: "offscreen", type: "stop-capture" }),
    2000
  ).catch(() => {});
  if (capturingTabId) {
    await cleanupOverlay(capturingTabId);
  }
}

async function startCapture(tabId) {
  const baseUrl = await backendUrl();
  const response = await fetchWithTimeout(`${baseUrl}/events`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Live Interpreter backend returned HTTP ${response.status}.`);
  const health = await response.json();
  if (health.service !== "live-interpreter" || Number(health.api_version) < 2) {
    throw new Error("主程序版本过旧，请关闭并重新打开 LiveInterpreter_Edge.exe。");
  }

  await stopCapture();
  await cleanupOverlay(tabId);
  await ensureOffscreenDocument();
  const streamId = await mediaStreamId(tabId);
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  await chrome.tabs.sendMessage(tabId, { type: "li-show" });
  const captureResult = await withTimeout(chrome.runtime.sendMessage({
    target: "offscreen",
    type: "start-capture",
    streamId,
    baseUrl,
    visualEnabled: Boolean(health.visual_enabled)
  }), 10000);
  if (!captureResult?.ok) {
    await cleanupOverlay(tabId);
    throw new Error(captureResult?.error || "Unable to start tab audio capture.");
  }
  await chrome.storage.local.set({ capturingTabId: tabId });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.target === "offscreen") return false;

  if (message.type === "start-current-tab") {
    startCapture(message.tabId).then(
      () => sendResponse({ ok: true }),
      (error) => sendResponse({ ok: false, error: error.message })
    );
    return true;
  }
  if (message.type === "stop-current-tab") {
    stopCapture().then(
      () => sendResponse({ ok: true }),
      (error) => sendResponse({ ok: false, error: error.message })
    );
    return true;
  }
  if (message.type === "get-subtitles") {
    backendUrl()
      .then((url) => fetchWithTimeout(`${url}/events`, { cache: "no-store" }))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "capture-error") {
    stopCapture().finally(() => {
      chrome.storage.local.set({ captureError: message.error || "Audio capture stopped." });
    });
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { capturingTabId } = await chrome.storage.local.get("capturingTabId");
  if (capturingTabId === tabId) await stopCapture();
});
