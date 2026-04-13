import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings

security = HTTPBearer()
settings = get_settings()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Validates the Bearer token sent by the Flutter app.

    Priority:
    1. If SUPABASE_JWT_SECRET is set → verify as a Supabase-signed JWT (HS256).
       Extracts sub (user_id), role, and is_anonymous from the payload.
    2. If JWT verification fails but token matches API_BEARER_TOKEN → allow as guest.
    3. Otherwise → 401 Unauthorized.
    """
    token = credentials.credentials

    if settings.SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired. Please sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError:
            pass  # fall through to static token check

    # Fallback: static dev/guest token
    if token == settings.API_BEARER_TOKEN:
        return {"sub": "anonymous", "role": "anon", "is_anonymous": True}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
