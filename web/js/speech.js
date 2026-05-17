import * as settings from "./settings.js";

// STT support: MediaRecorder + getUserMedia (much wider browser coverage than
// the old webkitSpeechRecognition path).
export const sttSupported = Boolean(
  navigator.mediaDevices &&
  navigator.mediaDevices.getUserMedia &&
  typeof MediaRecorder !== "undefined" &&
  (window.AudioContext || window.webkitAudioContext)
);

let currentAudio = null;
const ttsCache = new Map();

async function hash(s) {
  const data = new TextEncoder().encode(s);
  const buf = await crypto.subtle.digest("SHA-1", data);
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("");
}

export function cancelCurrentAudio() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.currentTime = 0;
    currentAudio = null;
  }
}

export async function speak(text, voice, { onStart, onEnd } = {}) {
  cancelCurrentAudio();
  const trimmed = text.trim();
  if (!trimmed) return;

  const key = await hash(`${voice}::${trimmed}`);
  let dataUrl = ttsCache.get(key);

  if (!dataUrl) {
    const endpoint = settings.apiEndpoint();
    if (!endpoint || endpoint === "REPLACE_WITH_API_GATEWAY_URL") {
      console.warn("speak(): API endpoint not configured");
      return;
    }
    const res = await fetch(`${endpoint}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: trimmed, voice }),
    });
    if (!res.ok) {
      console.warn("speak(): /speak returned", res.status);
      return;
    }
    const data = await res.json();
    if (!data.audio_base64) return;
    dataUrl = `data:${data.content_type || "audio/mpeg"};base64,${data.audio_base64}`;
    ttsCache.set(key, dataUrl);
  }

  const audio = new Audio(dataUrl);
  currentAudio = audio;
  if (onStart) onStart();
  audio.addEventListener("ended", () => {
    if (currentAudio === audio) currentAudio = null;
    if (onEnd) onEnd();
  });
  audio.addEventListener("error", () => {
    if (currentAudio === audio) currentAudio = null;
    if (onEnd) onEnd();
  });
  try { await audio.play(); }
  catch (err) { console.warn("audio.play() failed:", err); if (onEnd) onEnd(); }
}

// ── STT (conversation mode, Whisper-backed) ──────────────────────────────────
//
// Lifecycle:
//   startConversation() → grab mic, run a silence-VAD loop. Each time speech
//                         starts and then stops (~700ms of silence), we cut
//                         the current MediaRecorder chunk, POST it to
//                         /transcribe, and fire onUtterance() with the result.
//   suppressForTTS()    → answer is being read aloud; pause VAD + recorder to
//                         keep the TTS audio out of the next transcript.
//   resumeAfterTTS()    → re-enable VAD + recorder.
//   stopConversation()  → release mic, tear down audio graph.
//
// We rely on AnalyserNode in time-domain mode to compute short-window RMS and
// drive a tiny state machine. We do NOT use Web Speech API anywhere.

const VAD = {
  // RMS thresholds on a normalized [-1,1] waveform.
  speechStart: 0.025,
  speechEnd: 0.015,
  // Hysteresis windows.
  silenceMsToFinalize: 1000,
  minUtteranceMs: 250,
  // Cap so a single utterance can't grow without bound.
  maxUtteranceMs: 15000,
  // Cap to avoid sending barely-anything chunks (smaller blobs are silence).
  minUtteranceBytes: 1500,
};

const conv = {
  active: false,
  suppressed: false,
  callbacks: null,
  stream: null,
  audioCtx: null,
  analyser: null,
  recorder: null,
  chunks: [],
  speaking: false,
  utteranceStartedAt: 0,
  lastSpeechAt: 0,
  rafHandle: 0,
  // Per-recording state so we can decide what to do on the async 'stop' event.
  recordingMode: null, // "finalize" | "abort"
  // Set by stopConversation() when it wants the in-flight utterance flushed
  // before teardown. Consumed at the tail of finalizeUtterance().
  pendingStop: false,
};

function getAudioCtx() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  return new Ctx();
}

function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const t of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) {
      return t;
    }
  }
  return "";
}

function buildRecorder() {
  const mime = pickMimeType();
  const opts = mime ? { mimeType: mime } : undefined;
  const rec = new MediaRecorder(conv.stream, opts);
  conv.chunks = [];
  conv.recordingMode = "abort";
  rec.addEventListener("dataavailable", e => {
    if (e.data && e.data.size > 0) conv.chunks.push(e.data);
  });
  rec.addEventListener("stop", () => {
    const mode = conv.recordingMode;
    const chunks = conv.chunks;
    conv.chunks = [];
    conv.recordingMode = null;
    if (mode === "finalize") {
      const blob = new Blob(chunks, { type: rec.mimeType || mime || "audio/webm" });
      finalizeUtterance(blob);
      return;
    }
    // abort path: if stopConversation is waiting on us, finish the teardown;
    // otherwise rearm for the next utterance.
    if (conv.pendingStop) {
      completeStop();
    } else if (conv.active && !conv.suppressed) {
      armRecorder();
    }
  });
  rec.start();
  conv.recorder = rec;
}

function armRecorder() {
  if (!conv.active || conv.suppressed) return;
  if (conv.recorder && conv.recorder.state !== "inactive") return;
  try { buildRecorder(); }
  catch (err) { console.warn("MediaRecorder build failed:", err); }
}

function stopRecorder(mode) {
  conv.recordingMode = mode;
  const rec = conv.recorder;
  conv.recorder = null;
  if (rec && rec.state !== "inactive") {
    try { rec.stop(); } catch {}
  }
}

async function finalizeUtterance(blob) {
  // Skip blobs that are obviously silence/background noise.
  if (blob.size < VAD.minUtteranceBytes) {
    afterFinalize();
    return;
  }
  const endpoint = settings.apiEndpoint();
  if (!endpoint || endpoint === "REPLACE_WITH_API_GATEWAY_URL") {
    console.warn("/transcribe: API endpoint not configured");
    afterFinalize();
    return;
  }

  try {
    const audioB64 = await blobToBase64(blob);
    const res = await fetch(`${endpoint}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_base64: audioB64, content_type: blob.type || "audio/webm" }),
    });
    if (res.ok) {
      const data = await res.json();
      const text = (data.transcript || "").trim();
      if (text && conv.callbacks && conv.callbacks.onUtterance) {
        conv.callbacks.onUtterance(text);
      }
    } else {
      console.warn("/transcribe returned", res.status);
    }
  } catch (err) {
    console.warn("/transcribe failed:", err);
  } finally {
    afterFinalize();
  }
}

function afterFinalize() {
  // If stopConversation() is waiting for an in-flight finalize, complete the
  // teardown now. Otherwise rearm for the next utterance.
  if (conv.pendingStop) {
    completeStop();
  } else {
    armRecorder();
  }
}

function completeStop() {
  conv.pendingStop = false;
  const cb = conv.callbacks;
  conv.callbacks = null;
  teardown();
  if (cb && cb.onStop) cb.onStop();
}

function vadLoop() {
  if (!conv.active) return;
  conv.rafHandle = requestAnimationFrame(vadLoop);
  if (conv.suppressed || !conv.analyser) return;

  const buf = new Float32Array(conv.analyser.fftSize);
  conv.analyser.getFloatTimeDomainData(buf);
  let sumSq = 0;
  for (let i = 0; i < buf.length; i++) sumSq += buf[i] * buf[i];
  const rms = Math.sqrt(sumSq / buf.length);
  const now = performance.now();

  if (conv.speaking) {
    if (rms > VAD.speechEnd) conv.lastSpeechAt = now;
    const utteranceMs = now - conv.utteranceStartedAt;
    const silenceMs = now - conv.lastSpeechAt;
    const tooLong = utteranceMs > VAD.maxUtteranceMs;
    const longEnough = utteranceMs > VAD.minUtteranceMs;
    if ((silenceMs > VAD.silenceMsToFinalize && longEnough) || tooLong) {
      conv.speaking = false;
      stopRecorder("finalize");
    }
  } else if (rms > VAD.speechStart) {
    conv.speaking = true;
    conv.utteranceStartedAt = now;
    conv.lastSpeechAt = now;
    armRecorder();
  }
}

async function openMic() {
  conv.stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  conv.audioCtx = getAudioCtx();
  if (conv.audioCtx.state === "suspended") {
    try { await conv.audioCtx.resume(); } catch {}
  }
  const src = conv.audioCtx.createMediaStreamSource(conv.stream);
  conv.analyser = conv.audioCtx.createAnalyser();
  conv.analyser.fftSize = 1024;
  conv.analyser.smoothingTimeConstant = 0;
  src.connect(conv.analyser);
}

function teardown() {
  if (conv.rafHandle) cancelAnimationFrame(conv.rafHandle);
  conv.rafHandle = 0;
  if (conv.recorder && conv.recorder.state !== "inactive") {
    try { conv.recorder.stop(); } catch {}
  }
  conv.recorder = null;
  conv.chunks = [];
  if (conv.stream) {
    for (const t of conv.stream.getTracks()) {
      try { t.stop(); } catch {}
    }
  }
  conv.stream = null;
  if (conv.audioCtx) {
    try { conv.audioCtx.close(); } catch {}
  }
  conv.audioCtx = null;
  conv.analyser = null;
  conv.speaking = false;
}

export async function startConversation({ onStart, onUtterance, onStop } = {}) {
  if (!sttSupported || conv.active) return false;
  conv.callbacks = { onStart, onUtterance, onStop };
  try {
    await openMic();
  } catch (err) {
    console.warn("getUserMedia failed:", err);
    const cb = conv.callbacks;
    conv.callbacks = null;
    if (cb && cb.onStop) cb.onStop();
    return false;
  }
  conv.active = true;
  conv.suppressed = false;
  conv.speaking = false;
  armRecorder();
  vadLoop();
  if (onStart) onStart();
  return true;
}

export function stopConversation() {
  if (!conv.active) return;
  conv.active = false;
  conv.suppressed = false;
  if (conv.rafHandle) cancelAnimationFrame(conv.rafHandle);
  conv.rafHandle = 0;

  // If audio is being recorded, decide whether to flush it through /transcribe
  // before teardown. Only flush when VAD thinks the user is mid-utterance —
  // toggling off during silence should not bill Whisper for an empty buffer.
  if (conv.recorder && conv.recorder.state !== "inactive") {
    conv.pendingStop = true;
    const mode = conv.speaking ? "finalize" : "abort";
    conv.speaking = false;
    stopRecorder(mode);
    return;
  }
  conv.speaking = false;
  completeStop();
}

export function suppressForTTS() {
  if (!conv.active || conv.suppressed) return;
  conv.suppressed = true;
  conv.speaking = false;
  // Drop any in-flight utterance; we don't want to ship a partial captured
  // before the TTS started.
  stopRecorder("abort");
}

export function resumeAfterTTS() {
  if (!conv.active || !conv.suppressed) return;
  conv.suppressed = false;
  conv.speaking = false;
  armRecorder();
}

export function isInConversation() { return conv.active; }

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// Global keybind handler. Calls `trigger` when the configured key is pressed
// outside of input/textarea focus.
export function bindMicKeybind(getKey, trigger) {
  window.addEventListener("keydown", e => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    const key = getKey();
    if (!key) return;
    const matched = e.code === key || e.key === key;
    if (matched) {
      e.preventDefault();
      trigger();
    }
  });
}
