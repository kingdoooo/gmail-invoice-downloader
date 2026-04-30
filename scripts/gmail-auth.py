#!/usr/bin/env python3
"""Gmail API OAuth2 authorization - run once to get refresh token."""
import json
import sys
import urllib.parse
import urllib.request

CREDS_PATH = "/home/ubuntu/.openclaw/credentials/gmail/credentials.json"
TOKEN_PATH = "/home/ubuntu/.openclaw/credentials/gmail/token.json"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"

with open(CREDS_PATH) as f:
    creds = json.load(f)["installed"]

client_id = creds["client_id"]
client_secret = creds["client_secret"]
auth_uri = creds["auth_uri"]
token_uri = creds["token_uri"]
redirect_uri = creds["redirect_uris"][0]

# Step 1: Generate auth URL
params = urllib.parse.urlencode({
    "client_id": client_id,
    "redirect_uri": redirect_uri,
    "response_type": "code",
    "scope": SCOPES,
    "access_type": "offline",
    "prompt": "consent",
})
auth_url = f"{auth_uri}?{params}"

print("=" * 60)
print("Open this URL in your browser and authorize:")
print()
print(auth_url)
print()
print("=" * 60)
print()

# Step 2: Get the authorization code
code = input("Paste the authorization code here: ").strip()

# Step 3: Exchange code for tokens
data = urllib.parse.urlencode({
    "code": code,
    "client_id": client_id,
    "client_secret": client_secret,
    "redirect_uri": redirect_uri,
    "grant_type": "authorization_code",
}).encode()

req = urllib.request.Request(token_uri, data=data, method="POST")
req.add_header("Content-Type", "application/x-www-form-urlencoded")

try:
    with urllib.request.urlopen(req) as resp:
        token_data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f"Error: {e.code} {e.read().decode()}")
    sys.exit(1)

# Save token
with open(TOKEN_PATH, "w") as f:
    json.dump(token_data, f, indent=2)

print()
print(f"✅ Token saved to {TOKEN_PATH}")
print(f"   access_token: {token_data.get('access_token', 'N/A')[:20]}...")
print(f"   refresh_token: {'yes' if token_data.get('refresh_token') else 'no'}")
print(f"   expires_in: {token_data.get('expires_in', 'N/A')}s")
