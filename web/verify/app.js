const state = {
  queue: [],
  index: 0,
  current: null,
  verified: 0,
  totalSource: 0,
  recorder: null,
  chunks: [],
  stream: null,
};

const els = {
  word: document.getElementById("word"),
  ipa: document.getElementById("ipa"),
  transcript: document.getElementById("transcript"),
  progress: document.getElementById("progress"),
  btnBack: document.getElementById("btn-back"),
  btnPlay: document.getElementById("btn-play"),
  btnApprove: document.getElementById("btn-approve"),
  btnEdit: document.getElementById("btn-edit"),
  btnStop: document.getElementById("btn-stop"),
  btnSkip: document.getElementById("btn-skip"),
  status: document.getElementById("status"),
  audio: document.getElementById("audio"),
};

function setStatus(msg, cls = "") {
  els.status.textContent = msg;
  els.status.className = "status" + (cls ? " " + cls : "");
}

async function loadQueue() {
  setStatus("Loading queue...");
  try {
    const res = await fetch("/api/queue");
    const data = await res.json();
    state.queue = data.queue;
    state.verified = data.verified;
    state.totalSource = data.total_source;
    state.index = 0;
    if (state.queue.length === 0) {
      els.word.textContent = "All words verified.";
      els.ipa.value = "";
      els.progress.textContent = `${state.verified} verified`;
      setStatus("Nothing left to do.");
      return;
    }
    showCurrent();
  } catch (e) {
    setStatus("Failed to load queue: " + e.message, "error");
  }
}

function showCurrent() {
  if (state.index >= state.queue.length) {
    els.word.textContent = "Done.";
    els.ipa.value = "";
    setStatus(`Verified ${state.verified} words.`);
    return;
  }
  state.current = state.queue[state.index];
  els.word.textContent = state.current.word;
  els.ipa.value = state.current.ipa;
  els.transcript.textContent = "";
  els.progress.textContent = `${state.verified} verified / ${state.totalSource} (${state.queue.length - state.index} left)`;
  setStatus("");
  // Auto-play after a beat so the audio element is ready.
  setTimeout(playCurrent, 150);
}

async function playCurrent() {
  const word = state.current && state.current.word;
  const ipa = els.ipa.value.trim();
  if (!word || !ipa) {
    setStatus("Nothing to play.", "error");
    return;
  }
  setStatus("Synthesizing...");
  try {
    const res = await fetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word, ipa }),
    });
    if (!res.ok) {
      const err = await safeJson(res);
      setStatus("Polly: " + (err.error || res.statusText), "error");
      return;
    }
    const blob = await res.blob();
    els.audio.src = URL.createObjectURL(blob);
    try {
      await els.audio.play();
    } catch (playErr) {
      // Browsers block autoplay until first user gesture; ignore silently.
      setStatus("Tap Play (or hit space) to hear it.");
      return;
    }
    setStatus("");
  } catch (e) {
    setStatus("Playback failed: " + e.message, "error");
  }
}

async function approve() {
  const word = state.current && state.current.word;
  const ipa = els.ipa.value.trim();
  if (!word || !ipa) {
    setStatus("Need a word and non-empty IPA.", "error");
    return;
  }
  setStatus("Saving...");
  try {
    const res = await fetch("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word, ipa }),
    });
    if (!res.ok) {
      const err = await safeJson(res);
      setStatus("Save failed: " + (err.error || res.statusText), "error");
      return;
    }
    const data = await res.json();
    state.verified = data.verified;
    if (data.paired) {
      // Drop any later queued occurrence of the paired word.
      for (let i = state.index + 1; i < state.queue.length; i++) {
        if (state.queue[i].word === data.paired.word) {
          state.queue.splice(i, 1);
          break;
        }
      }
      // Inject the paired entry as the very next item.
      state.queue.splice(state.index + 1, 0, data.paired);
      setStatus(`Saved override. Paired "${data.paired.word}" queued.`);
    } else if (data.changed) {
      setStatus("Saved override.");
    } else {
      setStatus("Verified (unchanged).");
    }
    state.index++;
    showCurrent();
  } catch (e) {
    setStatus("Save failed: " + e.message, "error");
  }
}

function skip() {
  state.index++;
  showCurrent();
}

async function back() {
  if (state.index === 0) {
    setStatus("Already at first entry.");
    return;
  }
  state.index--;
  const target = state.queue[state.index];
  try {
    const res = await fetch("/api/unverify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word: target.word }),
    });
    if (res.ok) {
      const data = await res.json();
      state.verified = data.verified;
    }
  } catch (e) {
    // Non-fatal; still show the previous entry.
  }
  showCurrent();
  setStatus(`Back to "${target.word}" (un-verified).`);
}

async function startEdit() {
  try {
    if (!state.stream) {
      state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    state.recorder = new MediaRecorder(state.stream);
    state.chunks = [];
    state.recorder.addEventListener("dataavailable", (e) => {
      if (e.data && e.data.size > 0) state.chunks.push(e.data);
    });
    state.recorder.addEventListener("stop", onRecordingStop);
    state.recorder.start();
    els.btnEdit.hidden = true;
    els.btnStop.hidden = false;
    setStatus("Recording... say the word.", "recording");
  } catch (e) {
    setStatus("Mic error: " + e.message, "error");
  }
}

function stopEdit() {
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
  }
  els.btnStop.hidden = true;
  els.btnEdit.hidden = false;
}

async function onRecordingStop() {
  const word = state.current && state.current.word;
  if (!word) return;
  if (state.chunks.length === 0) {
    setStatus("No audio captured.", "error");
    return;
  }
  setStatus("Transcribing...");
  try {
    const mimeType = state.recorder.mimeType || "audio/webm";
    const blob = new Blob(state.chunks, { type: mimeType });
    const audioB64 = await blobToBase64(blob);
    const res = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        word,
        audio_base64: audioB64,
        content_type: mimeType,
      }),
    });
    if (!res.ok) {
      const err = await safeJson(res);
      setStatus("Transcribe: " + (err.error || res.statusText), "error");
      return;
    }
    const data = await res.json();
    els.ipa.value = data.ipa;
    els.transcript.textContent = `Heard: "${data.transcript}"`;
    setStatus("");
    setTimeout(playCurrent, 150);
  } catch (e) {
    setStatus("Transcribe failed: " + e.message, "error");
  }
}

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

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return {};
  }
}

els.btnBack.addEventListener("click", back);
els.btnPlay.addEventListener("click", playCurrent);
els.btnApprove.addEventListener("click", approve);
els.btnEdit.addEventListener("click", startEdit);
els.btnStop.addEventListener("click", stopEdit);
els.btnSkip.addEventListener("click", skip);

document.addEventListener("keydown", (e) => {
  if (e.target === els.ipa) return;
  if (e.key === " ") {
    e.preventDefault();
    playCurrent();
  } else if (e.key === "Enter") {
    e.preventDefault();
    approve();
  } else if (e.key === "e" || e.key === "E") {
    if (state.recorder && state.recorder.state === "recording") {
      stopEdit();
    } else {
      startEdit();
    }
  } else if (e.key === "n" || e.key === "N") {
    skip();
  } else if (e.key === "b" || e.key === "B") {
    back();
  }
});

loadQueue();
