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

// ── STT (push-to-record, Whisper-backed) ─────────────────────────────────────
//
// Simple start/stop recording. The user toggles the mic (button or keybind):
//   startRecording() → grab mic, start a MediaRecorder capturing everything.
//   stopRecording()  → stop the recorder, POST the whole take to /transcribe,
//                      fire onUtterance() with the result, then release the mic.
//
// There is no voice-activity detection: one toggle-on/toggle-off pair produces
// exactly one transcript. We do NOT use the Web Speech API anywhere.

// Skip blobs that are obviously too small to contain speech (an accidental
// double-tap, or stopping the instant after starting).
const MIN_UTTERANCE_BYTES = 1500;

const rec = {
  active: false,
  callbacks: null,
  stream: null,
  recorder: null,
  chunks: [],
};

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

async function openMic() {
  rec.stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
}

function releaseMic() {
  if (rec.stream) {
    for (const t of rec.stream.getTracks()) {
      try { t.stop(); } catch {}
    }
  }
  rec.stream = null;
}

async function finalizeUtterance(blob, callbacks) {
  // Skip blobs that are obviously silence/background noise.
  if (blob.size < MIN_UTTERANCE_BYTES) return;

  const endpoint = settings.apiEndpoint();
  if (!endpoint || endpoint === "REPLACE_WITH_API_GATEWAY_URL") {
    console.warn("/transcribe: API endpoint not configured");
    return;
  }

  // We're committed to a /transcribe round-trip now — bracket it with the
  // transcribe callbacks so the UI can show a "transcribing…" indicator.
  if (callbacks && callbacks.onTranscribeStart) callbacks.onTranscribeStart();
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
      if (text && callbacks && callbacks.onUtterance) {
        callbacks.onUtterance(text);
      }
    } else {
      console.warn("/transcribe returned", res.status);
    }
  } catch (err) {
    console.warn("/transcribe failed:", err);
  } finally {
    if (callbacks && callbacks.onTranscribeEnd) callbacks.onTranscribeEnd();
  }
}

// Reset all recording state to idle and fire onStop exactly once. Used for both
// the normal stop path and every error/abort path so the two never desync.
function finishTeardown() {
  releaseMic();
  rec.recorder = null;
  rec.chunks = [];
  rec.active = false;
  const cb = rec.callbacks;
  rec.callbacks = null;
  if (cb && cb.onStop) cb.onStop();
}

export async function startRecording(callbacks = {}) {
  if (!sttSupported || rec.active) return false;
  // Claim the active flag synchronously, before any await, so a second toggle
  // that arrives while the mic is still opening can't start a parallel session.
  rec.active = true;
  rec.callbacks = callbacks;

  try {
    await openMic();
  } catch (err) {
    console.warn("getUserMedia failed:", err);
    finishTeardown();
    return false;
  }

  // stopRecording() may have run while we were awaiting the mic.
  if (!rec.active) {
    releaseMic();
    return false;
  }

  const mime = pickMimeType();
  let recorder;
  try {
    recorder = new MediaRecorder(rec.stream, mime ? { mimeType: mime } : undefined);
  } catch (err) {
    console.warn("MediaRecorder build failed:", err);
    finishTeardown();
    return false;
  }

  rec.chunks = [];
  recorder.addEventListener("dataavailable", e => {
    if (e.data && e.data.size > 0) rec.chunks.push(e.data);
  });
  // The 'stop' event is the single teardown path, whether the stop was
  // user-initiated or spontaneous (e.g. the mic track ended).
  recorder.addEventListener("stop", async () => {
    const blob = new Blob(rec.chunks, { type: recorder.mimeType || mime || "audio/webm" });
    const cb = rec.callbacks;
    rec.recorder = null;
    rec.chunks = [];
    rec.active = false;
    rec.callbacks = null;
    releaseMic();
    // Show the stopped state immediately; transcription happens afterward and
    // delivers its result via onUtterance.
    if (cb && cb.onStop) cb.onStop();
    await finalizeUtterance(blob, cb);
  });
  recorder.start();
  rec.recorder = recorder;
  if (callbacks.onStart) callbacks.onStart();
  return true;
}

export function stopRecording() {
  if (!rec.active) return;
  rec.active = false;
  const recorder = rec.recorder;
  if (recorder && recorder.state !== "inactive") {
    // Let the 'stop' event handler transcribe, release the mic, and fire onStop.
    try { recorder.stop(); } catch { finishTeardown(); }
  } else {
    // No live recorder yet — the mic is still opening. startRecording() will
    // observe rec.active === false and bail; tear down whatever exists now.
    finishTeardown();
  }
}

export function isRecording() { return rec.active; }

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
    // Ignore OS key-repeat while the key is held — one press = one toggle.
    if (e.repeat) return;
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
