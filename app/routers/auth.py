import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from app.config import cfg
from app.services import auth_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / Response schemas ────────────────────────────────────────────────

class SignUpRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

class SignInRequest(BaseModel):
    email: EmailStr
    password: str

class GuestRequest(BaseModel):
    name: str
    email: EmailStr

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class VerifyOtpRequest(BaseModel):
    email: EmailStr
    token: str

class ResendOtpRequest(BaseModel):
    email: EmailStr

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str

class AuthResponse(BaseModel):
    token: str | None
    needs_otp: bool = False
    user: dict


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=AuthResponse)
async def signup(req: SignUpRequest):
    try:
        result = auth_service.signup(req.name, req.email, req.password)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[AUTH] Signup error: {e}")
        raise HTTPException(500, "Sign up failed. Please try again.")


@router.post("/signin", response_model=AuthResponse)
async def signin(req: SignInRequest):
    try:
        result = auth_service.signin(req.email, req.password)
        return result
    except ValueError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        logger.error(f"[AUTH] Signin error: {e}")
        raise HTTPException(500, "Sign in failed. Please try again.")


@router.post("/admin-login", response_model=AuthResponse)
async def admin_login(req: AdminLoginRequest):
    """The only way to obtain an admin session - no signup, no OTP, no social
    login. Backs the single fixed super-admin account that runs /admin.html."""
    from app.services import admin_auth
    if not admin_auth.verify_admin_credentials(req.email, req.password):
        raise HTTPException(401, "Invalid admin credentials")
    token = admin_auth.issue_admin_token()
    logger.info(f"[AUTH] Admin signin: {req.email}")
    return {
        "token": token,
        "user": {
            "id":    "admin",
            "email": req.email,
            "name":  "Admin",
            "role":  "admin",
        },
    }


@router.post("/guest", response_model=AuthResponse)
async def guest_signin(req: GuestRequest):
    token = auth_service.create_guest_token(req.name, req.email)
    logger.info(f"[AUTH] Guest signin: {req.email}")
    return {
        "token": token,
        "user": {
            "id":    f"guest-{req.email}",
            "email": req.email,
            "name":  req.name,
            "role":  "guest",
        },
    }


@router.post("/verify-otp", response_model=AuthResponse)
async def verify_otp(req: VerifyOtpRequest):
    try:
        result = auth_service.verify_otp(req.email, req.token)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[AUTH] OTP verify error: {e}")
        raise HTTPException(500, "Verification failed. Please try again.")


@router.post("/resend-otp")
async def resend_otp(req: ResendOtpRequest):
    try:
        sb = auth_service._supabase()
        sb.auth.sign_in_with_otp({
            "email": req.email,
            "options": {"should_create_user": False},
        })
        logger.info(f"[AUTH] OTP resent: {req.email}")
    except Exception as e:
        logger.warning(f"[AUTH] Resend OTP failed: {e}")
    # Always return success to prevent email enumeration
    return {"message": "Code sent"}


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    try:
        auth_service.forgot_password(req.email)
        return {"message": "Password reset email sent. Check your inbox."}
    except Exception as e:
        logger.error(f"[AUTH] Forgot password error: {e}")
        # Always return success to avoid email enumeration
        return {"message": "Password reset email sent. Check your inbox."}


@router.get("/oauth-config")
async def oauth_config():
    """Public config the frontend needs to drive Supabase's own OAuth redirect
    flow for 'Continue with Google' / 'Continue with Microsoft' (client-side,
    via the Supabase JS SDK - this app never handles the OAuth handshake
    itself, same as how Supabase already handles email/password and OTP).

    The anon key is Supabase's publishable key - safe to expose to the
    browser by design (it's rate-limited and gated by RLS, not a secret).
    Returns nulls if Supabase isn't configured, so the frontend can hide the
    social-login buttons instead of showing something that will just fail.
    """
    if not cfg.supabase_url or not cfg.supabase_anon_key:
        return {"supabase_url": None, "supabase_anon_key": None}
    return {"supabase_url": cfg.supabase_url, "supabase_anon_key": cfg.supabase_anon_key}


@router.get("/me")
async def me(request: Request):
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = auth_service.verify_token(token)
    if not user:
        raise HTTPException(401, "Invalid or expired token")
    return user


# ── Helper ────────────────────────────────────────────────────────────────────

def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None
