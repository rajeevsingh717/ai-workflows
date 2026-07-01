"""Google Drive download with OAuth2 authentication."""
import os
import re
import tempfile
from pathlib import Path

TOKEN_PATH = Path.home() / ".config" / "gdrive" / "token.json"
SECRETS_PATH = Path.home() / ".config" / "gdrive" / "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def extract_file_id(url: str) -> str:
    """Extract a Google Drive file ID from any Drive URL format."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    # If it looks like a raw file ID (no slashes, right length)
    if re.match(r"^[a-zA-Z0-9_-]{25,}$", url):
        return url
    raise ValueError(f"Could not extract file ID from: {url}")


def get_drive_service():
    """Return an authenticated Drive API service. Opens browser on first run."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Google Drive packages not installed.\n"
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    if not SECRETS_PATH.exists():
        raise RuntimeError(
            f"Google OAuth credentials not found at {SECRETS_PATH}\n\n"
            "Setup steps:\n"
            "  1. Go to console.cloud.google.com → your project\n"
            "  2. APIs & Services → Enable APIs → enable 'Google Drive API'\n"
            "  3. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID\n"
            "  4. Application type: Desktop app → Download JSON\n"
            f"  5. Save the downloaded file to: {SECRETS_PATH}"
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def download_file(url_or_id: str) -> tuple[str, str, str]:
    """
    Download a file from Google Drive to a temp file.
    Returns (local_path, filename, file_id).
    """
    from googleapiclient.http import MediaIoBaseDownload
    import io

    file_id = extract_file_id(url_or_id)
    service = get_drive_service()

    # Get file metadata
    meta = service.files().get(
        fileId=file_id,
        fields="name,mimeType,size"
    ).execute()

    filename = meta.get("name", file_id)
    mime_type = meta.get("mimeType", "")

    # Determine suffix
    suffix = Path(filename).suffix.lower()
    if not suffix:
        if "pdf" in mime_type:
            suffix = ".pdf"
        elif "image" in mime_type:
            suffix = ".jpg"
        else:
            suffix = ".bin"

    # Download to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(tmp, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    tmp.flush()
    return tmp.name, filename, file_id


def auth_flow() -> None:
    """Run the one-time OAuth browser flow and save the token."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()  # force fresh login
    get_drive_service()
    print(f"Authentication successful. Token saved to {TOKEN_PATH}")
