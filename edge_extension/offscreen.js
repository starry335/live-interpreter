const REQUEST_HEADERS = {
  "Content-Type": "application/octet-stream",
  "X-Live-Interpreter": "edge-extension"
};

let audioContext = null;
let mediaStream = null;
let processor = null;
let source = null;
let video = null;
let visualTimer = null;
let baseUrl = null;
let pendingPost = Promise.resolve();
let captureGeneration = 0;

function resampleTo16k(input, inputRate) {
  const outputLength = Math.max(1, Math.round(input.length * 16000 / inputRate));
  const output = new Int16Array(outputLength);
  const ratio = input.length / outputLength;
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(input.length - 1, left + 1);
    const value = input[left] + (input[right] - input[left]) * (position - left);
    output[index] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
  }
  return output;
}

async function post(path, body, contentType = "application/octet-stream") {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { ...REQUEST_HEADERS, "Content-Type": contentType },
      body,
      signal: controller.signal
    });
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) throw new Error(`Backend ${path} returned HTTP ${response.status}.`);
}

async function stopCapture() {
  captureGeneration += 1;
  if (processor) processor.disconnect();
  if (source) source.disconnect();
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  if (visualTimer) clearInterval(visualTimer);
  if (video) video.srcObject = null;
  if (audioContext) await audioContext.close().catch(() => {});
  processor = null;
  source = null;
  video = null;
  visualTimer = null;
  mediaStream = null;
  audioContext = null;
  pendingPost = Promise.resolve();
  if (baseUrl) await post("/audio/stop").catch(() => {});
}

function jpegFrame() {
  return new Promise((resolve) => {
    if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
      resolve(null);
      return;
    }
    const scale = Math.min(1, 960 / video.videoWidth, 540 / video.videoHeight);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(resolve, "image/jpeg", 0.72);
  });
}

async function uploadVisualFrame() {
  const frame = await jpegFrame();
  if (frame && frame.size <= 500000) await post("/visual", frame, "image/jpeg");
}

async function startCapture(streamId, nextBaseUrl, visualEnabled) {
  await stopCapture();
  baseUrl = nextBaseUrl;
  await post("/audio/start");
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId
      }
    },
    video: visualEnabled ? {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
        maxWidth: 1280,
        maxHeight: 720,
        maxFrameRate: 2
      }
    } : false
  });
  audioContext = new AudioContext();
  await audioContext.resume();
  source = audioContext.createMediaStreamSource(mediaStream);

  if (visualEnabled) {
    video = document.createElement("video");
    video.srcObject = mediaStream;
    video.muted = true;
    await video.play();
    visualTimer = setInterval(() => uploadVisualFrame().catch(() => {}), 1000);
  }

  // tabCapture suppresses normal tab playback; reconnect it for the viewer.
  source.connect(audioContext.destination);
  processor = audioContext.createScriptProcessor(4096, 1, 1);
  const generation = captureGeneration;
  processor.onaudioprocess = (event) => {
    const pcm = resampleTo16k(event.inputBuffer.getChannelData(0), audioContext.sampleRate);
    pendingPost = pendingPost
      .then(() => generation === captureGeneration && post("/audio", pcm.buffer))
      .catch((error) => chrome.runtime.sendMessage({ type: "capture-error", error: error.message }));
  };
  source.connect(processor);
  processor.connect(audioContext.destination);
  mediaStream.getAudioTracks()[0].addEventListener("ended", stopCapture, { once: true });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.target !== "offscreen") return false;
  const action = message.type === "start-capture"
    ? startCapture(message.streamId, message.baseUrl, message.visualEnabled).catch(async (error) => {
        await stopCapture();
        throw error;
      })
    : stopCapture();
  action.then(
    () => sendResponse({ ok: true }),
    (error) => {
      chrome.runtime.sendMessage({ type: "capture-error", error: error.message });
      sendResponse({ ok: false, error: error.message });
    }
  );
  return true;
});
