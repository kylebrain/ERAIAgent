"""
Speech handler:
  POST /speak       { text, voice? }                  -> { audio_base64, content_type }
  POST /transcribe  { audio_base64, content_type? }   -> { transcript }

/speak     : Polly TTS using LEXICON_NAMES (env, comma-separated).
/transcribe: AssemblyAI STT. The API key lives in SSM Parameter Store at
             ASSEMBLYAI_API_KEY_PARAM (env). The Whisper call is biased with
             up to 1000 Elden Ring proper nouns via keyterms_prompt (built
             from the lexicon by scripts/build_stt_prompt.py), then the
             returned transcript runs through ipa_corrector for phonetic
             post-correction against the full ~480-term lexicon.
"""
import os
import json
import base64
import time
import urllib.request
import urllib.error
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

import ipa_corrector

REGION = os.environ.get("AWS_REGION", "us-east-1")
LEXICON_NAMES = [n.strip() for n in os.environ.get("LEXICON_NAMES", "").split(",") if n.strip()]
ASSEMBLYAI_API_KEY_PARAM = os.environ.get("ASSEMBLYAI_API_KEY_PARAM", "")
ALLOWED_VOICES = {"Stephen", "Joanna", "Matthew", "Amy", "Brian", "Ruth"}
DEFAULT_VOICE = "Stephen"
MAX_TEXT_LEN = 3000
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # 8 MB — API GW payload cap kicks in first

ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
ASSEMBLYAI_SPEECH_MODELS = ["universal-3-pro", "universal-2"]
ASSEMBLYAI_POLL_INTERVAL_SEC = 0.5
ASSEMBLYAI_MAX_WAIT_SEC = 20

polly = boto3.client("polly", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)

# Loaded lazily on first /transcribe call, then cached for the warm container.
_assemblyai_key: str | None = None
_keyterms: list[str] | None = None

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
        # API call rather than billing AssemblyAI for silence.
        return _ok({"transcript": ""})

    try:
        api_key = _get_assemblyai_key()
    except Exception as e:
        print(f"Failed to load AssemblyAI API key: {e}")
        return _err(500, "STT not configured (missing AssemblyAI API key in SSM).")

    print(f"Transcribing {len(audio_bytes)} bytes of audio with {len(_get_keyterms())} keyterms")
    try:
        transcript = _assemblyai(audio_bytes, api_key, _get_keyterms())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        print(f"AssemblyAI HTTP {e.code}: {body_text}")
        return _err(502, f"AssemblyAI error ({e.code}).")
    except Exception as e:
        print(f"AssemblyAI call failed: {e}")
        return _err(502, "AssemblyAI call failed.")

    print(f"Recieved response from AssemblyAI: {repr(transcript)}")
    transcript = ipa_corrector.correct(transcript)
    print(f"IPA-corrected transcript: {repr(transcript)}")
    return _ok({"transcript": transcript})


def _get_assemblyai_key() -> str:
    global _assemblyai_key
    if _assemblyai_key:
        return _assemblyai_key
    if not ASSEMBLYAI_API_KEY_PARAM:
        raise RuntimeError("ASSEMBLYAI_API_KEY_PARAM env var not set")
    resp = ssm.get_parameter(Name=ASSEMBLYAI_API_KEY_PARAM, WithDecryption=True)
    _assemblyai_key = resp["Parameter"]["Value"]
    return _assemblyai_key


def _get_keyterms() -> list[str]:
    global _keyterms
    if _keyterms is not None:
        return _keyterms
    path = Path(__file__).parent / "stt_keyterms.json"
    _keyterms = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return _keyterms


def _assemblyai(audio_bytes: bytes, api_key: str, keyterms: list[str]) -> str:
    # 1. Upload raw audio bytes to AssemblyAI's temporary storage.
    upload_req = urllib.request.Request(
        ASSEMBLYAI_UPLOAD_URL,
        data=audio_bytes,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(upload_req, timeout=20) as r:
        upload_resp = json.loads(r.read().decode("utf-8"))
    upload_url = upload_resp.get("upload_url")
    if not upload_url:
        raise RuntimeError(f"AssemblyAI upload returned no upload_url: {upload_resp}")

    # 2. Create transcript job with keyterm biasing.
    payload: dict = {
        "audio_url": upload_url,
        "language_code": "en",
        "speech_models": ASSEMBLYAI_SPEECH_MODELS,
    }
    if keyterms:
        payload["keyterms_prompt"] = keyterms
    create_req = urllib.request.Request(
        ASSEMBLYAI_TRANSCRIPT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(create_req, timeout=20) as r:
        create_resp = json.loads(r.read().decode("utf-8"))
    transcript_id = create_resp.get("id")
    if not transcript_id:
        raise RuntimeError(f"AssemblyAI create returned no id: {create_resp}")

    # 3. Poll for completion.
    poll_url = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"
    deadline = time.monotonic() + ASSEMBLYAI_MAX_WAIT_SEC
    while time.monotonic() < deadline:
        poll_req = urllib.request.Request(
            poll_url,
            headers={"Authorization": api_key},
            method="GET",
        )
        with urllib.request.urlopen(poll_req, timeout=10) as r:
            poll_resp = json.loads(r.read().decode("utf-8"))
        status = poll_resp.get("status")
        if status == "completed":
            return (poll_resp.get("text") or "").strip()
        if status == "error":
            raise RuntimeError(f"AssemblyAI transcript error: {poll_resp.get('error')}")
        time.sleep(ASSEMBLYAI_POLL_INTERVAL_SEC)

    raise TimeoutError(
        f"AssemblyAI transcript {transcript_id} did not complete within {ASSEMBLYAI_MAX_WAIT_SEC}s"
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(body: dict) -> dict:
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(body)}


def _err(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": CORS_HEADERS,
            "body": json.dumps({"error": message})}
