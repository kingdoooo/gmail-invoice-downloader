# Gmail API Setup

## 1. Create Google Cloud Project

1. Go to https://console.cloud.google.com
2. Create a new project (e.g., "Invoice Downloader")
3. Enable **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable

## 2. Create OAuth Credentials

1. APIs & Services → Credentials → Create Credentials → OAuth client ID
2. First time: configure OAuth consent screen
   - User Type: **External**
   - App name: anything (e.g., "Invoice Downloader")
   - Scopes: add `gmail.readonly`
3. Create OAuth client ID:
   - Application type: **Desktop app**
   - Download `credentials.json`

## 3. Authorize

Place `credentials.json` in your credentials directory, then run:

```bash
python3 scripts/gmail-auth.py
```

1. Open the printed URL in a browser
2. Log in and authorize
3. Browser redirects to `http://localhost?code=XXXXX` (page won't load — that's normal)
4. Copy the `code=` value from the URL bar and paste it back
5. `token.json` is saved — subsequent runs use the refresh token automatically

## Token Refresh

The access token expires in 1 hour. All scripts auto-refresh using the refresh token. No manual intervention needed after initial setup.

## Permissions

`gmail.readonly` grants:
- View email messages and attachments
- Search emails
- View settings (labels, filters)

It does NOT allow: sending, deleting, modifying, or forwarding emails.

Revoke access anytime at: https://myaccount.google.com/permissions
