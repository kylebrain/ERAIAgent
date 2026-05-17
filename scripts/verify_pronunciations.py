"""
Local web app for verifying / correcting per-word pronunciations from
infra/lexicons/elden_ring_words_filtered.json.

Workflow per word:
  1. Polly synthesizes the current IPA over SSML <phoneme>; browser plays it.
  2. You click Approve, or Edit to re-record. Edit pipes mic audio through
     OpenAI Whisper to get the transcript, then Claude to convert it to IPA.
  3. Approve writes the IPA to infra/lexicons/overrides.json. If the source has
     a matching possessive / non-possessive pair (e.g. "Malenia" <-> "Malenia's"),
     the paired form is queued next with a derived IPA for verification.

Env:
    AWS creds (any default boto3 chain), AWS_REGION (default us-east-1)
    POLLY_VOICE        optional, default Stephen
    OPENAI_API_KEY     required for Edit (Whisper transcription)
    ANTHROPIC_API_KEY  required for Edit (text -> IPA)
    ANTHROPIC_MODEL    optional, default claude-haiku-4-5-20251001

Usage:
    python scripts/verify_pronunciations.py [--port 8765]
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

LEXICON_DIR = Path("infra/lexicons")
WORDS_PATH = LEXICON_DIR / "elden_ring_words_filtered.json"
OVERRIDES_PATH = LEXICON_DIR / "overrides.json"
VERIFIED_PATH = LEXICON_DIR / "elden_ring_words_verified.json"
WEB_ROOT = Path(__file__).resolve().parent.parent / "web" / "verify"

REGION = os.environ.get("AWS_REGION", "us-east-1")
POLLY_VOICE = os.environ.get("POLLY_VOICE", "Stephen")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

IPA_SYSTEM = """You convert spoken English transcriptions into IPA (International Phonetic Alphabet) suitable for AWS Polly SSML <phoneme alphabet="ipa">.

You will receive a target proper noun (often from the video game Elden Ring) and a transcription of how a speaker pronounced it. Produce IPA that matches the speaker's pronunciation as faithfully as you can.

Rules:
- Output ONLY the IPA string. No slashes, brackets, quotes, commentary, or surrounding text.
- Use IPA stress marks (ˈ primary, ˌ secondary) where appropriate.
- Use one cohesive IPA transcription, not multiple variants.
- Prefer narrow, phonetic transcription over abstract phonemic forms.

Example output: məˈlɛniə"""

_polly = None


def polly_client():
    global _polly
    if _polly is None:
        _polly = boto3.client("polly", region_name=REGION)
    return _polly


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_overrides(data: dict) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_verified() -> set[str]:
    if not VERIFIED_PATH.exists():
        return set()
    try:
        return set(json.loads(VERIFIED_PATH.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def save_verified(verified: set[str]) -> None:
    VERIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERIFIED_PATH.write_text(
        json.dumps(sorted(verified), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def source_ipa(words: dict, word: str) -> str:
    entry = words.get(word)
    if entry is None:
        return ""
    raw = entry.get("ipa", "") if isinstance(entry, dict) else entry
    return (raw or "").strip()


def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def synthesize(word: str, ipa: str) -> bytes:
    ssml = (
        '<speak><phoneme alphabet="ipa" ph="'
        + escape_xml(ipa)
        + '">'
        + escape_xml(word)
        + "</phoneme></speak>"
    )
    resp = polly_client().synthesize_speech(
        Engine="neural",
        VoiceId=POLLY_VOICE,
        OutputFormat="mp3",
        TextType="ssml",
        Text=ssml,
    )
    return resp["AudioStream"].read()


def whisper_transcribe(audio_bytes: bytes, content_type: str, hint: str) -> str:
    boundary = "----verify" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="model"',
        b"",
        b"whisper-1",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="prompt"',
        b"",
        hint.encode("utf-8"),
        f"--{boundary}".encode(),
        (
            f'Content-Disposition: form-data; name="file"; filename="audio.webm"'
        ).encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
    ]
    body = crlf.join(parts) + crlf + audio_bytes + crlf + f"--{boundary}--".encode() + crlf
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("text", "").strip()


def claude_to_ipa(word: str, transcript: str) -> str:
    user_msg = (
        f"Target word: {word}\n"
        f"Spoken transcription: {transcript}\n\n"
        f"Produce IPA matching the speaker's pronunciation:"
    )
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 200,
        "system": IPA_SYSTEM,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["content"][0]["text"].strip()


def paired_word(word: str) -> str | None:
    """Return the apostrophe-paired form: 'Malenia' <-> 'Malenia’s'.

    Handles straight (') and curly (’) apostrophes. Returns None if no
    sensible pair (e.g. plural-possessive ending in s')."""
    for apo in ("'", "’"):
        if word.endswith(apo + "s"):
            return word[: -len(apo + "s")]
        if word.endswith("s" + apo):
            return None
    return word + "'s"


VOICELESS_FINALS = {"p", "t", "k", "f", "θ"}  # p t k f θ
SIBILANT_FINALS = {"s", "z", "ʃ", "ʒ", "ʧ", "ʤ"}  # s z ʃ ʒ tʃ dʒ


def derive_possessive_ipa(ipa: str) -> str:
    """Add an English possessive ending to an IPA string.

    /ɪz/ after sibilants, /s/ after voiceless, /z/ otherwise."""
    if not ipa:
        return ipa
    last = ipa[-1]
    if last in SIBILANT_FINALS:
        return ipa + "ɨz"  # ɨz, close to ɪz; ɪ is ɪ
    if last in VOICELESS_FINALS:
        return ipa + "s"
    return ipa + "z"


def strip_possessive_ipa(ipa: str) -> str:
    if not ipa:
        return ipa
    # Try to remove possessive endings in order of specificity.
    for suffix in ("ɪz", "ɨz", "əz", "z", "s"):
        if ipa.endswith(suffix):
            return ipa[: -len(suffix)]
    return ipa


def derive_paired_ipa(approved_ipa: str, adding_possessive: bool) -> str:
    return (
        derive_possessive_ipa(approved_ipa)
        if adding_possessive
        else strip_possessive_ipa(approved_ipa)
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_err(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_bytes(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif self.path == "/app.js":
            self._serve_static("app.js")
        elif self.path == "/style.css":
            self._serve_static("style.css")
        elif self.path == "/api/queue":
            self._handle_queue()
        else:
            self._send_err(404, f"Not found: {self.path}")

    def do_POST(self):
        if self.path == "/api/speak":
            self._handle_speak()
        elif self.path == "/api/transcribe":
            self._handle_transcribe()
        elif self.path == "/api/approve":
            self._handle_approve()
        elif self.path == "/api/unverify":
            self._handle_unverify()
        else:
            self._send_err(404, f"Not found: {self.path}")

    def _serve_static(self, name: str) -> None:
        path = WEB_ROOT / name
        if not path.exists():
            self._send_err(404, f"Static file missing: {name}")
            return
        data = path.read_bytes()
        if name.endswith(".js"):
            ctype = "application/javascript"
        elif name.endswith(".css"):
            ctype = "text/css"
        elif name.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        else:
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send_bytes(200, ctype, data)

    def _handle_queue(self) -> None:
        words = load_json(WORDS_PATH, {})
        overrides = load_json(OVERRIDES_PATH, {})
        verified = load_verified()
        queue = []
        verified_in_source = 0
        for w, entry in words.items():
            if w in verified:
                verified_in_source += 1
                continue
            # Prefer existing override IPA over source IPA so a previously
            # derived paired override is what the user actually hears.
            ipa = overrides.get(w) or (
                entry.get("ipa", "") if isinstance(entry, dict) else entry
            )
            queue.append({"word": w, "ipa": ipa})
        self._send_json(
            200,
            {
                "queue": queue,
                "verified": verified_in_source,
                "total_source": len(words),
                "remaining": len(queue),
            },
        )

    def _handle_speak(self) -> None:
        data = self._read_json()
        word = (data.get("word") or "").strip()
        ipa = (data.get("ipa") or "").strip()
        if not word or not ipa:
            self._send_err(400, "word and ipa required")
            return
        try:
            audio = synthesize(word, ipa)
        except ClientError as e:
            self._send_err(502, f"Polly error: {e}")
            return
        except Exception as e:
            self._send_err(500, f"Synth failed: {e}")
            return
        self._send_bytes(200, "audio/mpeg", audio)

    def _handle_transcribe(self) -> None:
        if not OPENAI_API_KEY:
            self._send_err(500, "OPENAI_API_KEY not set")
            return
        if not ANTHROPIC_API_KEY:
            self._send_err(500, "ANTHROPIC_API_KEY not set")
            return
        data = self._read_json()
        audio_b64 = data.get("audio_base64")
        word = (data.get("word") or "").strip()
        content_type = data.get("content_type") or "audio/webm"
        if not audio_b64 or not word:
            self._send_err(400, "audio_base64 and word required")
            return
        try:
            audio = base64.b64decode(audio_b64)
            transcript = whisper_transcribe(audio, content_type, hint=word)
            ipa = claude_to_ipa(word, transcript)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self._send_err(502, f"API {e.code}: {body[:300]}")
            return
        except Exception as e:
            self._send_err(502, f"Transcribe failed: {e}")
            return
        self._send_json(200, {"transcript": transcript, "ipa": ipa})

    def _handle_approve(self) -> None:
        data = self._read_json()
        word = (data.get("word") or "").strip()
        ipa = (data.get("ipa") or "").strip()
        if not word or not ipa:
            self._send_err(400, "word and ipa required")
            return

        words = load_json(WORDS_PATH, {})
        overrides = load_json(OVERRIDES_PATH, {})
        verified = load_verified()

        original = source_ipa(words, word)
        changed = ipa != original

        # Only write the override if the user changed the IPA. If they reverted
        # an earlier edit back to the source IPA, drop any stale override.
        if changed:
            overrides[word] = ipa
        elif word in overrides:
            del overrides[word]

        verified.add(word)

        # Pair derivation only fires when the user actually changed the IPA
        # (no edit = nothing to propagate) AND the paired form isn't already
        # verified (prevents Adula <-> Adula's ping-pong).
        pair_info = None
        if changed:
            pair = paired_word(word)
            if pair and pair in words and pair not in verified:
                adding = not (word.endswith("'s") or word.endswith("’s"))
                pair_ipa = derive_paired_ipa(ipa, adding_possessive=adding)
                pair_original = source_ipa(words, pair)
                if pair_ipa != pair_original:
                    overrides[pair] = pair_ipa
                elif pair in overrides:
                    del overrides[pair]
                pair_info = {"word": pair, "ipa": pair_ipa}

        save_overrides(overrides)
        save_verified(verified)
        verified_in_source = sum(1 for w in words if w in verified)
        self._send_json(
            200,
            {
                "saved": True,
                "changed": changed,
                "paired": pair_info,
                "verified": verified_in_source,
                "total_source": len(words),
            },
        )

    def _handle_unverify(self) -> None:
        data = self._read_json()
        word = (data.get("word") or "").strip()
        if not word:
            self._send_err(400, "word required")
            return
        verified = load_verified()
        if word in verified:
            verified.remove(word)
            save_verified(verified)
        words = load_json(WORDS_PATH, {})
        verified_in_source = sum(1 for w in words if w in verified)
        self._send_json(
            200,
            {"verified": verified_in_source, "total_source": len(words)},
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not WEB_ROOT.exists():
        print(f"ERROR: {WEB_ROOT} does not exist", file=sys.stderr)
        sys.exit(1)
    if not WORDS_PATH.exists():
        print(
            f"ERROR: {WORDS_PATH} does not exist. Run filter_hard_words.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not OPENAI_API_KEY:
        print("WARN: OPENAI_API_KEY not set; Edit (mic transcription) will fail.")
    if not ANTHROPIC_API_KEY:
        print("WARN: ANTHROPIC_API_KEY not set; Edit (IPA generation) will fail.")

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"Serving at http://localhost:{args.port}")
        print("Open that URL in Chrome/Edge (mic permission required).")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down")


if __name__ == "__main__":
    main()
