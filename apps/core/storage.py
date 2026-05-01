from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings

class CustomS3Storage(S3Boto3Storage):
    """
    Custom S3 storage backend to forcefully override URL protocol
    when testing on IP-based VPS without SSL.
    """
    def url(self, name, parameters=None, expire=None, http_method=None):
        url = super().url(name, parameters, expire, http_method)
        
        # Force HTTP if the URL contains our VPS IP and we are not using SSL
        if url and '178.105.46.108' in url and ':9000' in url:
            if url.startswith('https://'):
                return url.replace('https://', 'http://', 1)
            elif url.startswith('//'):
                return 'http:' + url
            
        return url
