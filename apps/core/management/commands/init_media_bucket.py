import json

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Create the media bucket on the S3-compatible store and make its "
        "objects publicly readable. Idempotent — safe to run on every boot."
    )

    def _client(self):
        return boto3.client(
            "s3",
            endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            verify=settings.AWS_S3_VERIFY,
        )

    def handle(self, *args, **options):
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        client = self._client()

        self._ensure_bucket(client, bucket)
        self._ensure_public_read(client, bucket)
        self.stdout.write(self.style.SUCCESS(f"Media bucket ready: {bucket}"))

    def _ensure_bucket(self, client, bucket):
        try:
            client.head_bucket(Bucket=bucket)
            self.stdout.write(f"Bucket '{bucket}' already exists.")
        except ClientError:
            params = {"Bucket": bucket}
            # MinIO and most S3-compatible servers reject us-east-1 as a
            # location constraint, which is also the only region they accept.
            region = getattr(settings, "AWS_S3_REGION_NAME", "us-east-1")
            if region and region != "us-east-1":
                params["CreateBucketConfiguration"] = {
                    "LocationConstraint": region
                }
            client.create_bucket(**params)
            self.stdout.write(f"Created bucket '{bucket}'.")

    def _ensure_public_read(self, client, bucket):
        client.put_bucket_policy(
            Bucket=bucket,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "PublicReadProductMedia",
                            "Effect": "Allow",
                            "Principal": "*",
                            "Action": "s3:GetObject",
                            "Resource": f"arn:aws:s3:::{bucket}/*",
                        }
                    ],
                }
            ),
        )
