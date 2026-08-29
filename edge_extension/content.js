(() => {
  if (window.__liveInterpreterOverlay) {
    window.__liveInterpreterOverlay.destroy();
  }
  document.getElementById("live-interpreter-edge-overlay")?.remove();

  const host = document.createElement("div");
  host.id = "live-interpreter-edge-overlay";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .panel {
        --li-font-size: 16px; --li-source-size: 14px;
        position: fixed; left: 50%; bottom: 7%; z-index: 2147483647;
        width: min(980px, 90vw); min-width: 360px; min-height: 120px;
        transform: translateX(-50%); box-sizing: border-box;
        color: #f7f8fa; font-family: "Microsoft YaHei UI", "Noto Sans JP", sans-serif;
        letter-spacing: 0; pointer-events: auto; user-select: none;
      }
      .toolbar {
        position: absolute; top: 0; left: 0; right: 0; height: 40px;
        display: flex; align-items: center; gap: 6px; padding: 0 8px;
        box-sizing: border-box; border: 1px solid rgba(255,255,255,.14);
        border-radius: 7px; background: rgba(20,22,25,.92);
        opacity: 0; pointer-events: none; transition: opacity 150ms ease;
      }
      .panel.editing .toolbar { opacity: 1; pointer-events: auto; }
      .drag { flex: 1; height: 100%; display: flex; align-items: center; gap: 7px;
        color: #d8dce2; font-size: 13px; cursor: move; }
      .status { color: #8f97a2; font-size: 11px; }
      button { width: 30px; height: 30px; padding: 0; border: 0; border-radius: 5px;
        color: #d8dce2; background: transparent; font: 14px "Segoe UI"; cursor: pointer; }
      button:hover { background: rgba(255,255,255,.10); }
      button.active { color: #63b58a; }
      button.close:hover { background: rgba(148,61,66,.36); }
      .subtitles { position: absolute; left: 20px; right: 20px; bottom: 0; }
      .source { margin-bottom: 14px; color: #aeb4bd; font-size: var(--li-source-size); line-height: 1.55;
        white-space: pre-wrap; overflow-wrap: anywhere; text-shadow: 0 1px 3px rgba(0,0,0,.9); }
      .translations { display: flex; flex-direction: column; gap: 9px; }
      .line { color: rgba(247,248,250,.82); font-size: var(--li-font-size); font-weight: 600;
        line-height: 1.65; overflow-wrap: anywhere; text-shadow:
        -1px -1px 1px rgba(0,0,0,.82), 1px -1px 1px rgba(0,0,0,.82),
        -1px 1px 1px rgba(0,0,0,.82), 1px 1px 1px rgba(0,0,0,.82),
        0 2px 4px rgba(0,0,0,.72); }
      .line.current { color: #f7f8fa; }
      .empty { color: rgba(220,224,230,.62); font-size: 14px; text-shadow: 0 1px 3px #000; }
      .hidden { display: none !important; }
    </style>
    <section class="panel hidden" aria-live="polite">
      <div class="toolbar">
        <div class="drag"><span>◎</span><span>翻译为：中文</span><span class="status"></span></div>
        <button class="source-toggle" title="显示或隐藏原文">CC</button>
        <button class="lock" title="锁定位置">◇</button>
        <button class="close" title="关闭字幕">×</button>
      </div>
      <div class="subtitles">
        <div class="source hidden"></div>
        <div class="translations"><div class="empty">等待声音</div></div>
      </div>
    </section>`;

  const panel = shadow.querySelector(".panel");
  const toolbar = shadow.querySelector(".toolbar");
  const dragArea = shadow.querySelector(".drag");
  const status = shadow.querySelector(".status");
  const sourceBox = shadow.querySelector(".source");
  const translations = shadow.querySelector(".translations");
  const sourceToggle = shadow.querySelector(".source-toggle");
  const lockButton = shadow.querySelector(".lock");
  let visible = false;
  let showSource = false;
  let fontSize = 16;
  let holdSeconds = 8;
  let locked = false;
  let hideTimer = 0;
  let pollTimer = 0;
  let destroyed = false;

  function applyFontSize(value) {
    fontSize = Math.max(14, Math.min(48, Number(value) || 16));
    panel.style.setProperty("--li-font-size", `${fontSize}px`);
    panel.style.setProperty("--li-source-size", `${Math.max(12, fontSize - 2)}px`);
  }

  function mount() {
    if (destroyed) return;
    const parent = document.fullscreenElement || document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }

  function revealControls() {
    panel.classList.add("editing");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => panel.classList.remove("editing"), 4000);
  }

  function render(snapshot) {
    status.textContent = snapshot.status || "";
    const now = Number(snapshot.monotonic_now);
    const maximumLines = fontSize >= 32 ? 3 : 6;
    const events = (Number.isFinite(now) ? snapshot.events || [] : []).filter((item) => {
      if (!item.source && !item.translated) return false;
      const updatedAge = now - Number(item.updated_at || now);
      if (!item.final) return updatedAge < Math.max(20, holdSeconds * 2);
      const finalizedAge = now - Number(item.finalized_at || item.updated_at || now);
      return finalizedAge < holdSeconds;
    }).slice(-maximumLines);
    const translated = events.filter((item) => item.translated && item.translated !== "翻译中...");
    translations.replaceChildren();
    if (!translated.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = events.length ? "正在翻译" : "等待声音";
      translations.appendChild(empty);
    } else {
      translated.forEach((item, index) => {
        const line = document.createElement("div");
        line.className = `line${index === translated.length - 1 ? " current" : ""}`;
        line.textContent = item.translated;
        translations.appendChild(line);
      });
    }
    const originals = events.filter((item) => item.source).slice(-2).map((item) => item.source);
    sourceBox.textContent = originals.join("\n");
    sourceBox.classList.toggle("hidden", !showSource || !originals.length);
  }

  async function poll() {
    if (!visible || destroyed) return;
    try {
      const result = await chrome.runtime.sendMessage({ type: "get-subtitles" });
      if (result?.ok) render(result.data);
      else status.textContent = "本地服务未连接";
    } catch (_error) {
      status.textContent = "本地服务未连接";
    }
    pollTimer = setTimeout(poll, 300);
  }

  async function loadPreferences() {
    const values = await chrome.storage.local.get({
      showSource: false,
      fontSize: 16,
      holdSeconds: 8,
      overlayPosition: null
    });
    showSource = values.showSource;
    holdSeconds = Math.max(2, Math.min(30, Number(values.holdSeconds) || 8));
    applyFontSize(values.fontSize);
    sourceToggle.classList.toggle("active", showSource);
    if (values.overlayPosition) {
      panel.style.left = `${values.overlayPosition.left}px`;
      panel.style.top = `${values.overlayPosition.top}px`;
      panel.style.bottom = "auto";
      panel.style.transform = "none";
    }
  }

  function show() {
    if (destroyed) return;
    visible = true;
    mount();
    panel.classList.remove("hidden");
    clearTimeout(pollTimer);
    loadPreferences();
    poll();
  }

  function hide() {
    visible = false;
    clearTimeout(pollTimer);
    panel.classList.add("hidden");
  }

  function destroy() {
    destroyed = true;
    hide();
    clearTimeout(hideTimer);
    host.remove();
  }

  panel.addEventListener("click", revealControls);
  toolbar.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  toolbar.addEventListener("mouseleave", revealControls);
  sourceToggle.addEventListener("click", async (event) => {
    event.stopPropagation();
    showSource = !showSource;
    sourceToggle.classList.toggle("active", showSource);
    await chrome.storage.local.set({ showSource });
  });
  lockButton.addEventListener("click", (event) => {
    event.stopPropagation();
    locked = !locked;
    lockButton.classList.toggle("active", locked);
    lockButton.textContent = locked ? "◆" : "◇";
  });
  shadow.querySelector(".close").addEventListener("click", (event) => {
    event.stopPropagation();
    chrome.runtime.sendMessage({ type: "stop-current-tab" });
    destroy();
  });

  dragArea.addEventListener("pointerdown", (event) => {
    if (locked) return;
    event.preventDefault();
    dragArea.setPointerCapture(event.pointerId);
    const rect = panel.getBoundingClientRect();
    const offsetX = event.clientX - rect.left;
    const offsetY = event.clientY - rect.top;
    const move = (next) => {
      panel.style.left = `${Math.max(0, next.clientX - offsetX)}px`;
      panel.style.top = `${Math.max(0, next.clientY - offsetY)}px`;
      panel.style.bottom = "auto";
      panel.style.transform = "none";
    };
    const up = async () => {
      dragArea.removeEventListener("pointermove", move);
      const position = { left: panel.offsetLeft, top: panel.offsetTop };
      await chrome.storage.local.set({ overlayPosition: position });
      revealControls();
    };
    dragArea.addEventListener("pointermove", move);
    dragArea.addEventListener("pointerup", up, { once: true });
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "li-show") show();
    if (message.type === "li-hide") destroy();
  });
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.showSource) {
      showSource = Boolean(changes.showSource.newValue);
      sourceToggle.classList.toggle("active", showSource);
    }
    if (changes.fontSize) applyFontSize(changes.fontSize.newValue);
    if (changes.holdSeconds) {
      holdSeconds = Math.max(2, Math.min(30, Number(changes.holdSeconds.newValue) || 8));
    }
  });
  document.addEventListener("fullscreenchange", mount);

  window.__liveInterpreterOverlay = { show, hide, destroy };
  show();
})();
