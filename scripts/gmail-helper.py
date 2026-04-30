#!/usr/bin/env python3
"""Gmail API helper - search and list messages with token refresh."""
import json
import sys
import urllib.parse
import urllib.request
import time

CREDS_PATH = "/home/ubuntu/.openclaw/credentials/gmail/credentials.json"
TOKEN_PATH = "/home/ubuntu/.openclaw/credentials/gmail/token.json"

def load_creds():
    with open(CREDS_PATH) as f:
        return json.load(f)["installed"]

def load_token():
    with open(TOKEN_PATH) as f:
        return json.load(f)

def save_token(token):
    with open(TOKEN_PATH, "w") as f:
        json.dump(token, f, indent=2)

def refresh_access_token():
    creds = load_creds()
    token = load_token()
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(creds["token_uri"], data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read())
    token["access_token"] = new_token["access_token"]
    token["expires_in"] = new_token.get("expires_in", 3600)
    save_token(token)
    return token

def api_get(url, token=None):
    if token is None:
        token = load_token()
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token = refresh_access_token()
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {token['access_token']}")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        raise

def search_messages(query, max_results=50):
    q = urllib.parse.quote(query)
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults={max_results}"
    return api_get(url)

def get_message(msg_id, fmt="full"):
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format={fmt}"
    return api_get(url)

def get_attachment(msg_id, att_id):
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{att_id}"
    return api_get(url)

if __name__ == "__main__":
    # Support: gmail-helper.py "query" OR gmail-helper.py search "query"
    if len(sys.argv) >= 3 and sys.argv[1] == "search":
        query = sys.argv[2]
    elif len(sys.argv) >= 2 and sys.argv[1] != "search":
        query = sys.argv[1]
    else:
        query = "发票 OR invoice"
    print(f"Searching: {query}")
    result = search_messages(query)
    msgs = result.get("messages", [])
    print(f"Found: {len(msgs)} messages")
    
    for msg in msgs:
        detail = get_message(msg["id"], "metadata")
        headers = {h["name"]: h["value"] for h in detail["payload"].get("headers", [])}
        subj = headers.get("Subject", "(no subject)")
        frm = headers.get("From", "?")
        date = headers.get("Date", "?")
        print(f"  [{date}] {frm[:50]} | {subj}")
