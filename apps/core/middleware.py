import re

class ForceHTTPImagesMiddleware:
    """
    Middleware to forcefully replace https with http for image URLs 
    pointing to the MinIO server on port 9000.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        try:
            # Only process JSON responses that have content we can read
            content_type = response.get('Content-Type', '')
            if content_type and 'application/json' in content_type and hasattr(response, 'content'):
                # Try to decode content safely
                content = response.content.decode('utf-8', errors='ignore')
                
                # Look for the specific IP and port pattern and force http
                pattern = r'https://178\.105\.46\.108:9000'
                replacement = 'http://178.105.46.108:9000'
                
                if 'https://178.105.46.108:9000' in content:
                    new_content = content.replace('https://178.105.46.108:9000', 'http://178.105.46.108:9000')
                    response.content = new_content.encode('utf-8')
                    # Update Content-Length if it exists
                    if response.has_header('Content-Length'):
                        response['Content-Length'] = str(len(response.content))
        except Exception:
            # If middleware fails, just return original response instead of crashing
            pass
                    
        return response
