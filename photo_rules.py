"""Rule engine: classify each photo as KEEP_ICLOUD / ARCHIVE_GCP / DELETE_QUEUE."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

KEEP_ICLOUD = "KEEP_ICLOUD"
ARCHIVE_GCP = "ARCHIVE_GCP"
DELETE_QUEUE = "DELETE_QUEUE"

# Tune these after the first plan pass and re-run.
BLUR_THRESHOLD = 80.0       # Laplacian variance below this counts as blurry
DARK_THRESHOLD = 20.0       # mean luminance below this counts as black/blank
RECENT_KEEP_DAYS = 180      # last 6 months stays in iCloud regardless


@dataclass
class PhotoFacts:
    uuid: str
    filename: str
    path: str | None
    date: datetime | None
    favorite: bool
    hidden: bool
    screenshot: bool
    persons: list[str]
    labels: list[str]
    burst: bool
    burst_selected: bool
    size_bytes: int
    blur_score: float | None = None
    dark_score: float | None = None
    duplicate_group: str | None = None
    is_duplicate_representative: bool = False


@dataclass
class Decision:
    action: str
    reasons: list[str] = field(default_factory=list)


def classify(p: PhotoFacts, now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)

    # --- DELETE_QUEUE: junk filters (never delete favorites) ---
    if p.screenshot and not p.favorite:
        return Decision(DELETE_QUEUE, ["screenshot"])
    if p.blur_score is not None and 0 <= p.blur_score < BLUR_THRESHOLD and not p.favorite:
        return Decision(DELETE_QUEUE, [f"blurry(score={p.blur_score:.0f})"])
    if p.dark_score is not None and 0 <= p.dark_score < DARK_THRESHOLD and not p.favorite:
        return Decision(DELETE_QUEUE, [f"dark(mean={p.dark_score:.0f})"])
    if p.burst and not p.burst_selected and not p.favorite:
        return Decision(DELETE_QUEUE, ["burst_non_selected"])
    if p.duplicate_group and not p.is_duplicate_representative and not p.favorite:
        return Decision(DELETE_QUEUE, [f"near_duplicate_in_{p.duplicate_group}"])

    # --- KEEP_ICLOUD: priority signals ---
    if p.favorite:
        return Decision(KEEP_ICLOUD, ["favorite"])
    if p.persons:
        return Decision(KEEP_ICLOUD, [f"named_people:{','.join(p.persons[:3])}"])
    if p.date and (now - p.date).days < RECENT_KEEP_DAYS:
        return Decision(KEEP_ICLOUD, [f"recent_<{RECENT_KEEP_DAYS}d"])

    # --- ARCHIVE_GCP: everything else worth preserving ---
    reasons = ["no_named_people", f"older_than_{RECENT_KEEP_DAYS}d"]
    label_set = {l.lower() for l in p.labels}
    if label_set & {"document", "receipt", "screenshot"}:
        reasons.append("doc_like_label")
    return Decision(ARCHIVE_GCP, reasons)
