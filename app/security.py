from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets
from urllib.parse import unquote, urlsplit

from flask import abort, redirect, request, session, url_for


PBKDF2_ITERATIONS = 260_000
MAX_AUTHORIZED_ORDERS = 50


def hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_secret(value: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    token = session.get("csrf_token")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or not submitted or not hmac.compare_digest(token, submitted):
        abort(400, "Invalid CSRF token")


def authorize_order(order_no: str) -> None:
    """Grant this signed browser session access to one order only."""
    authorized = list(session.get("authorized_orders", []))
    if order_no in authorized:
        authorized.remove(order_no)
    authorized.append(order_no)
    session["authorized_orders"] = authorized[-MAX_AUTHORIZED_ORDERS:]


def can_reveal_order(order) -> bool:
    """Only the browser session that created the order, or that proved the
    order's query password, may see delivered cards. Knowing the contact
    email alone is never enough (it is unverified and easy to guess)."""
    return order["order_no"] in session.get("authorized_orders", [])


def safe_redirect_path(target: str | None, fallback: str) -> str:
    """Accept only a local absolute path, including after URL decoding."""
    if not target:
        return fallback
    decoded = unquote(target)
    if (
        not target.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        return fallback
    try:
        parsed = urlsplit(target)
    except ValueError:
        return fallback
    return target if not parsed.scheme and not parsed.netloc else fallback


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        admin_user_id = session.get("admin_user_id")
        if not admin_user_id:
            return redirect(url_for("admin_login", next=request.path))
        from .db import fetch_one

        admin = fetch_one(
            "SELECT session_version FROM admin_users WHERE id = ?", (admin_user_id,)
        )
        # 退出或改密码会递增 session_version，复制走的旧 Cookie 随之失效。
        if not admin or session.get("admin_session_version") != admin["session_version"]:
            session.clear()
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
