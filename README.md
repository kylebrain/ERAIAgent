# Elden Ring AI Guide

An AI agent that answers questions about Elden Ring enemies, items, boss locations,
and spatial relationships — grounded in the Fextralife wiki and actual game data.

**Estimated AWS cost: ~$1–2/month** for personal use (~50 queries/day).

---

## Architecture

```
Fextralife wiki  ─► Lambda (weekly scraper)   ─►┐
Fan API          ─► Lambda (monthly fetcher)  ─►│  S3 Bucket (KB docs)
Game map (MSBE)  ─► Smithbox+WitchyBND+script ─►│
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
6. Select all files (`Ctrl+A`), right-click → **Extract Selected** → save to a local folder, e.g. `C:\er-msb\`
7. Result: `C:\er-msb\*.msb.dcx` — one file per map tile

**Step 0b — Serialize MSB files to XML with WitchyBND**
1. Download **WitchyBND** from https://github.com/ividyon/WitchyBND/releases (grab `WitchyBND.zip`)
2. Extract `witchybnd.exe` to a convenient folder
3. Run:
   ```bash
   ./witchybnd.exe --recursive "C:/er-msb/"
   ```
4. Result: `C:\er-msb\<tile>.msb.dcx-witchy\*.xml` — one XML per map tile with all entity positions

**Step 0c — Build spatial proximity documents**

Requires the lookup files under `./lookups/` (committed to the repo):
- `enemy_lookup.xml`, `item_lookup.xml`, `map_lookup.xml` — id → friendly name mappings
- `map_overview.html` — sourced from the Souls Modding Wiki, used to enumerate named Sites of Grace per map tile

```bash
python scripts/extract_msb_coordinates.py \
  --msb-dir "C:/er-msb" \
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
# Upload spatial + Kaggle data
python scripts/upload_to_s3.py --bucket <KbDocsBucketName>

# Seed wiki docs by invoking the scraper once
aws lambda invoke \
  --function-name elden-ring-scraper \
  --payload '{}' \
  --log-type Tail \
  response.json
```

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

Update the CloudFormation stack with the real KB ID:
```bash
aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name elden-ring-agent \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides KnowledgeBaseId=<your-kb-id>
```

---

### Phase 5 — Deploy the Web UI

1. Edit `web/index.html` — replace `REPLACE_WITH_API_GATEWAY_URL` with the
   `ApiEndpoint` value from the CloudFormation outputs.

2. Upload (use the `WebBucketName` from the CloudFormation outputs, or
   `$WEB_BUCKET` from your `.env`):
   ```bash
   aws s3 cp web/index.html s3://"$WEB_BUCKET"/index.html --content-type text/html
   ```

3. Open the `WebsiteUrl` from the CloudFormation outputs in your browser.

---

## Keeping Data Fresh

- **Wiki:** Lambda runs automatically every Sunday at 02:00 UTC.
  Manual trigger: `aws lambda invoke --function-name elden-ring-scraper --payload '{}' out.json`

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
