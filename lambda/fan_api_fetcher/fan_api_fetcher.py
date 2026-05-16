"""
Fetches all Elden Ring entity data from the Fan API (eldenring.fanapis.com)
and writes structured plain-text documents to S3 for KB ingestion.
Runs monthly via EventBridge — game data changes infrequently.
"""
import json
import os
import time
import urllib.request

import boto3

BASE_URL = "https://eldenring.fanapis.com/api"
DOCS_BUCKET = os.environ["DOCS_BUCKET"]
S3_PREFIX = "structured/fan-api/"

ENDPOINTS = [
    "bosses",
    "creatures",
    "weapons",
    "armors",
    "shields",
    "items",
    "talismans",
    "incantations",
    "sorceries",
    "spirits",
    "npcs",
    "locations",
    "classes",
    "ammos",
    "ashes",
]

s3 = boto3.client("s3")


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "EldenRingAIAgent/1.0 (personal research)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _fetch_all(endpoint: str) -> list[dict]:
    entities = []
    page = 0
    limit = 100
    while True:
        url = f"{BASE_URL}/{endpoint}?limit={limit}&page={page}"
        try:
            data = _fetch_json(url)
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            break
        items = data.get("data", [])
        if not items:
            break
        entities.extend(items)
        total = data.get("total", 0)
        if len(entities) >= total or len(items) < limit:
            break
        page += 1
        time.sleep(0.3)
    return entities


def _format_entity(entity: dict, entity_type: str) -> str:
    lines = []
    name = entity.get("name", "Unknown")
    lines.append(f"[{entity_type.upper()}] {name}")

    for field in ("description", "quote", "effect", "passiveEffect"):
        val = entity.get(field)
        if val and isinstance(val, str) and val.strip():
            lines.append(f"\n{val.strip()}")
            break

    for field in ("location", "region"):
        val = entity.get(field)
        if val and isinstance(val, str) and val.strip():
            lines.append(f"\nLocation: {val.strip()}")
            break

    drops = entity.get("drops", [])
    if drops and isinstance(drops, list):
        drop_names = [d if isinstance(d, str) else d.get("name", str(d)) for d in drops[:10]]
        lines.append(f"\nDrops: {', '.join(drop_names)}")

    skip = {"id", "name", "description", "quote", "effect", "passiveEffect",
            "location", "region", "drops", "image", "icon", "url"}
    stat_lines = []
    for key, val in entity.items():
        if key in skip:
            continue
        if isinstance(val, (str, int, float)) and val not in ("", None):
            stat_lines.append(f"{key}: {val}")
        elif isinstance(val, list) and val:
            flat = [v if isinstance(v, str) else json.dumps(v) for v in val[:5]]
            stat_lines.append(f"{key}: {', '.join(flat)}")
        elif isinstance(val, dict):
            for sub_key, sub_val in val.items():
                if sub_val not in ("", None, []):
                    stat_lines.append(f"{key}.{sub_key}: {sub_val}")

    if stat_lines:
        lines.append("\nDetails:")
        lines.extend(f"  {s}" for s in stat_lines)

    return "\n".join(lines)


def handler(event, context):
    total_written = 0
    for endpoint in ENDPOINTS:
        print(f"Fetching /{endpoint}...")
        entities = _fetch_all(endpoint)
        if not entities:
            print(f"  (no data)")
            continue

        for entity in entities:
            entity_id = entity.get("id", entity.get("name", f"unknown_{total_written}"))
            safe_id = str(entity_id).replace("/", "_").replace(" ", "_")[:80]
            key = f"{S3_PREFIX}{endpoint}/{safe_id}.txt"
            text = _format_entity(entity, endpoint.rstrip("s"))
            s3.put_object(
                Bucket=DOCS_BUCKET,
                Key=key,
                Body=text.encode("utf-8"),
                ContentType="text/plain",
            )
            total_written += 1

        print(f"  {endpoint}: {len(entities)} entities written to S3")
        time.sleep(0.5)

    print(f"Done. Total: {total_written} documents")
    return {"total_written": total_written}
