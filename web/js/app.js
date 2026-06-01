import * as settings from "./settings.js";
import * as speech from "./speech.js";
import { randomQuote } from "./quotes.js";
import { render as renderMarkdown, toPlainText } from "./markdown.js";

const chat = document.getElementById("chat");
const questionEl = document.getElementById("question");
const sendBtn = document.getElementById("send");
const banner = document.getElementById("config-banner");
const muteBtn = document.getElementById("mute-toggle");
const testBtn = document.getElementById("test-toggle");
const gearBtn = document.getElementById("gear-toggle");
const panel = document.getElementById("settings-panel");
const micBtn = document.getElementById("mic");

const endpoint = settings.apiEndpoint();
if (!endpoint || endpoint === "REPLACE_WITH_API_GATEWAY_URL") {
  banner.style.display = "block";
}

function escape(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function addBubble(cls, html, { speakable = null } = {}) {
  const div = document.createElement("div");
  div.className = `bubble ${cls}`;
  div.innerHTML = html;
  if (speakable && cls === "agent") {
    const btn = document.createElement("button");
    btn.className = "speaker-btn";
    btn.title = "Read aloud";
    btn.textContent = "🔊";
    btn.addEventListener("click", () => playBubble(btn, speakable));
    const label = div.querySelector(".label");
    if (label) label.appendChild(btn); else div.prepend(btn);
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

async function playBubble(btn, text) {
  btn.classList.add("playing");
  await speech.speak(text, settings.get("voice"), {
    onEnd: () => {
      btn.classList.remove("playing");
    },
  });
}

async function ask() {
  const question = questionEl.value.trim();
  if (!question) return;

  questionEl.value = "";
  sendBtn.disabled = true;

  addBubble("user", escape(question));
  const thinking = addBubble("thinking", "Consulting the archives…");

  try {
    let answer, sources;

    if (settings.get("testMode")) {
      await new Promise(r => setTimeout(r, 600));
      answer = randomQuote();
      sources = [{ excerpt: "Test mode — no retrieval. Quote drawn from local bank.", score: null }];
    } else {
      const res = await fetch(`${endpoint}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      const data = await res.json();
      thinking.remove();
      if (!res.ok) {
        addBubble("agent", `<div class="label">Error</div>${escape(data.error || "Unknown error")}`);
        return;
      }
      answer = data.answer;
      sources = data.sources;
    }

    if (settings.get("testMode")) thinking.remove();

    let html = `<div class="label">Elden Ring Guide</div>${renderMarkdown(answer)}`;
    if (sources && sources.length) {
      const items = sources
        .filter(s => s.excerpt)
        .map(s => `<p>• ${escape(s.excerpt)}${s.score != null ? ` (score: ${s.score.toFixed(3)})` : ""}</p>`)
        .join("");
      html += `<details class="sources"><summary>Sources (${sources.length})</summary>${items}</details>`;
    }
    // TTS reads plain prose, not the Markdown markers.
    const spoken = toPlainText(answer);
    const bubble = addBubble("agent", html, { speakable: spoken });

    if (settings.get("autoSpeak")) {
      const speakerBtn = bubble.querySelector(".speaker-btn");
      if (speakerBtn) playBubble(speakerBtn, spoken);
    }
  } catch (err) {
    thinking.remove();
    addBubble("agent", `<div class="label">Network Error</div>${escape(String(err))}`);
  } finally {
    sendBtn.disabled = false;
    questionEl.focus();
  }
}

sendBtn.addEventListener("click", ask);
questionEl.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
});

// ── Header toggle wiring ─────────────────────────────────────────────────────
function refreshToggles() {
  muteBtn.classList.toggle("active", settings.get("autoSpeak"));
  muteBtn.textContent = settings.get("autoSpeak") ? "🔊 Voice on" : "🔇 Muted";
  testBtn.classList.toggle("active", settings.get("testMode"));
}
refreshToggles();
settings.onChange(refreshToggles);

muteBtn.addEventListener("click", () => {
  const next = !settings.get("autoSpeak");
  settings.set("autoSpeak", next);
  if (!next) speech.cancelCurrentAudio();
});

testBtn.addEventListener("click", () => settings.set("testMode", !settings.get("testMode")));

gearBtn.addEventListener("click", () => {
  if (!panel.classList.contains("open")) settings.renderSettingsPanel(panel);
  panel.classList.toggle("open");
});

document.addEventListener("click", e => {
  if (!panel.contains(e.target) && e.target !== gearBtn) panel.classList.remove("open");
});

// ── Mic / STT wiring ─────────────────────────────────────────────────────────
// Press once to start recording, press again to stop. On stop the whole take
// is transcribed and submitted as a question.
let transcribingBubble = null;
function clearTranscribing() {
  if (transcribingBubble) { transcribingBubble.remove(); transcribingBubble = null; }
}

function triggerMic() {
  if (!speech.sttSupported) return;
  if (speech.isRecording()) {
    speech.stopRecording();
    return;
  }
  speech.startRecording({
    onStart: () => {
      micBtn.classList.add("recording");
      questionEl.classList.add("listening");
    },
    onTranscribeStart: () => {
      transcribingBubble = addBubble("thinking transcribing", "Scribing…");
    },
    onTranscribeEnd: clearTranscribing,
    onUtterance: (text) => {
      // Replace the "Transcribing…" indicator with the question + its answer.
      clearTranscribing();
      questionEl.value = text;
      ask();
    },
    onStop: () => {
      micBtn.classList.remove("recording");
      questionEl.classList.remove("listening");
    },
  });
}

if (!speech.sttSupported) {
  micBtn.classList.add("unsupported");
} else {
  micBtn.addEventListener("click", () => {
    triggerMic();
    // Drop focus so the default Space keybind doesn't also "click" the button.
    micBtn.blur();
  });
  speech.bindMicKeybind(() => settings.get("micKeybind"), triggerMic);
}

// ── Greeting ─────────────────────────────────────────────────────────────────
addBubble(
  "agent",
  `<div class="label">Elden Ring Guide</div>
   Welcome, Tarnished. Ask me about enemies, item locations, boss strategies,
   or what can be found near a specific area. I draw on the Fextralife wiki
   and game data for grounded answers.`,
  { speakable: "Welcome, Tarnished. Ask me about enemies, item locations, boss strategies, or what can be found near a specific area." }
);
