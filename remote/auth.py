import base64
import hashlib
import hmac
import io
import json
import secrets
import time
from pathlib import Path

import bcrypt
import pyotp

CONFIG_PATH = Path.home() / ".nanoclaw-remote" / "config.json"
_rate_limit: dict[str, dict] = {}
_session_secret: str | None = None


def is_setup_done() -> bool:
    return CONFIG_PATH.exists()


def run_setup(password: str, ntfy_topic: str) -> tuple[str, str, str]:
    """Initialise config. Returns (qr_data_uri, totp_secret, provisioning_uri)."""
    totp_secret = pyotp.random_base32()
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    session_secret = secrets.token_hex(32)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "password_hash": pw_hash,
        "totp_secret": totp_secret,
        "session_secret": session_secret,
        "ntfy_topic": ntfy_topic,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)

    totp = pyotp.TOTP(totp_secret)
    uri = totp.provisioning_uri("Nanoclaw Remote", issuer_name="Nanoclaw")

    try:
        import qrcode as qr_lib
        img = qr_lib.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        qr_data_uri = f"data:image/png;base64,{qr_b64}"
    except Exception:
        qr_data_uri = ""

    return qr_data_uri, totp_secret, uri


def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def get_ntfy_topic() -> str:
    cfg = load_config()
    return (cfg or {}).get("ntfy_topic", "")


def _get_session_secret() -> str:
    global _session_secret
    if _session_secret is None:
        cfg = load_config()
        _session_secret = cfg["session_secret"] if cfg else secrets.token_hex(32)
    return _session_secret


def create_token(expiry_hours: int = 8) -> str:
    expires = int(time.time()) + expiry_hours * 3600
    payload = str(expires)
    sig = hmac.new(
        _get_session_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def verify_token(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode() + b"==").decode()
        payload, sig = raw.rsplit(".", 1)
        if time.time() > int(payload):
            return False
        expected = hmac.new(
            _get_session_secret().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def check_rate_limit(ip: str) -> tuple[bool, str]:
    now = time.time()
    state = _rate_limit.get(ip, {"attempts": 0, "locked_until": 0.0})
    if state["locked_until"] > now:
        wait = int(state["locked_until"] - now)
        return False, f"Too many attempts. Wait {wait}s."
    return True, ""


def record_failed(ip: str) -> None:
    state = _rate_limit.get(ip, {"attempts": 0, "locked_until": 0.0})
    state["attempts"] += 1
    if state["attempts"] >= 5:
        state["locked_until"] = time.time() + 900  # 15-min lockout
        state["attempts"] = 0
    _rate_limit[ip] = state


def clear_rate_limit(ip: str) -> None:
    _rate_limit.pop(ip, None)


def verify_credentials(password: str, totp_code: str) -> tuple[bool, str]:
    cfg = load_config()
    if not cfg:
        return False, "Not configured. Run: python main.py remote --setup"
    if not bcrypt.checkpw(password.encode(), cfg["password_hash"].encode()):
        return False, "Wrong password."
    if not pyotp.TOTP(cfg["totp_secret"]).verify(totp_code, valid_window=1):
        return False, "Invalid authenticator code."
    return True, ""
