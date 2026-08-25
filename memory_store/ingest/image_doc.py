"""Extract text from an image or scanned PDF with Anthropic vision."""

import base64
import io
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _image_payload(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    supported = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    if suffix in supported:
        return supported[suffix], base64.b64encode(path.read_bytes()).decode("ascii")

    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
    return "image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii")


def extract_image_text(path: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package not installed") from exc

    source = Path(path)
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    if source.suffix.lower() == ".pdf":
        content = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(source.read_bytes()).decode("ascii"),
            },
        }
    else:
        media_type, data = _image_payload(source)
        content = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                content,
                {"type": "text", "text": "Transcribe all visible document text accurately. Return only the transcription."},
            ],
        }],
    )
    return "\n".join(block.text for block in message.content if getattr(block, "type", "") == "text").strip()
