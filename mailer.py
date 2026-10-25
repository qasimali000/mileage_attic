"""Send the login code. HTTPS-only providers (Railway Hobby blocks SMTP).

EMAIL_PROVIDER = gmail | resend | console. If unset: gmail when GMAIL_REFRESH_TOKEN
is present, resend when RESEND_API_KEY is present, otherwise console (local dev only).
"""
import base64
import os
from email.message import EmailMessage

import requests

SENDER = os.environ.get("EMAIL_FROM", "")  # e.g. "The Mileage Attic <you@gmail.com>"


def provider():
    p = os.environ.get("EMAIL_PROVIDER", "").lower()
    if p:
        return p
    if os.environ.get("GMAIL_REFRESH_TOKEN"):
        return "gmail"
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    return "console"


def _gmail_access_token():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _send_gmail(to, subject, text):
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = SENDER or "me"
    msg["Subject"] = subject
    msg.set_content(text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        json={"raw": raw},
        timeout=15,
    )
    r.raise_for_status()


def _send_resend(to, subject, text):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": SENDER, "to": [to], "subject": subject, "text": text},
        timeout=15,
    )
    r.raise_for_status()


def send_code(to, code):
    subject = f"{code} is your Mileage Attic code"
    text = (
        f"Your Mileage Attic login code is {code}\n\n"
        "It expires in 10 minutes. If you didn't ask for it, ignore this email.\n"
    )
    p = provider()
    if p == "gmail":
        _send_gmail(to, subject, text)
    elif p == "resend":
        _send_resend(to, subject, text)
    elif p == "console" and not os.environ.get("RAILWAY_ENVIRONMENT"):
        print(f"[mailer:console] to={to} code={code}", flush=True)
    else:
        raise RuntimeError(f"no usable email provider ({p})")
