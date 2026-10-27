# FA Knowledge Coach – Pilot

Streamlit app connected to the Ford Academy Google Sheets question bank.

## Deploy
1. Upload `app.py`, `requirements.txt`, `.gitignore` to your GitHub repository.
2. Streamlit Community Cloud → Create app → choose repository and `app.py`.
3. Streamlit app settings → Secrets. Paste your **own** credentials in the format below (never commit them to GitHub):

```toml
OPENAI_API_KEY = "YOUR_KEY"
OPENAI_MODEL = "gpt-4.1-mini"

[gcp_service_account]
type = "service_account"
project_id = "YOUR_PROJECT_ID"
private_key_id = "YOUR_KEY_ID"
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "YOUR_SERVICE_ACCOUNT_EMAIL"
client_id = "YOUR_CLIENT_ID"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "YOUR_CERT_URL"
```

4. Share the Google Sheet with `client_email` as **Editor**.
5. In `Question Bank - L1`, set `Trạng thái duyệt` to `Approved` for **MCQ rows with options and answer key**. For IDs also present in `Question Bank`, approval comes from the master tab.
6. Test Level 1; inspect `Câu trả lời` and `Lượt đánh giá`. Test Level 3/4 with `OPENAI_API_KEY` configured.

## Pilot caveats
- Only one live assessment session per browser; a page reload can reset in-memory session progress. Individual submitted answers remain in Sheets.
- Trainer should check answer keys and rubrics before approval. AI scores are provisional.
- This is a pilot, **not a production-secure assessment platform**. Do not publish a public app containing real student personal information until authentication, rate limiting, access controls, and replay/duplicate protection are added.
- The app intentionally does not enforce the 13:00–11:30 assessment window during testing.
