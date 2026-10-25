"""Shared password + name/email/phone sign-in (unverified), session cookies, usage stats."""
import hashlib
import hmac
import os
import re
import secrets
import time
from functools import wraps

import dns.exception
import dns.resolver
import requests
from flask import Blueprint, g, jsonify, request

import db

bp = Blueprint("auth", __name__)

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
COOKIE = "attic_session"

SESSION_TTL = 30 * 24 * 3600
IP_LIMIT = (10, 3600)  # sign-ups per IP, window seconds

UK_MOBILE_RE = re.compile(r"^7[1-9]\d{8}$")  # national number after the leading 0 / +44
EMAIL_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com",
    "gmail.co": "gmail.com", "gmail.con": "gmail.com", "gnail.com": "gmail.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "yahooo.com": "yahoo.com", "yaho.com": "yahoo.com", "yahoo.con": "yahoo.com",
    "outlok.com": "outlook.com", "outlook.co": "outlook.com", "outlook.con": "outlook.com",
}
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


def normalise_uk_mobile(raw):
    """-> '+447...' or None. Accepts 07..., +447..., 00447... with any spacing."""
    p = re.sub(r"[\s\-().]", "", raw)
    if p.startswith("+44"):
        p = p[3:]
    elif p.startswith("0044"):
        p = p[4:]
    elif p.startswith("0"):
        p = p[1:]
    else:
        return None
    if p.startswith("0"):  # +44 (0)7... written out
        p = p[1:]
    if not UK_MOBILE_RE.fullmatch(p) or len(set(p[1:])) < 3:
        return None
    return "+44" + p


def email_domain_ok(domain):
    """False only when DNS says the domain can't receive mail; fails open on timeouts."""
    for rtype in ("MX", "A"):
        try:
            dns.resolver.resolve(domain, rtype, lifetime=3)
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            continue
        except dns.resolver.NoNameservers:
            return False
        except dns.exception.DNSException:
            return True
    return False


def turnstile_ok(token):
    if not TURNSTILE_SECRET:
        return not ON_RAILWAY  # unset: allow locally, refuse in production
    if not token:
        return False
    try:
        r = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": _client_ip()},
            timeout=10,
        )
        return bool(r.json().get("success"))
    except Exception:
        return False


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
    db.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
    db.execute("DELETE FROM code_requests WHERE created_at<?", (now - 86400,))


@bp.route("/auth/register", methods=["POST"])
def register():
    body = request.get_json(silent=True) or {}
    if body.get("website"):  # honeypot: real users never fill this
        return jsonify({"ok": True})
    now = _now()
    n_ip = db.execute(
        "SELECT COUNT(*) FROM code_requests WHERE ip=? AND created_at>?",
        (_client_ip(), now - IP_LIMIT[1]), fetch="one")[0]
    if n_ip >= IP_LIMIT[0]:
        return _error("Too many attempts. Please wait a few minutes.", 429)

    password = str(body.get("password", ""))
    if not APP_PASSWORD or not hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        db.execute("INSERT INTO code_requests (ip, email, created_at) VALUES (?,?,?)",
                   (_client_ip(), "", now))  # failed guesses count toward the IP limit
        return _error("Wrong password.", 403)

    name = " ".join(str(body.get("name", "")).split())
    if not 2 <= len(name) <= 80:
        return _error("Enter your name.", 400)
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return _error("Enter a valid email address.", 400)
    domain = email.rsplit("@", 1)[1]
    if domain in EMAIL_TYPOS:
        return _error(f"Did you mean @{EMAIL_TYPOS[domain]}?", 400)
    if domain in DISPOSABLE:
        return _error("Disposable email addresses aren't accepted.", 400)
    phone = normalise_uk_mobile(str(body.get("phone", "")))
    if not phone:
        return _error("Enter a valid UK mobile number (e.g. 07123 456789).", 400)
    if not turnstile_ok(body.get("turnstile")):
        return _error("Verification failed. Please refresh and try again.", 400)
    if not email_domain_ok(domain):
        return _error("That email domain can't receive mail. Check for typos.", 400)
    owner = db.execute("SELECT email FROM users WHERE phone=? AND email<>?",
                       (phone, email), fetch="one")
    if owner:
        return _error("That phone number is already registered with a different email.", 400)

    _cleanup()
    db.execute("INSERT INTO code_requests (ip, email, created_at) VALUES (?,?,?)",
               (_client_ip(), email, now))

    db.execute(
        "INSERT INTO users (email, name, phone, first_seen, last_seen, login_count) "
        "VALUES (?,?,?,?,?,1) "
        "ON CONFLICT(email) DO UPDATE SET name=excluded.name, phone=excluded.phone, "
        "last_seen=excluded.last_seen, login_count=users.login_count+1",
        (email, name, phone, now, now),
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


@bp.route("/auth/config")
def config():
    return jsonify({"turnstile_site_key": TURNSTILE_SITE_KEY})


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
