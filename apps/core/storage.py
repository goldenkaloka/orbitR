from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings

class CustomS3Storage(S3Boto3Storage):
    """
    Standard S3 storage backend using production settings.
    Protocol and domain are handled via AWS_S3_SECURE_URLS and AWS_S3_CUSTOM_DOMAIN.
    """
    pass
