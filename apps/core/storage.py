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

    def _compress_path(self, full_path):
        import os
        import logging
        logger = logging.getLogger(__name__)
        
        if not os.path.exists(full_path):
            logger.warning(f"SafeCompressedManifestStaticFilesStorage: File not found for compression, skipping: {full_path}")
            return

        try:
            yield from super()._compress_path(full_path)
        except Exception as e:
            logger.error(f"SafeCompressedManifestStaticFilesStorage: Error compressing file {full_path}: {str(e)}")
            return
