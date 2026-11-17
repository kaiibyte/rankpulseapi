
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.api.dependencies import require_api_available
from app.core.config import SECRET_KEY, _raw_env
from app.core.exceptions import AppError
from app.core.http import _get_client
from app.core.security import CryptoManager


router = APIRouter(
    prefix="/api/rankpulse",
    tags=["rankpulse"],
    dependencies=[Depends(require_api_available)],
)

bearer = HTTPBearer(auto_error=False)

RONE_API = "https://arena.rone.dev/api"


class MLBBAccountInput(BaseModel):
    role_id: int = Field(gt=0)
    zone_id: int = Field(gt=0)


class MLBBVerifyInput(MLBBAccountInput):
    vc: str = Field(min_length=4, max_length=8)


def _config() -> tuple[str, str]:
    url = (_raw_env("SUPABASE_URL") or "").rstrip("/")
    key = _raw_env("SUPABASE_SERVICE_ROLE_KEY") or ""

    if not url or not key:
        raise AppError(
            status_code=503,
            code="NOT_CONFIGURED",
            message="Supabase server configuration is missing.",
        )

    return url, key


def _authenticate(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer),
    ],
) -> str:
    if not credentials:
        raise AppError(
            status_code=401,
            code="UNAUTHORIZED",
            message="A RankPulse login is required.",
        )

    url, key = _config()

    try:
        response = _get_client().get(
            f"{url}/auth/v1/user",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {credentials.credentials}",
            },
        )
    except Exception as exc:
        raise AppError(
            status_code=503,
            code="AUTH_SERVICE_ERROR",
            message="Could not verify your RankPulse session.",
        ) from exc

    if response.status_code != 200:
        raise AppError(
            status_code=401,
            code="INVALID_SESSION",
            message="Your RankPulse session is invalid or expired.",
        )

    try:
        user = response.json()
        user_id = user.get("id") if isinstance(user, dict) else None
    except ValueError:
        user_id = None

    if not user_id:
        raise AppError(
            status_code=401,
            code="INVALID_SESSION",
            message="Could not identify your RankPulse account.",
        )

    return str(user_id)


RankPulseUser = Annotated[str, Depends(_authenticate)]


def _rone_request(
    method: str,
    endpoint: str,
    *,
    payload: dict[str, Any] | None = None,
    jwt: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
    }

    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"

    try:
        response = _get_client().request(
            method,
            f"{RONE_API}{endpoint}",
            headers=headers,
            json=payload,
        )
    except Exception as exc:
        raise AppError(
            status_code=502,
            code="RONE_API_UNAVAILABLE",
            message="Could not contact the Rone Arena API.",
        ) from exc

    if response.status_code != 200:
        raise AppError(
            status_code=502,
            code="RONE_API_ERROR",
            message="The Rone Arena API returned an error.",
        )

    try:
        result = response.json()
    except ValueError as exc:
        raise AppError(
            status_code=502,
            code="INVALID_RONE_RESPONSE",
            message="Rone Arena returned invalid JSON.",
        ) from exc

    if not isinstance(result, dict):
        raise AppError(
            status_code=502,
            code="INVALID_RONE_RESPONSE",
            message="Rone Arena returned an invalid response.",
        )

    return result


def _db_request(
    method: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    prefer: str | None = None,
):
    url, key = _config()

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    try:
        response = _get_client().request(
            method,
            f"{url}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=payload,
        )
    except Exception as exc:
        raise AppError(
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            message="Could not contact the account database.",
        ) from exc

    if response.status_code >= 300:
        raise AppError(
            status_code=502,
            code="DATABASE_REQUEST_FAILED",
            message="The account database rejected the request.",
        )

    return response


def _encrypt_jwt(jwt: str) -> str:
    manager = CryptoManager(
        SECRET_KEY + ":rankpulse:mlbb-session:v1"
    )
    return manager.encrypt(jwt).decode("ascii")


def _save_account(
    user_id: str,
    role_id: int,
    zone_id: int,
    name: str,
    avatar: str,
    jwt: str,
) -> None:
    _db_request(
        "POST",
        "mlbb_account_tokens",
        params={"on_conflict": "user_id"},
        payload={
            "user_id": user_id,
            "encrypted_jwt": _encrypt_jwt(jwt),
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )

    existing = _db_request(
        "GET",
        "mlbb_accounts",
        params={
            "select": "id",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    ).json()

    metadata = {
        "user_id": user_id,
        "role_id": role_id,
        "zone_id": zone_id,
        "mlbb_name": name,
        "avatar_url": avatar,
    }

    if existing:
        _db_request(
            "PATCH",
            "mlbb_accounts",
            params={"user_id": f"eq.{user_id}"},
            payload=metadata,
            prefer="return=minimal",
        )
    else:
        _db_request(
            "POST",
            "mlbb_accounts",
            payload=metadata,
            prefer="return=minimal",
        )


@router.post("/mlbb/send-code")
def send_mlbb_code(
    body: MLBBAccountInput,
    user_id: RankPulseUser,
):
    result = _rone_request(
        "POST",
        "/user/auth/send-vc",
        payload={
            "role_id": body.role_id,
            "zone_id": body.zone_id,
        },
    )

    if str(result.get("code")) != "0":
        raise AppError(
            status_code=400,
            code="VERIFICATION_CODE_FAILED",
            message="Could not send the MLBB verification code.",
        )

    return {
        "success": True,
        "message": "Verification code sent. Check your in-game mail.",
    }


@router.post("/mlbb/verify")
def verify_mlbb_account(
    body: MLBBVerifyInput,
    user_id: RankPulseUser,
):
    try:
        verification_code = int(body.vc)
    except ValueError:
        raise AppError(
            status_code=422,
            code="INVALID_CODE",
            message="The verification code must contain numbers only.",
        )

    login_result = _rone_request(
        "POST",
        "/user/auth/login",
        payload={
            "role_id": body.role_id,
            "zone_id": body.zone_id,
            "vc": verification_code,
        },
    )

    if str(login_result.get("code")) != "0":
        raise AppError(
            status_code=400,
            code="MLBB_LOGIN_FAILED",
            message="The verification code is incorrect or expired.",
        )

    login_data = login_result.get("data")

    if not isinstance(login_data, dict):
        raise AppError(
            status_code=502,
            code="INVALID_MLBB_RESPONSE",
            message="MLBB returned an invalid login response.",
        )

    jwt = login_data.get("jwt")

    if not isinstance(jwt, str) or not jwt:
        raise AppError(
            status_code=502,
            code="MLBB_TOKEN_MISSING",
            message="MLBB did not return an account session.",
        )

    info_result = _rone_request(
        "GET",
        "/user/info",
        jwt=jwt,
    )

    if str(info_result.get("code")) != "0":
        raise AppError(
            status_code=400,
            code="MLBB_PROFILE_FAILED",
            message="Could not verify the MLBB account profile.",
        )

    info = info_result.get("data")

    if not isinstance(info, dict):
        raise AppError(
            status_code=502,
            code="INVALID_MLBB_PROFILE",
            message="MLBB returned invalid profile information.",
        )

    actual_role = info.get("roleId", login_data.get("roleid"))
    actual_zone = info.get("zoneId", login_data.get("zoneid"))

    try:
        if (
            int(actual_role) != body.role_id
            or int(actual_zone) != body.zone_id
        ):
            raise AppError(
                status_code=400,
                code="ACCOUNT_MISMATCH",
                message="The verified MLBB account does not match the supplied IDs.",
            )
    except (TypeError, ValueError):
        raise AppError(
            status_code=502,
            code="ACCOUNT_ID_MISSING",
            message="MLBB did not return the account IDs needed for verification.",
        )

    name = str(info.get("name") or "")
    avatar = str(info.get("avatar") or "")

    _save_account(
        user_id=user_id,
        role_id=body.role_id,
        zone_id=body.zone_id,
        name=name,
        avatar=avatar,
        jwt=jwt,
    )

    return {
        "success": True,
        "message": "MLBB account linked successfully.",
        "account": {
            "role_id": body.role_id,
            "zone_id": body.zone_id,
            "mlbb_name": name,
            "avatar_url": avatar,
        },
    }


@router.get("/mlbb/me")
def get_linked_mlbb_account(user_id: RankPulseUser):
    result = _db_request(
        "GET",
        "mlbb_accounts",
        params={
            "select": "role_id,zone_id,mlbb_name,avatar_url,linked_at",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    ).json()

    if not result:
        return {
            "linked": False,
            "account": None,
        }

    return {
        "linked": True,
        "account": result[0],
    }


@router.delete("/mlbb/disconnect")
def disconnect_mlbb_account(user_id: RankPulseUser):
    _db_request(
        "DELETE",
        "mlbb_account_tokens",
        params={"user_id": f"eq.{user_id}"},
    )

    _db_request(
        "DELETE",
        "mlbb_accounts",
        params={"user_id": f"eq.{user_id}"},
    )

    return {
        "success": True,
        "message": "MLBB account disconnected.",
    }
