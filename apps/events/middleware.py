import jwt
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from channels.db import database_sync_to_async
from apps.users.models import User
from urllib.parse import parse_qs

@database_sync_to_async
def get_user(user_id):
    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return AnonymousUser()

class JWTAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        query_string = scope.get('query_string', b'').decode()
        query_params = parse_qs(query_string)
        token = query_params.get('token', [None])[0]

        if token:
            try:
                # Assuming standard JWT setup. Update if using SimpleJWT specific decoding.
                # Here we use settings.SECRET_KEY, but if using SimpleJWT, 
                # you might want to use their authentication backend.
                payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
                user_id = payload.get('user_id')
                user = await get_user(user_id)
                scope['user'] = user
                print(f"DEBUG: WebSocket JWT Auth successful for user: {user} (ID: {user_id})")
            except jwt.ExpiredSignatureError:
                print("DEBUG: WebSocket JWT Auth failed: Token expired")
                scope['user'] = AnonymousUser()
            except jwt.DecodeError:
                print("DEBUG: WebSocket JWT Auth failed: Decode error")
                scope['user'] = AnonymousUser()
            except Exception as e:
                print(f"DEBUG: WebSocket JWT Auth failed: {str(e)}")
                scope['user'] = AnonymousUser()
        else:
            print("DEBUG: WebSocket JWT Auth failed: No token provided")
            scope['user'] = AnonymousUser()

        return await self.inner(scope, receive, send)

def JWTAuthMiddlewareStack(inner):
    return JWTAuthMiddleware(inner)
