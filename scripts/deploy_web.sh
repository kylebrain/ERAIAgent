#!/usr/bin/env bash
# Upload web/ to the S3 origin behind CloudFront and invalidate the cache.
#
# Usage:
#   scripts/deploy_web.sh
#
# Reads WEB_BUCKET and (optionally) CLOUDFRONT_DISTRIBUTION_ID from .env.
#
# Why this exists instead of a plain `aws s3 sync`:
# The AWS CLI on Windows looks up MIME types from the registry (HKCR\.js etc.),
# which is often missing or wrong, so `.js` files get uploaded as text/plain.
# Strict module loaders then refuse to execute them ("Expected a JavaScript-or-
# Wasm module script…"). This script forces the correct Content-Type per
# extension regardless of host OS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${WEB_BUCKET:?WEB_BUCKET must be set (in .env or environment)}"
WEB_DIR="$SCRIPT_DIR/../web"

echo "==> sync web/ to s3://$WEB_BUCKET (with --delete to prune)"
aws s3 sync "$WEB_DIR/" "s3://$WEB_BUCKET/" --delete

# `aws s3 cp` always uploads (unlike `sync`), so the corrected Content-Type
# lands even when the file body itself hasn't changed.
fix_type() {
  local pattern="$1" ctype="$2"
  aws s3 cp "$WEB_DIR/" "s3://$WEB_BUCKET/" --recursive \
    --exclude "*" --include "$pattern" \
    --content-type "$ctype" \
    --metadata-directive REPLACE
}

echo "==> fix Content-Type for module-loaded assets"
fix_type "*.js"   application/javascript
fix_type "*.mjs"  application/javascript
fix_type "*.css"  text/css
fix_type "*.html" text/html
fix_type "*.json" application/json
fix_type "*.svg"  image/svg+xml

if [[ -n "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  echo "==> invalidate CloudFront $CLOUDFRONT_DISTRIBUTION_ID /*"
  aws cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
    --paths "/*" \
    --query "Invalidation.{Id:Id,Status:Status}" \
    --output table
else
  echo "==> CLOUDFRONT_DISTRIBUTION_ID not set; skipping cache invalidation"
  echo "    (set it in .env to invalidate automatically)"
fi

echo "==> done"
