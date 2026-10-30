"""License：HMAC 离线签名与校验（FR-A4；03 §1.2）。

激活码 = base64url(payload_json) + "." + base64url(hmac_sha256(payload, secret))
payload = {fingerprint, expires_at, modules, issued_at}
四类失败码：INVALID_SIGN / MACHINE_MISMATCH / EXPIRED / CLOCK_ROLLBACK
"""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.core.config import get_settings

settings = get_settings()


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def generate_license(machine_fingerprint: str, days: int = 365, modules: list[str] | None = None) -> str:
    """签发（开发用：tests / 管理端调用）。"""
    payload = json.dumps(
        {
            "fingerprint": machine_fingerprint,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
            "modules": modules or ["ops"],
            "issued_at": datetime.now(timezone.utc).isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    sig = hmac.new(settings.jwt_secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_license(license_key: str, machine_fingerprint: str) -> dict:
    try:
        payload_part, sig_part = license_key.split(".", 1)
        payload = _b64d(payload_part)
        expect = hmac.new(settings.jwt_secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig_part), expect):
            raise HTTPException(402, "INVALID_SIGN")
        data = json.loads(payload)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(402, "INVALID_SIGN")

    if data["fingerprint"] != machine_fingerprint:
        raise HTTPException(402, "MACHINE_MISMATCH")
    now = datetime.now(timezone.utc)
    issued = datetime.fromisoformat(data["issued_at"])
    expires = datetime.fromisoformat(data["expires_at"])
    if now < issued - timedelta(minutes=5):  # 服务器时钟回拨到签发前
        raise HTTPException(402, "CLOCK_ROLLBACK")
    if now > expires:
        raise HTTPException(402, "EXPIRED")
    return data


def machine_fingerprint() -> str:
    """本机指纹（与 Agent 同源算法：序列号+CPU+首网卡 MAC，03 §1.2）。"""
    import platform
    import subprocess

    parts = [platform.node(), platform.machine()]
    try:  # macOS: 硬件 UUID
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if "IOPlatformUUID" in line:
                parts.append(line.split("=")[-1].strip().strip('"'))
                break
    except Exception:
        pass
    try:  # 首块网卡 MAC
        out = subprocess.run(["ifconfig", "en0"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if "ether" in line:
                parts.append(line.split()[1])
                break
    except Exception:
        pass
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def new_agent_token() -> str:
    return secrets.token_hex(16)
