const STORAGE_KEY = "eldenring.settings.v1";

const DEFAULTS = {
  autoSpeak: true,
  voice: "Stephen",
  micKeybind: "Space",
  testMode: false,
};

const VOICES = ["Stephen", "Joanna", "Matthew", "Amy", "Brian", "Ruth"];

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULTS };
  }
}

let state = load();

function persist() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch {}
}

export function get(key) { return state[key]; }

export function set(key, value) {
  state[key] = value;
  persist();
  for (const fn of listeners) fn(key, value);
}

const listeners = new Set();
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function apiEndpoint() {
  const meta = document.querySelector('meta[name="api-endpoint"]');
  return meta ? meta.content : "";
}

export function renderSettingsPanel(panel) {
  panel.innerHTML = `
    <div class="row">
      <label for="voice-select">Voice</label>
      <select id="voice-select">
        ${VOICES.map(v => `<option value="${v}"${v === state.voice ? " selected" : ""}>${v}</option>`).join("")}
      </select>
    </div>
    <div class="row">
      <label>Mic keybind</label>
      <span class="keybind-capture" id="keybind-capture">${state.micKeybind}</span>
    </div>
  `;

  panel.querySelector("#voice-select").addEventListener("change", e => {
    set("voice", e.target.value);
  });

  const capture = panel.querySelector("#keybind-capture");
  capture.addEventListener("click", () => {
    capture.classList.add("capturing");
    capture.textContent = "Press a key…";
    const onKey = e => {
      e.preventDefault();
      const key = e.code || e.key;
      set("micKeybind", key);
      capture.textContent = key;
      capture.classList.remove("capturing");
      window.removeEventListener("keydown", onKey, true);
    };
    window.addEventListener("keydown", onKey, true);
  });
}
