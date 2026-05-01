from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings

class CustomS3Storage(S3Boto3Storage):
    """
    Custom S3 storage backend to forcefully override URL protocol
    when testing on IP-based VPS without SSL.
    """
    def url(self, name, parameters=None, expire=None, http_method=None):
        url = super().url(name, parameters, expire, http_method)
        
        # If we are explicitly using an http:// endpoint in settings, 
        # ensure the generated URL matches that protocol.
        if settings.MINIO_EXTERNAL_URL.startswith('http://') and url.startswith('https://'):
            return url.replace('https://', 'http://', 1)
            
        return url
