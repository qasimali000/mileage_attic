"""One-time helper: get a Gmail refresh token for sending login codes.

1. Google Cloud Console -> new project -> enable "Gmail API".
2. OAuth consent screen: External, add your Gmail as a test user, then PUBLISH the app
   ("In production") so the refresh token doesn't expire after 7 days.
3. Credentials -> Create OAuth client ID -> "Desktop app". Copy the id and secret.
4. Run:  GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python scripts/gmail_token.py
5. Put the printed refresh token in Railway as GMAIL_REFRESH_TOKEN.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
if not (CLIENT_ID and CLIENT_SECRET):
    sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first.")

PORT = 8765
REDIRECT = f"http://localhost:{PORT}"
SCOPE = "https://www.googleapis.com/auth/gmail.send"
got = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        got["code"] = q.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Done - you can close this tab.")

    def log_message(self, *a):
        pass


url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
    "scope": SCOPE, "access_type": "offline", "prompt": "consent",
})
print("Opening browser to authorise...\n", url)
webbrowser.open(url)
HTTPServer(("localhost", PORT), Handler).handle_request()
if not got.get("code"):
    sys.exit("No authorisation code received.")

data = urllib.parse.urlencode({
    "code": got["code"], "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT, "grant_type": "authorization_code",
}).encode()
tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
print("\nGMAIL_REFRESH_TOKEN=" + tok["refresh_token"])
