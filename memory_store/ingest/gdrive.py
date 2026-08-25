"""Google Drive OAuth and file-download helpers."""

import io
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

STORE_DIR = Path(__file__).resolve().parents[1]
SECRETS_PATH = STORE_DIR / "gdrive_credentials.json"
TOKEN_PATH = STORE_DIR / "gdrive_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def extract_file_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    patterns = [r"/d/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise ValueError("Could not extract a Google Drive file ID")


def auth_flow() -> Credentials:
    credentials = None
    if TOKEN_PATH.exists():
        credentials = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not SECRETS_PATH.exists():
            raise FileNotFoundError(f"Google OAuth credentials not found: {SECRETS_PATH}")
        flow = InstalledAppFlow.from_client_secrets_file(SECRETS_PATH, SCOPES)
        credentials = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(credentials.to_json())
    return credentials


def _service():
    return build("drive", "v3", credentials=auth_flow(), cache_discovery=False)


def download_file(source: str) -> tuple[str, str, str]:
    file_id = extract_file_id(source)
    service = _service()
    metadata = service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    filename = metadata.get("name") or file_id
    mime_type = metadata.get("mimeType", "")

    export_types = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    }
    if mime_type in export_types:
        export_mime, suffix = export_types[mime_type]
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        if not filename.lower().endswith(suffix):
            filename += suffix
    else:
        request = service.files().get_media(fileId=file_id)

    download_dir = STORE_DIR / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    destination = download_dir / Path(filename).name
    with destination.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return str(destination), destination.name, file_id
