from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings

class CustomS3Storage(S3Boto3Storage):
    """
    Standard S3 storage backend using production settings.
    Protocol and domain are handled via AWS_S3_SECURE_URLS and AWS_S3_CUSTOM_DOMAIN.
    """
    pass

from whitenoise.storage import CompressedManifestStaticFilesStorage

class SafeCompressedManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """
    Bypasses FileNotFoundError during collectstatic if a file is missing.
    Useful for vendor files (like select2 i18n) that may be inconsistently present.
    """
    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content, filename)
        except (ValueError, FileNotFoundError):
            return name
