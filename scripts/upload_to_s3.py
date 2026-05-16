"""
Upload local data files to the Elden Ring KB S3 bucket.

Usage:
    python scripts/upload_to_s3.py --bucket elden-ring-kb-docs-<account-id>

Uploads:
    ./spatial_docs/  -> s3://<bucket>/spatial/
    ./kaggle/        -> s3://<bucket>/kaggle/
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

UPLOAD_DIRS = [
    ("./spatial_docs", "spatial/"),
    ("./kaggle", "kaggle/"),
]

MAX_WORKERS = 16

# Per-file transfer config: multi-part for files >8MB, 4 parallel parts each.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def _upload_one(s3, bucket: str, local_file: Path, key: str):
    s3.upload_file(
        str(local_file), bucket, key,
        ExtraArgs={"ContentType": "text/plain"},
        Config=TRANSFER_CONFIG,
    )
    return key


def upload_dir(s3, bucket: str, local_dir: Path, prefix: str):
    if not local_dir.exists():
        print(f"  [skip] {local_dir} does not exist")
        return 0

    files = [f for f in local_dir.rglob("*") if f.is_file()]
    if not files:
        print(f"  [skip] {local_dir} is empty")
        return 0

    total = len(files)
    print(f"  {total} files, uploading with {MAX_WORKERS} workers...")

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                _upload_one, s3, bucket, f,
                prefix + str(f.relative_to(local_dir)).replace("\\", "/"),
            ): f
            for f in files
        }
        for fut in as_completed(futures):
            fut.result()  # surface exceptions
            done += 1
            if done % 50 == 0 or done == total:
                print(f"    {done}/{total}")
    return done


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True, help="S3 bucket name (KbDocsBucketName from CFN output)")
    parser.add_argument("--profile", help="AWS CLI profile name (optional)")
    args = parser.parse_args()

    # Bump the connection pool so concurrent uploads don't serialize on it.
    client_config = Config(max_pool_connections=MAX_WORKERS * 2)
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    s3 = session.client("s3", config=client_config)

    total = 0
    for local_path_str, prefix in UPLOAD_DIRS:
        local_path = Path(local_path_str)
        print(f"\nUploading {local_path}/ -> s3://{args.bucket}/{prefix}")
        total += upload_dir(s3, args.bucket, local_path, prefix)

    print(f"\nTotal files uploaded: {total}")
    print("\nNext step: trigger a Knowledge Base sync in the AWS Bedrock console")
    print("  or via CLI:")
    print(f"  aws bedrock-agent start-ingestion-job --knowledge-base-id <KB_ID> --data-source-id <DS_ID>")


if __name__ == "__main__":
    main()
