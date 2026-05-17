"""
Speech handler:
  POST /speak       { text, voice? }                  -> { audio_base64, content_type }
  POST /transcribe  { audio_base64, content_type? }   -> { transcript }

/speak    : Polly TTS using LEXICON_NAMES (env, comma-separated).
/transcribe: OpenAI Whisper STT. The API key lives in SSM Parameter Store at
            OPENAI_API_KEY_PARAM (env). The Whisper call is biased with
            stt_prompt.txt (built from the lexicon by scripts/build_stt_prompt.py)
            so fantasy proper nouns like "Radahn", "Caelid", "Mohgwyn"
            transcribe correctly.
"""
import os
import json
import base64
import uuid
import urllib.request
import urllib.error
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
LEXICON_NAMES = [n.strip() for n in os.environ.get("LEXICON_NAMES", "").split(",") if n.strip()]
OPENAI_API_KEY_PARAM = os.environ.get("OPENAI_API_KEY_PARAM", "")
ALLOWED_VOICES = {"Stephen", "Joanna", "Matthew", "Amy", "Brian", "Ruth"}
DEFAULT_VOICE = "Stephen"
MAX_TEXT_LEN = 3000
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # 8 MB — Whisper limit is 25 MB but API GW caps payload smaller
WHISPER_MODEL = "whisper-1"
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

polly = boto3.client("polly", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)

# Loaded lazily on first /transcribe call, then cached for the warm container.
_openai_key: str | None = None
_stt_prompt: str | None = None

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    path = event.get("path") or event.get("resource") or ""
    if path.endswith("/transcribe"):
        return _handle_transcribe(event)
    return _handle_speak(event)


# ── /speak ───────────────────────────────────────────────────────────────────

def _handle_speak(event):
    try:
        body = json.loads(event.get("body") or "{}")
        text = (body.get("text") or "").strip()
        voice = body.get("voice") or DEFAULT_VOICE
    except (json.JSONDecodeError, AttributeError):
        return _err(400, "Request body must be JSON with a 'text' field.")

    if not text:
        return _err(400, "'text' field is required.")
    if len(text) > MAX_TEXT_LEN:
        return _err(400, f"Text must be {MAX_TEXT_LEN} characters or fewer.")
    if voice not in ALLOWED_VOICES:
        return _err(400, f"Unknown voice '{voice}'. Allowed: {sorted(ALLOWED_VOICES)}")

    audio_bytes = _synthesize(text, voice, LEXICON_NAMES)
    if audio_bytes is None:
        return _err(502, "Failed to synthesize speech.")

    return _ok({
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "content_type": "audio/mpeg",
    })


def _synthesize(text: str, voice: str, lexicon_names: list[str]) -> bytes | None:
    kwargs = dict(Engine="neural", VoiceId=voice, OutputFormat="mp3", Text=text)
    if lexicon_names:
        kwargs["LexiconNames"] = lexicon_names
    try:
        response = polly.synthesize_speech(**kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "LexiconNotFoundException" and lexicon_names:
            print(f"Lexicon(s) {lexicon_names} not found, retrying without them")
            kwargs.pop("LexiconNames", None)
            try:
                response = polly.synthesize_speech(**kwargs)
            except Exception as retry_err:
                print(f"Polly retry failed: {retry_err}")
                return None
        else:
            print(f"Polly error: {e}")
            return None

    stream = response.get("AudioStream")
    if stream is None:
        return None
    return stream.read()


# ── /transcribe ──────────────────────────────────────────────────────────────

def _handle_transcribe(event):
    try:
        body = json.loads(event.get("body") or "{}")
        audio_b64 = body.get("audio_base64") or ""
        content_type = (body.get("content_type") or "audio/webm").strip()
    except (json.JSONDecodeError, AttributeError):
        return _err(400, "Request body must be JSON with an 'audio_base64' field.")

    if not audio_b64:
        return _err(400, "'audio_base64' field is required.")

    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except (ValueError, base64.binascii.Error):
        return _err(400, "'audio_base64' is not valid base64.")

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return _err(413, f"Audio exceeds {MAX_AUDIO_BYTES} bytes.")
    if len(audio_bytes) < 200:
        # MediaRecorder produces tiny blobs when nothing was said — skip the
        # API call rather than billing Whisper for silence.
        return _ok({"transcript": ""})

    try:
        api_key = _get_openai_key()
    except Exception as e:
        print(f"Failed to load OpenAI API key: {e}")
        return _err(500, "STT not configured (missing OpenAI API key in SSM).")

    prompt = _get_stt_prompt()
    try:
        transcript = _whisper(audio_bytes, content_type, api_key, prompt)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        print(f"Whisper HTTP {e.code}: {body_text}")
        return _err(502, f"Whisper error ({e.code}).")
    except Exception as e:
        print(f"Whisper call failed: {e}")
        return _err(502, "Whisper call failed.")

    return _ok({"transcript": transcript})


def _get_openai_key() -> str:
    global _openai_key
    if _openai_key:
        return _openai_key
    if not OPENAI_API_KEY_PARAM:
        raise RuntimeError("OPENAI_API_KEY_PARAM env var not set")
    resp = ssm.get_parameter(Name=OPENAI_API_KEY_PARAM, WithDecryption=True)
    _openai_key = resp["Parameter"]["Value"]
    return _openai_key


def _get_stt_prompt() -> str:
    global _stt_prompt
    if _stt_prompt is not None:
        return _stt_prompt
    path = Path(__file__).parent / "stt_prompt.txt"
    _stt_prompt = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return _stt_prompt


def _whisper(audio_bytes: bytes, content_type: str, api_key: str, prompt: str) -> str:
    ext = _ext_for_content_type(content_type)
    boundary = f"----WhisperBoundary{uuid.uuid4().hex}"
    body = _multipart(boundary, audio_bytes, ext, content_type, prompt)
    req = urllib.request.Request(
        WHISPER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return (payload.get("text") or "").strip()


def _multipart(boundary: str, audio: bytes, ext: str, content_type: str, prompt: str) -> bytes:
    crlf = b"\r\n"
    parts: list[bytes] = []

    def add_field(name: str, value: str):
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    add_field("model", WHISPER_MODEL)
    add_field("response_format", "json")
    add_field("language", "en")
    if prompt:
        add_field("prompt", prompt)

    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="audio.{ext}"'.encode()
    )
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    parts.append(audio)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    return crlf.join(parts)


def _ext_for_content_type(content_type: str) -> str:
    ct = content_type.lower().split(";", 1)[0].strip()
    return {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }.get(ct, "webm")


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(body: dict) -> dict:
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(body)}


def _err(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": CORS_HEADERS,
            "body": json.dumps({"error": message})}
