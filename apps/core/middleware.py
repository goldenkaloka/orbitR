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
        
        # Only process JSON responses
        content_type = response.get('Content-Type', '')
        if 'application/json' in content_type and hasattr(response, 'content'):
            content = response.content.decode('utf-8')
            # Look for the specific IP and port pattern and force http
            pattern = r'https://178\.105\.46\.108:9000'
            replacement = 'http://178.105.46.108:9000'
            
            if re.search(pattern, content):
                new_content = re.sub(pattern, replacement, content)
                response.content = new_content.encode('utf-8')
                # Update Content-Length if it exists
                if response.has_header('Content-Length'):
                    response['Content-Length'] = str(len(response.content))
                    
        return response
