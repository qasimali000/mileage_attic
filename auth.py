"""Email + one-time-code login, session cookies, usage stats."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from functools import wraps

from flask import Blueprint, g, jsonify, request

import db
import mailer

bp = Blueprint("auth", __name__)

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
COOKIE = "attic_session"

CODE_TTL = 10 * 60
SESSION_TTL = 30 * 24 * 3600
MAX_ATTEMPTS = 5
PER_EMAIL_LIMIT = (3, 15 * 60)   # requests, window seconds
PER_IP_LIMIT = (10, 3600)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "trashmail.com", "yopmail.com", "sharklasers.com",
    "getnada.com", "throwawaymail.com", "dispostable.com", "maildrop.cc",
}


def _hash(value):
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _now():
    return int(time.time())


def _client_ip():
    return request.remote_addr or "unknown"


def _error(msg, status):
    return jsonify({"error": msg}), status


def current_email():
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.execute(
        "SELECT email FROM sessions WHERE token_hash=? AND expires_at>?",
        (_hash(token), _now()), fetch="one",
    )
    return row[0] if row else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        email = current_email()
        if not email:
            return _error("ACCESS DENIED", 401)
        g.email = email
        return fn(*a, **kw)
    return wrapper


def log_run(email, journeys, miles):
    db.execute(
        "INSERT INTO runs (email, created_at, journeys, miles) VALUES (?,?,?,?)",
        (email, _now(), journeys, miles),
    )


def _cleanup():
    now = _now()
    db.execute("DELETE FROM login_codes WHERE expires_at<?", (now,))
    db.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
    db.execute("DELETE FROM code_requests WHERE created_at<?", (now - 86400,))


def _send_async(email, code):
    def run():
        try:
            mailer.send_code(email, code)
        except Exception as e:  # never log the code itself
            print(f"[auth] failed to send code to {email}: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


@bp.route("/auth/request-code", methods=["POST"])
def request_code():
    body = request.get_json(silent=True) or {}
    if body.get("website"):  # honeypot: real users never fill this
        return jsonify({"ok": True})
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return _error("Enter a valid email address.", 400)
    if email.rsplit("@", 1)[1] in DISPOSABLE:
        return _error("Disposable email addresses aren't accepted.", 400)

    _cleanup()
    now = _now()
    n_email = db.execute(
        "SELECT COUNT(*) FROM code_requests WHERE email=? AND created_at>?",
        (email, now - PER_EMAIL_LIMIT[1]), fetch="one")[0]
    n_ip = db.execute(
        "SELECT COUNT(*) FROM code_requests WHERE ip=? AND created_at>?",
        (_client_ip(), now - PER_IP_LIMIT[1]), fetch="one")[0]
    if n_email >= PER_EMAIL_LIMIT[0] or n_ip >= PER_IP_LIMIT[0]:
        return _error("Too many requests. Please wait a few minutes.", 429)

    code = f"{secrets.randbelow(10**6):06d}"
    db.execute(
        "INSERT INTO login_codes (email, code_hash, expires_at, attempts) VALUES (?,?,?,0) "
        "ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash, "
        "expires_at=excluded.expires_at, attempts=0",
        (email, _hash(email + ":" + code), now + CODE_TTL),
    )
    db.execute("INSERT INTO code_requests (ip, email, created_at) VALUES (?,?,?)",
               (_client_ip(), email, now))
    _send_async(email, code)
    return jsonify({"ok": True})


@bp.route("/auth/verify", methods=["POST"])
def verify():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    code = re.sub(r"\D", "", str(body.get("code", "")))
    row = db.execute(
        "SELECT code_hash, expires_at, attempts FROM login_codes WHERE email=?",
        (email,), fetch="one")
    now = _now()
    if not row or row[1] < now:
        return _error("Code expired — request a new one.", 400)
    if row[2] >= MAX_ATTEMPTS:
        db.execute("DELETE FROM login_codes WHERE email=?", (email,))
        return _error("Too many attempts — request a new code.", 429)
    if not hmac.compare_digest(row[0], _hash(email + ":" + code)):
        db.execute("UPDATE login_codes SET attempts=attempts+1 WHERE email=?", (email,))
        return _error("Wrong code.", 400)

    db.execute("DELETE FROM login_codes WHERE email=?", (email,))
    db.execute(
        "INSERT INTO users (email, first_seen, last_seen, login_count) VALUES (?,?,?,1) "
        "ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen, "
        "login_count=users.login_count+1",
        (email, now, now),
    )
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions (token_hash, email, expires_at) VALUES (?,?,?)",
               (_hash(token), email, now + SESSION_TTL))
    resp = jsonify({"ok": True, "email": email})
    resp.set_cookie(
        COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Lax",
        secure=request.headers.get("X-Forwarded-Proto", request.scheme) == "https",
    )
    return resp


@bp.route("/auth/me")
def me():
    email = current_email()
    if not email:
        return _error("not signed in", 401)
    return jsonify({"email": email})


@bp.route("/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get(COOKIE)
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@bp.route("/admin/stats")
def stats():
    auth = request.headers.get("Authorization", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(auth, f"Bearer {ADMIN_TOKEN}"):
        return _error("not found", 404)
    now = _now()
    day = 86400

    def one(sql, *p):
        return db.execute(sql, p, fetch="one")[0] or 0

    return jsonify({
        "total_users": one("SELECT COUNT(*) FROM users"),
        "new_users_7d": one("SELECT COUNT(*) FROM users WHERE first_seen>?", now - 7 * day),
        "new_users_30d": one("SELECT COUNT(*) FROM users WHERE first_seen>?", now - 30 * day),
        "active_users_7d": one("SELECT COUNT(DISTINCT email) FROM runs WHERE created_at>?", now - 7 * day),
        "active_users_30d": one("SELECT COUNT(DISTINCT email) FROM runs WHERE created_at>?", now - 30 * day),
        "total_runs": one("SELECT COUNT(*) FROM runs"),
        "total_miles": round(float(one("SELECT SUM(miles) FROM runs")), 2),
    })
