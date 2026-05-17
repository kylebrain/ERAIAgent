# Elden Ring AI Guide

An AI agent that answers questions about Elden Ring enemies, items, boss locations,
and spatial relationships — grounded in the Fextralife wiki and actual game data.

**Estimated AWS cost: ~$1–2/month** for personal use (~50 queries/day).

---

## Architecture

```
Fextralife wiki  ─► local scraper script     ─►┐
Fan API          ─► Lambda (weekly fetcher)   ─►│  S3 Bucket (KB docs)
Game map (MSB)   ─► Smithbox+WitchyBND+script ─►│
Kaggle dataset   ─► manual upload             ─►│
                                               │
                                               ▼
                                    Bedrock Knowledge Base
                                    (Titan Embeddings v2 + S3 Vectors)
                                               │
                                               ▼
User ──► CloudFront ──► S3 (web/index.html)
         API Gateway ──► Lambda (query handler) ──► Bedrock (Claude Haiku)
```

---

## Setup

### Prerequisites

- AWS CLI configured (`aws configure`)
- AWS SAM CLI installed: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
- Python 3.12+
- Elden Ring installed on PC (for map entity coordinate extraction)
- An S3 bucket for CloudFormation artifacts (create once):
  ```bash
  aws s3 mb s3://cfn-artifacts-<your-account-id>
  ```
- Copy `.env.example` to `.env` and fill in your AWS account ID, region, and
  artifacts bucket name. `.env` is gitignored. `scripts/deploy.sh` sources it
  automatically.

**Python environment** — set up a venv at the repo root for the local scripts
(scraper, MSB extraction, S3 upload, lookup generation):

```bash
python -m venv .venv
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# Windows (git-bash):   source .venv/Scripts/activate
# macOS / Linux:        source .venv/bin/activate
pip install -r requirements.txt
```

`.venv/` is gitignored. Re-activate it in any new shell before running scripts
under `scripts/`. Lambda functions under `lambda/` have their own
`requirements.txt` files used by `sam build` — they're separate from this venv.

---

### Phase 0 — Extract Game Data Locally (one-time)

Generates spatial proximity documents from actual game map entity files.
No need to unpack the game archives — Smithbox reads them directly.

**Step 0a — Extract MSB map files with Smithbox**
> Smithbox reads the packed game archives (Data0–3.bdt) natively — no UXM required.

1. Download **Smithbox** from https://github.com/vawser/Smithbox/releases (grab the latest `.zip`)
2. Extract and run `Smithbox.exe`
3. Create a new project: **File → New Project → Elden Ring** → point at `C:\Program Files (x86)\Steam\steamapps\common\ELDEN RING\Game`
4. Open the **File Browser** panel (View → File Browser)
5. Navigate to `map/mapstudio/` in the file tree
6. Select all files (`Ctrl+A`), right-click → **Extract Selected** → save to a local folder. Set `MSB_DIR` in your `.env` to this path.
7. Result: `$MSB_DIR\*.msb.dcx` — one file per map tile

**Step 0b — Serialize MSB files to XML with WitchyBND**
1. Download **WitchyBND** from https://github.com/ividyon/WitchyBND/releases (grab `WitchyBND.zip`) and extract it.
2. Set both paths in your `.env` (use forward slashes):
   - `WITCHYBND_EXE` — full path to `WitchyBND.exe`
   - `MSB_DIR` — full path to the `mapstudio/` folder containing the `.msb.dcx` files from Step 0a
3. From the repo root, generate the batched WitchyBND commands and run them:
   ```bash
   ./scripts/generate_witchy_commands.sh   # writes ./scripts/commands.sh
   ./scripts/commands.sh
   ```
   The generator reads `.env`, finds every `.msb.dcx` in `$MSB_DIR`, and writes
   one `WitchyBND.exe` call per line. The batch size is auto-computed from the
   prefix length, the longest filename, and `CMDLINE_LIMIT` (default 8000, the
   cmd.exe limit) so each line stays under the Windows command-line limit.
   Override with `CMDLINE_LIMIT=32000 ./scripts/generate_witchy_commands.sh`.

   To minimize per-line length, the generated `scripts/commands.sh` `cd`s into
   `$MSB_DIR` and refers to files by basename; `WitchyBND.exe` is referenced
   via a path relative to `$MSB_DIR` when that's shorter than the absolute
   path. `scripts/commands.sh` is gitignored.
4. Result: `$MSB_DIR/<tile>.msb.dcx-witchy/*.xml` — one XML per map tile with all entity positions.

**Step 0c — Build spatial proximity documents**

Requires the lookup files under `./lookups/` (committed to the repo):
- `enemy_lookup.xml`, `item_lookup.xml`, `map_lookup.xml` — id → friendly name mappings
- `map_overview.html` — sourced from the Souls Modding Wiki, used to enumerate named Sites of Grace per map tile

```bash
python scripts/extract_msb_coordinates.py \
  --msb-dir "$MSB_DIR" \
  --output-dir ./spatial_docs \
  --radius 30
```

Optional flags:
- `--workers N` — parallel worker count (default: CPU count). Phase 1 (entity gathering from XML) and Phase 2 (proximity computation) both run on a `ThreadPoolExecutor`.
- `--test [N]` — limit to the first N tile subfolders for a quick run (default 5 when bare; pass `--test exclude` to force a full run).

Outputs under `./spatial_docs/`:
- `proximity/` — one doc per tracked entity listing what's within `--radius` meters, with cardinal directions
- `region_summaries/` — one doc per region listing entity counts/uniques per type, plus the named Sites of Grace from `map_overview.html`
- `clusters/` — one doc per grid cell (≥3 entities) grouped by cardinal area within its region
- `entity_coordinates.json` — flat coordinate index (reference only, not for KB ingestion)

Caching:
- Per-tile parsed entities are written to `./.cache/{tile_id}.json` so repeat runs skip XML parsing.
- Delete `./.cache/` if the lookup tables change — the cache holds already-resolved entity names.

**Step 0d — Download Kaggle dataset (optional enrichment)**
1. Download from https://kaggle.com/datasets/robikscube/elden-ring-ultimate-dataset
2. Save CSV files to `./kaggle/`

---

### Phase 1 — Deploy AWS Infrastructure

**Step 1a — Package and deploy the CloudFormation stack**

`sam build` installs each function's `requirements.txt` into `.aws-sam/build/` —
package and deploy from that built template (not the source template), otherwise
Lambda dependencies like `beautifulsoup4` won't be bundled.

```bash
# Easiest: use the wrapper (reads .env for ARTIFACTS_BUCKET / STACK_NAME / KB_ID)
scripts/deploy.sh

# Or run the steps manually:
sam build --template-file infra/template.yaml

aws cloudformation package \
  --template-file .aws-sam/build/template.yaml \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --output-template-file infra/packaged.yaml

aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides KnowledgeBaseId=PLACEHOLDER
```

> `infra/packaged.yaml` is a build artifact (gitignored). It is regenerated by
> `sam build` + `aws cloudformation package`.

**Step 1b — Note the stack outputs**
```bash
aws cloudformation describe-stacks --stack-name elden-ring-agent \
  --query "Stacks[0].Outputs"
```
You need: `KbDocsBucketName`, `WebBucketName`, `ApiEndpoint`, `WebsiteUrl`

---

### Phase 2 — Upload Documents to S3

```bash
# Upload spatial + Kaggle data ($KB_DOCS_BUCKET comes from .env)
python scripts/upload_to_s3.py --bucket "$KB_DOCS_BUCKET"

# Seed wiki docs by running the scraper locally (~1hr for full sitemap @ 1s/page)
python scripts/run_scraper_local.py --bucket "$KB_DOCS_BUCKET"
```

The scraper runs locally rather than as a Lambda because the full sitemap walk
exceeds the 15-minute Lambda timeout. Use `--preserve` on later runs to skip
URLs already in S3, or `--start-offset N` to resume an interrupted run.

---

### Phase 3 — Create the Bedrock Knowledge Base (console, ~5 min)

1. Open **AWS Console → Bedrock → Knowledge Bases → Create**
2. Name: `elden-ring-kb`
3. IAM role: let AWS create one
4. Data source: Amazon S3 → select `<KbDocsBucketName>`
5. Embedding model: **Amazon Titan Embeddings v2**
6. Vector database: **Amazon S3 Vectors** (cheapest option)
7. Chunking: Fixed-size, 512 tokens, 20% overlap
8. Create → click **Sync** → wait for ingestion to complete

Note the **Knowledge Base ID** (e.g. `ABCDE12345`).

---

### Phase 4 — Wire up the Knowledge Base ID

Set `KB_ID` in your `.env`, then redeploy via the wrapper:
```bash
scripts/deploy.sh "$KB_ID"
```

Or run the raw AWS CLI:
```bash
aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name elden-ring-agent \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides KnowledgeBaseId=<your-kb-id>
```

---

### Phase 5 — Deploy the Web UI

1. Edit `web/index.html` — set the `content` attribute of the
   `<meta name="api-endpoint">` tag to the `ApiEndpoint` value from the
   CloudFormation outputs.

2. Upload the whole `web/` directory so HTML, CSS, and JS modules all land with
   correct content types (the AWS CLI infers MIME types from extension):
   ```bash
   aws s3 sync web/ s3://"$WEB_BUCKET"/ --delete
   ```

3. Open the `WebsiteUrl` from the CloudFormation outputs in your browser.

---

### Phase 5a — Voice features

The UI ships with two voice features:

- **Speech-to-text** via **AssemblyAI** through the `/transcribe` endpoint.
  Click the mic button (or use the configurable global keybind under gear →
  "Mic keybind") to enter conversation mode. The browser records via
  `MediaRecorder` and detects utterance boundaries with a Web Audio RMS-based
  VAD (~1000ms of silence cuts the chunk; toggling the mic off also flushes).
  Each utterance is POSTed to `/transcribe`, which calls AssemblyAI biased
  with up to 1000 Elden Ring proper nouns via `keyterms_prompt` (see Phase
  5b), then runs the transcript through a Double-Metaphone post-correction
  pass against the full lexicon — so "Radahn", "Caelid", "Mohgwyn" transcribe
  correctly instead of as English near-matches. Works in any browser with
  `getUserMedia` + `MediaRecorder` (Chromium/Firefox/Safari) over HTTPS.

- **Text-to-speech** via the `/speak` endpoint, which calls **Amazon Polly**
  (Neural engine; default voice **Stephen**, others selectable in the gear
  menu). Auto-plays on each answer when "Voice on" is toggled; the speaker
  icon on each bubble always works for manual replay.

Polly is a separate service from Bedrock, so Polly works even when Bedrock
inference quotas are constrained. Polly does not need explicit model-access
opt-in like Bedrock does.

There is also a **Test mode** toggle in the header. When enabled, the UI
bypasses the `/ask` endpoint entirely and returns a random Elden Ring quote
from a local bank — handy for exercising the full voice pipeline (STT → render
→ TTS) without depending on the Bedrock RAG path.

### Phase 5b — Pronunciation lexicon (one-time, ~$1)

Polly mispronounces fictional proper nouns by default. A one-time pipeline
extracts every title from the wiki S3 prefix, uses Claude (direct Anthropic
API — separate from Bedrock) to generate IPA pronunciations, compiles a PLS
lexicon, and uploads it to Polly. The Lambda passes the lexicon name(s) on
every synthesis call.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/extract_wiki_terms.py --bucket "$KB_DOCS_BUCKET"
python scripts/generate_pronunciations.py
# (optional) edit infra/lexicons/overrides.json to fix anything that sounds wrong
python scripts/build_lexicon.py
python scripts/upload_lexicon.py
python scripts/build_stt_prompt.py   # rebuild AssemblyAI keyterms + corrector lexicon
```

`build_lexicon.py` prints a `LEXICON_NAMES` value (e.g. `eldenring` or
`eldenring,eldenringb,eldenringc` if sharded) that you copy into the
`SpeechHandlerFunction.Environment.Variables.LEXICON_NAMES` field in
`infra/template.yaml`, then redeploy. (Polly lexicon names must match
`[0-9A-Za-z]{1,20}`, so no underscores or dashes.)

`build_stt_prompt.py` writes two files into `lambda/speech_handler/`:
`stt_keyterms.json` (up to 1000 terms passed as AssemblyAI's `keyterms_prompt`
to bias the model at recognition time) and `lexicon_data.json` (the full
term→IPA map used by `ipa_corrector.py` as a phonetic post-correction safety
net). Re-run it whenever the lexicon or overrides change, then redeploy so
the updated files ship with the Lambda.

**Fixing a mispronunciation.** If Polly says "mawg" instead of "moag" for Mohg,
add `{"Mohg": "moʊɡ"}` to `infra/lexicons/overrides.json`, re-run
`build_lexicon.py` and `upload_lexicon.py`. Overrides always win over the
LLM-generated map.

The Anthropic API key is billed by Anthropic directly and is **not** affected
by Bedrock quotas.

### Phase 5c — AssemblyAI API key for STT (one-time)

The `/transcribe` endpoint calls AssemblyAI, so the speech Lambda needs an
AssemblyAI API key. Store it as an encrypted SSM Parameter (the Lambda reads
it on cold start with `ssm:GetParameter`):

```bash
aws ssm put-parameter \
  --name "/elden-ring/assemblyai-api-key" \
  --type SecureString \
  --value "..." \
  --overwrite
```

The parameter name is hard-coded in `infra/template.yaml`
(`SpeechHandlerFunction.Environment.Variables.ASSEMBLYAI_API_KEY_PARAM` and
the matching IAM resource ARN). If you ever change the name, update both.

AssemblyAI Universal pricing is roughly **$0.0043 / minute** of audio (with
the `keyterms_prompt` surcharge included), billed by AssemblyAI directly —
independent of AWS spend. At typical conversational pace (~150 utterances/hr
× 3 s each = 7.5 min/hr) that's about $0.03/hr of mic time.

---

## Keeping Data Fresh

- **Wiki scraper:** Run locally when you want fresh wiki content:
  `python scripts/run_scraper_local.py --bucket "$KB_DOCS_BUCKET" --preserve`
  (`--preserve` skips pages already in S3, so incremental refreshes are fast.)

- **Fan API fetcher:** Runs automatically every Sunday at 02:00 UTC (`cron(0 2 ? * SUN *)`).
  Manual trigger: `aws lambda invoke --function-name elden-ring-fan-api-fetcher --payload '{}' out.json`

- **Game data:** Re-run Phase 0 after game patches, then re-upload and re-sync the KB.

---

## Cost Breakdown (personal use, ~50 queries/day)

| Service | ~$/month |
|---|---|
| S3 Vectors (vector store) | $0.05–0.50 |
| Bedrock Claude Haiku 4.5 (inference) | $0.50–1.00 |
| Bedrock Titan Embeddings (one-time ingestion) | $0.05 one-time |
| Lambda, API Gateway, S3, CloudFront | ~$0 (free tier) |
| **Total** | **~$1–2/month** |
