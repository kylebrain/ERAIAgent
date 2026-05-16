"""
Upload local data files to the Elden Ring KB S3 bucket.

Usage:
    python scripts/upload_to_s3.py --bucket elden-ring-kb-docs-<account-id>

Uploads:
    ./spatial_docs/  -> s3://<bucket>/spatial/
    ./kaggle/        -> s3://<bucket>/kaggle/
"""
import argparse
import os
from pathlib import Path
import boto3

UPLOAD_DIRS = [
    ("./spatial_docs", "spatial/"),
    ("./kaggle", "kaggle/"),
]


def upload_dir(s3, bucket: str, local_dir: Path, prefix: str):
    if not local_dir.exists():
        print(f"  [skip] {local_dir} does not exist")
        return 0

    files = list(local_dir.rglob("*"))
    files = [f for f in files if f.is_file()]
    if not files:
        print(f"  [skip] {local_dir} is empty")
        return 0

    count = 0
    for f in files:
        key = prefix + str(f.relative_to(local_dir)).replace("\\", "/")
        print(f"  {f} -> s3://{bucket}/{key}")
        s3.upload_file(
            str(f), bucket, key,
            ExtraArgs={"ContentType": "text/plain"},
        )
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True, help="S3 bucket name (KbDocsBucketName from CFN output)")
    parser.add_argument("--profile", help="AWS CLI profile name (optional)")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    s3 = session.client("s3")

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
