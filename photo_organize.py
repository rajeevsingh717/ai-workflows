"""CLI: rule-based photo organizer for Apple Photos.

Phase 1 — plan:    python3 photo_organize.py plan --out plan.json [--limit N] [--since YYYY-MM-DD]
Phase 2 — execute: python3 photo_organize.py execute plan.json [--dry-run] [--yes]

The execute phase uploads ARCHIVE_GCP photos to GCS, adds them to a
'Archived-to-GCP' album in Photos.app, and adds DELETE_QUEUE photos to a
'Plan-to-Delete' album. It never deletes anything from Photos itself —
the user reviews and removes from the album manually.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from photo_quality import (
    darkness_score,
    group_near_duplicates,
    laplacian_blur_score,
    perceptual_hash,
)
from photo_rules import (
    ARCHIVE_GCP,
    DELETE_QUEUE,
    KEEP_ICLOUD,
    Decision,
    PhotoFacts,
    classify,
)


def _preview_path(photo) -> str | None:
    """Apple's preview JPEG if available, else the original if it's a JPEG."""
    derivs = getattr(photo, "path_derivatives", None) or []
    for d in derivs:
        if d and d.lower().endswith((".jpg", ".jpeg")):
            return d
    if photo.path and photo.path.lower().endswith((".jpg", ".jpeg")):
        return photo.path
    return None


def gather_facts(photo) -> tuple[PhotoFacts, str | None]:
    preview = _preview_path(photo)
    blur = dark = None
    if preview and Path(preview).exists():
        try:
            blur = laplacian_blur_score(preview)
            dark = darkness_score(preview)
        except Exception:
            pass

    persons = [n for n in (photo.persons or []) if n and n != "_UNKNOWN_"]
    size = 0
    if photo.path:
        try:
            size = Path(photo.path).stat().st_size
        except OSError:
            pass

    facts = PhotoFacts(
        uuid=photo.uuid,
        filename=photo.original_filename or "",
        path=photo.path,
        date=photo.date,
        favorite=bool(photo.favorite),
        hidden=bool(photo.hidden),
        screenshot=bool(getattr(photo, "screenshot", False)),
        persons=persons,
        labels=list(photo.labels or []),
        burst=bool(getattr(photo, "burst", False)),
        burst_selected=bool(getattr(photo, "burst_selected", False)),
        size_bytes=size,
        blur_score=blur,
        dark_score=dark,
    )
    return facts, preview


def plan_cmd(args: argparse.Namespace) -> None:
    import osxphotos

    print(f"Opening Photos library: {args.library or '(system default)'}")
    db = osxphotos.PhotosDB(dbfile=args.library)
    photos = db.photos(images=True, movies=False)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        photos = [p for p in photos if p.date and p.date >= cutoff]
    if args.limit:
        photos = photos[: args.limit]
    print(f"Analyzing {len(photos)} photos...")

    skipped_missing = 0
    facts_list: list[PhotoFacts] = []
    hashes: dict[str, str] = {}
    time_buckets: dict[str, str] = {}

    for i, photo in enumerate(photos):
        if photo.ismissing:
            skipped_missing += 1
        facts, preview = gather_facts(photo)
        facts_list.append(facts)
        if preview and Path(preview).exists():
            try:
                hashes[facts.uuid] = perceptual_hash(preview)
            except Exception:
                pass
        if facts.date:
            time_buckets[facts.uuid] = facts.date.strftime("%Y%m%d%H")
        if (i + 1) % 500 == 0:
            print(f"  ...analyzed {i + 1}/{len(photos)}")

    print("Grouping near-duplicates...")
    group_of = group_near_duplicates(hashes, time_buckets, max_distance=5)
    group_counts = Counter(group_of.values())
    size_by_uuid = {f.uuid: f.size_bytes for f in facts_list}
    reps_by_group: dict[str, str] = {}
    for uuid, gid in group_of.items():
        cur = reps_by_group.get(gid)
        if cur is None or size_by_uuid.get(uuid, 0) > size_by_uuid.get(cur, 0):
            reps_by_group[gid] = uuid
    for f in facts_list:
        gid = group_of.get(f.uuid)
        if gid and group_counts[gid] > 1:
            f.duplicate_group = gid
            f.is_duplicate_representative = reps_by_group[gid] == f.uuid

    print("Classifying...")
    now = datetime.now(timezone.utc)
    counts: Counter[str] = Counter()
    actions: list[dict] = []
    for f in facts_list:
        d: Decision = classify(f, now=now)
        counts[d.action] += 1
        actions.append({
            "uuid": f.uuid,
            "filename": f.filename,
            "path": f.path,
            "date": f.date.isoformat() if f.date else None,
            "size_bytes": f.size_bytes,
            "favorite": f.favorite,
            "persons": f.persons,
            "labels": f.labels[:10],
            "blur_score": f.blur_score,
            "dark_score": f.dark_score,
            "duplicate_group": f.duplicate_group,
            "is_duplicate_representative": f.is_duplicate_representative,
            "action": d.action,
            "reasons": d.reasons,
        })

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library_path": args.library or "(system default)",
        "stats": {
            "total_scanned": len(facts_list),
            "skipped_missing_original": skipped_missing,
            "keep_icloud": counts[KEEP_ICLOUD],
            "archive_gcp": counts[ARCHIVE_GCP],
            "delete_queue": counts[DELETE_QUEUE],
        },
        "actions": actions,
    }
    Path(args.out).write_text(json.dumps(plan, indent=2, default=str))
    print(f"\nPlan written to {args.out}")
    print(f"  KEEP_ICLOUD:  {counts[KEEP_ICLOUD]}")
    print(f"  ARCHIVE_GCP:  {counts[ARCHIVE_GCP]}")
    print(f"  DELETE_QUEUE: {counts[DELETE_QUEUE]}")
    if skipped_missing:
        print(f"  SKIPPED (originals not downloaded from iCloud): {skipped_missing}")
    print("\nReview plan.json before running 'execute'.")


def download_cmd(args: argparse.Namespace) -> None:
    import osxphotos
    from osxphotos import PhotoExporter
    from osxphotos.exportoptions import ExportOptions

    print(f"Opening Photos library: {args.library or '(system default)'}")
    db = osxphotos.PhotosDB(dbfile=args.library)
    missing = [p for p in db.photos(images=True, movies=False) if p.ismissing]
    print(f"iCloud-only photos to download: {len(missing)}")
    if not missing:
        print("Nothing to download — all originals already local.")
        return

    ok = failed = 0
    with tempfile.TemporaryDirectory(prefix="photo_dl_") as tmp:
        for i, photo in enumerate(missing):
            exporter = PhotoExporter(photo)
            opts = ExportOptions(
                download_missing=True,
                use_photos_export=True,
                overwrite=True,
            )
            result = exporter.export(tmp, options=opts)
            if result.exported:
                ok += 1
            else:
                print(f"  ! could not download: {photo.original_filename}")
                failed += 1
            if (i + 1) % 10 == 0:
                print(f"  ...{i + 1}/{len(missing)}  ({ok} ok, {failed} failed)")

    print(f"\nDownloaded {ok}/{len(missing)} photos  ({failed} failed)")
    print("Now run: python3 photo_organize.py plan --out plan.json")


def _get_or_create_album(lib, name: str):
    for alb in lib.albums():
        if alb.name == name:
            return alb
    return lib.create_album(name)


def execute_cmd(args: argparse.Namespace) -> None:
    plan = json.loads(Path(args.plan).read_text())
    bucket_name = os.getenv("GCS_BUCKET")
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    archive_actions = [a for a in plan["actions"] if a["action"] == ARCHIVE_GCP]
    delete_actions = [a for a in plan["actions"] if a["action"] == DELETE_QUEUE]

    print("Plan summary:")
    print(f"  ARCHIVE_GCP:  {len(archive_actions)} → add to album 'Archived-to-GCP'")
    print(f"  DELETE_QUEUE: {len(delete_actions)} → add to album 'Plan-to-Delete'")
    print("  Nothing is deleted. Review both albums in Photos.app, then act manually.\n")

    if args.dry_run:
        print("(dry-run, exiting without action)")
        return

    if not args.yes:
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    try:
        from photoscript import PhotosLibrary
    except ImportError:
        sys.exit("photoscript not installed — run: pip install photoscript")

    lib = PhotosLibrary()

    if archive_actions:
        archive_uuids = [a["uuid"] for a in archive_actions]
        album = _get_or_create_album(lib, "Archived-to-GCP")
        photos = list(lib.photos(uuid=archive_uuids))
        if photos:
            album.add(photos)
        print(f"Added {len(photos)}/{len(archive_actions)} to album 'Archived-to-GCP'")
        if len(photos) < len(archive_actions):
            print(f"  ({len(archive_actions) - len(photos)} iCloud-only photos not yet downloaded — run 'download' first)")

    if delete_actions:
        delete_uuids = [a["uuid"] for a in delete_actions]
        album = _get_or_create_album(lib, "Plan-to-Delete")
        photos = list(lib.photos(uuid=delete_uuids))
        if photos:
            album.add(photos)
        print(f"Added {len(photos)}/{len(delete_actions)} to album 'Plan-to-Delete'")

    print("\nDone. Review both albums in Photos.app:")
    print("  'Archived-to-GCP'  → export & upload to GCS, then delete the album")
    print("  'Plan-to-Delete'   → review and delete (removes from iCloud too)")


VISION_LABELS = [
    "selfie", "people", "group", "landscape", "nature",
    "food", "object", "document", "animal", "vehicle", "building", "other",
]
VISION_PROMPT = """\
Analyze this photo and return ONLY a valid JSON object with exactly these fields:
{
  "label": "<one of: selfie, people, group, landscape, nature, food, object, document, animal, vehicle, building, other>",
  "persons": ["<describe each visible person, e.g. 'woman in blue dress', 'young boy'. Empty list if no people.>"],
  "description": "<1-2 sentences describing what is in the photo>",
  "mood": "<one of: happy, candid, formal, scenic, serious, fun, romantic, sad, other>",
  "setting": "<one of: indoor, outdoor, beach, restaurant, home, office, nature, street, other>",
  "quality": "<one of: sharp, blurry, dark, overexposed, good>",
  "event_type": "<one of: birthday, wedding, vacation, everyday, holiday, sports, graduation, other>",
  "objects": ["<list main objects visible, e.g. 'dog', 'car', 'birthday cake'>"],
  "text_content": "<transcribe ALL visible text exactly as it appears — signs, labels, documents, receipts, screens, captions, watermarks. Empty string if none.>"
}
Return only the JSON. No explanation, no markdown, no code block."""


def _jpeg_preview(photo) -> str | None:
    derivs = getattr(photo, "path_derivatives", None) or []
    jpegs = [d for d in derivs if d and d.lower().endswith((".jpg", ".jpeg")) and Path(d).exists()]
    if jpegs:
        return max(jpegs, key=lambda d: Path(d).stat().st_size)
    if photo.path and photo.path.lower().endswith((".jpg", ".jpeg")) and Path(photo.path).exists():
        return photo.path
    return None


def _vision_analyze(preview_path: str, model: str, persons_from_photos: list[str]) -> dict:
    import base64
    import ollama
    with open(preview_path, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    resp = ollama.generate(model=model, prompt=VISION_PROMPT, images=[img], stream=False)
    raw = resp["response"].strip()
    # strip markdown code fences if model wraps output
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    # normalize label
    label = str(data.get("label", "other")).lower().strip().rstrip(".,;:")
    if label not in VISION_LABELS:
        label = "other"
    # merge named persons from Photos.app (osxphotos) with Ollama descriptions
    named = [n for n in persons_from_photos if n and n != "_UNKNOWN_"]
    described = data.get("persons") or []
    persons_merged = named + [d for d in described if d not in named]
    # support both old field name and new
    text = str(data.get("text_content") or data.get("text_in_image") or "")
    return {
        "label": label,
        "persons": persons_merged,
        "description": str(data.get("description", "")),
        "mood": str(data.get("mood", "other")),
        "setting": str(data.get("setting", "other")),
        "quality": str(data.get("quality", "good")),
        "event_type": str(data.get("event_type", "everyday")),
        "objects": list(data.get("objects") or []),
        "text_content": text,
    }


def vision_cmd(args: argparse.Namespace) -> None:
    import osxphotos

    out_path = Path(args.out)
    existing: dict[str, dict] = {}
    if out_path.exists() and out_path.stat().st_size > 0:
        raw = json.loads(out_path.read_text())
        # handle old format (uuid → string label) — upgrade transparently
        existing = {
            k: v if isinstance(v, dict) else {"label": v}
            for k, v in raw.items()
        }
        print(f"Resuming — {len(existing)} photos in catalog.")

    print(f"Opening Photos library: {args.library or '(system default)'}")
    db = osxphotos.PhotosDB(dbfile=args.library)
    photos = db.photos(images=True, movies=False)
    if args.limit:
        photos = photos[: args.limit]

    # update status for all existing entries — mark deleted ones without removing them
    library_uuids = {p.uuid for p in db.photos(images=True, movies=False)}
    newly_deleted = 0
    for uuid, entry in existing.items():
        if isinstance(entry, dict):
            was_active = entry.get("status", "active") == "active"
            is_active = uuid in library_uuids
            if was_active and not is_active:
                entry["status"] = "deleted"
                newly_deleted += 1
            elif is_active:
                entry["status"] = "active"
    if newly_deleted:
        print(f"Marked {newly_deleted} entries as deleted (photos removed from library)")

    to_classify = [p for p in photos if p.uuid not in existing and not p.ismissing]
    skipped_missing = sum(1 for p in photos if p.uuid not in existing and p.ismissing)
    print(f"To classify: {len(to_classify)}  |  already done: {len(existing)}  |  iCloud-only (skip): {skipped_missing}")

    ok = failed = 0
    for i, photo in enumerate(to_classify):
        preview = _jpeg_preview(photo)
        named_persons = [n for n in (photo.persons or []) if n and n != "_UNKNOWN_"]
        meta = {
            "filename": photo.original_filename or "",
            "date": photo.date.isoformat() if photo.date else None,
            "status": "active",
            "media_type": "photo",
            "photo_owner": args.owner,
        }
        if not preview:
            existing[photo.uuid] = {**meta, "label": "other", "persons": named_persons}
            failed += 1
        else:
            try:
                data = _vision_analyze(preview, args.model, named_persons)
                existing[photo.uuid] = {**meta, **data}
                ok += 1
            except Exception as e:
                print(f"  ! {photo.original_filename}: {e}")
                existing[photo.uuid] = {**meta, "label": "other", "persons": named_persons}
                failed += 1
        if (i + 1) % 10 == 0:
            out_path.write_text(json.dumps(existing, indent=2))
            print(f"  ...{i + 1}/{len(to_classify)}  ({ok} ok, {failed} failed)")

    out_path.write_text(json.dumps(existing, indent=2))
    from collections import Counter
    counts = Counter(v.get("label", "other") for v in existing.values())
    print(f"\nDone. Data saved to {out_path}")
    for label, count in counts.most_common():
        print(f"  {label:15s} {count}")
    print(f"\nNow run: python3 photo_organize.py vision-albums {out_path}")


def vision_albums_cmd(args: argparse.Namespace) -> None:
    raw: dict = json.loads(Path(args.labels).read_text())

    try:
        from photoscript import PhotosLibrary
    except ImportError:
        sys.exit("photoscript not installed — run: pip install photoscript")

    from collections import defaultdict
    by_label: dict[str, list[str]] = defaultdict(list)
    for uuid, val in raw.items():
        if isinstance(val, dict) and val.get("status") == "deleted":
            continue
        label = val.get("label", val) if isinstance(val, dict) else val
        by_label[str(label)].append(uuid)

    import time
    from photoscript import Photo
    lib = PhotosLibrary()
    batch_size = 20
    print(f"Creating albums for {len(by_label)} labels...")
    for label, uuids in sorted(by_label.items()):
        album_name = f"Vision-{label.capitalize()}"
        album = _get_or_create_album(lib, album_name)
        photos = []
        for uuid in uuids:
            try:
                photos.append(Photo(uuid))
            except Exception:
                pass
        added = 0
        for i in range(0, len(photos), batch_size):
            batch = photos[i: i + batch_size]
            album.add(batch)
            added += len(batch)
            time.sleep(1)
        print(f"  {album_name:25s} {added}/{len(uuids)} photos added")

    print("\nDone. Open Photos.app to browse albums by Vision-* label.")


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".3gp", ".mkv"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".bmp"}


def _extract_video_frames(video_path: str, tmp_dir: Path, n_frames: int = 3) -> list[str]:
    import cv2
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total == 0:
        cap.release()
        return []
    positions = [int(total * p) for p in [0.1, 0.5, 0.9]][:n_frames]
    frames = []
    for idx, pos in enumerate(positions):
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            out = tmp_dir / f"frame_{idx}.jpg"
            cv2.imwrite(str(out), frame)
            frames.append(str(out))
    cap.release()
    return frames


def _vision_analyze_video(frames: list[str], model: str) -> dict:
    from collections import Counter
    results = []
    for frame in frames:
        try:
            results.append(_vision_analyze(frame, model, []))
        except Exception:
            pass
    if not results:
        return {"label": "other", "description": "", "persons": [], "objects": [],
                "mood": "other", "setting": "other", "quality": "good",
                "event_type": "everyday", "text_in_image": ""}
    label = Counter(r["label"] for r in results).most_common(1)[0][0]
    descriptions = [r["description"] for r in results if r.get("description")]
    persons = list({p for r in results for p in r.get("persons", [])})
    objects = list({o for r in results for o in r.get("objects", [])})
    return {
        "label": label,
        "description": descriptions[0] if descriptions else "",
        "persons": persons,
        "mood": results[0].get("mood", "other"),
        "setting": results[0].get("setting", "other"),
        "quality": results[0].get("quality", "good"),
        "event_type": Counter(r.get("event_type", "everyday") for r in results).most_common(1)[0][0],
        "objects": objects,
        "text_in_image": " ".join(r.get("text_in_image", "") for r in results if r.get("text_in_image")).strip(),
    }


def _get_exif_date(path: str) -> str | None:
    try:
        from PIL import Image, ExifTags
        img = Image.open(path)
        exif = img._getexif()
        if exif:
            for tag, val in exif.items():
                if ExifTags.TAGS.get(tag) in ("DateTimeOriginal", "DateTime"):
                    return datetime.strptime(val, "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat()
    except Exception:
        return None


def _to_jpeg(src: Path, tmp_dir: Path) -> str:
    if src.suffix.lower() in (".jpg", ".jpeg"):
        return str(src)
    out = tmp_dir / (src.stem + ".jpg")
    subprocess.run(
        ["sips", "-s", "format", "jpeg", str(src), "--out", str(out)],
        check=True, capture_output=True,
    )
    return str(out)


def vision_folder_cmd(args: argparse.Namespace) -> None:
    import hashlib

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a directory: {folder}")

    all_extensions = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
    media_files = sorted(f for f in folder.rglob("*") if f.suffix.lower() in all_extensions)
    photos = [f for f in media_files if f.suffix.lower() in PHOTO_EXTENSIONS]
    videos = [f for f in media_files if f.suffix.lower() in VIDEO_EXTENSIONS]
    print(f"Found {len(photos)} photos and {len(videos)} videos in {folder}")

    # auto-name output file based on owner if user didn't override the default
    out_file = args.out
    if args.owner and out_file == "vision_labels.json":
        out_file = f"{args.owner}_labels.json"
    out_path = Path(out_file)
    existing: dict[str, dict] = {}
    if out_path.exists() and out_path.stat().st_size > 0:
        raw = json.loads(out_path.read_text())
        existing = {k: v if isinstance(v, dict) else {"label": v} for k, v in raw.items()}
        print(f"Catalog has {len(existing)} existing entries.")

    # build set of already-processed hashes to skip duplicates
    processed_hashes = {
        k.replace("folder:", "") for k in existing if k.startswith("folder:")
    }

    to_process = []
    for f in media_files:
        with open(f, "rb") as fh:
            file_hash = hashlib.md5(fh.read()).hexdigest()
        if file_hash not in processed_hashes:
            to_process.append((f, file_hash))

    print(f"To classify: {len(to_process)}  |  already done: {len(processed_hashes)}")
    if not to_process:
        print("Nothing new to classify.")
        return

    ok = failed = 0
    with tempfile.TemporaryDirectory(prefix="vision_folder_") as tmp:
        tmp_dir = Path(tmp)
        for i, (media_path, file_hash) in enumerate(to_process):
            key = f"folder:{file_hash}"
            is_video = media_path.suffix.lower() in VIDEO_EXTENSIONS
            meta = {
                "filename": media_path.name,
                "source_path": str(media_path),
                "date": _get_exif_date(str(media_path)),
                "status": "active",
                "source": "folder",
                "media_type": "video" if is_video else "photo",
                "photo_owner": args.owner,
            }
            try:
                if is_video:
                    frames = _extract_video_frames(str(media_path), tmp_dir)
                    if not frames:
                        raise ValueError("no frames extracted")
                    data = _vision_analyze_video(frames, args.model)
                else:
                    jpeg = _to_jpeg(media_path, tmp_dir)
                    data = _vision_analyze(jpeg, args.model, [])
                existing[key] = {**meta, **data}
                ok += 1
            except Exception as e:
                print(f"  ! {media_path.name}: {e}")
                existing[key] = {**meta, "label": "other"}
                failed += 1

            if (i + 1) % 10 == 0:
                out_path.write_text(json.dumps(existing, indent=2))
                print(f"  ...{i + 1}/{len(to_process)}  ({ok} ok, {failed} failed)")

    out_path.write_text(json.dumps(existing, indent=2))
    from collections import Counter
    folder_entries = {k: v for k, v in existing.items() if k.startswith("folder:")}
    counts = Counter(v.get("label", "other") for v in folder_entries.values())
    print(f"\nDone. {ok} classified, {failed} failed.")
    for label, count in counts.most_common():
        print(f"  {label:15s} {count}")
    print(f"\nNow run: python3 photo_organize.py backup {out_path}")


def backup_cmd(args: argparse.Namespace) -> None:
    import osxphotos
    from google.cloud import storage
    from google.oauth2 import service_account

    bucket_name = os.getenv("GCS_BUCKET")
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not bucket_name:
        sys.exit("GCS_BUCKET not set in .env")

    if sa_path:
        creds = service_account.Credentials.from_service_account_file(sa_path)
        client = storage.Client(credentials=creds, project=creds.project_id)
    else:
        client = storage.Client()
    bucket = client.bucket(bucket_name)

    labels: dict = json.loads(Path(args.labels).read_text())
    db = osxphotos.PhotosDB()
    photo_map = {p.uuid: p for p in db.photos(images=True, movies=False)}

    uploaded = skipped_exists = skipped_missing = failed = 0
    total = len(labels)
    print(f"Backing up {total} photos to gs://{bucket_name}/...")

    for i, (uuid, entry) in enumerate(labels.items()):
        if isinstance(entry, dict) and entry.get("status") == "deleted":
            continue
        label = entry.get("label", "other") if isinstance(entry, dict) else entry
        folder = f"Vision-{label.capitalize()}"

        # folder-sourced entries use source_path directly
        if uuid.startswith("folder:"):
            src_path = entry.get("source_path") if isinstance(entry, dict) else None
            if not src_path or not Path(src_path).exists():
                skipped_missing += 1
                continue
            photo_path = src_path
            filename = Path(src_path).name
        else:
            photo = photo_map.get(uuid)
            if not photo or not photo.path or not Path(photo.path).exists():
                skipped_missing += 1
                continue
            photo_path = photo.path
            filename = Path(photo.path).name

        prefix = (args.prefix.rstrip("/") + "/") if args.prefix else ""
        blob_name = f"{prefix}{folder}/{filename}"
        blob = bucket.blob(blob_name)

        if blob.exists():
            skipped_exists += 1
        else:
            try:
                blob.upload_from_filename(photo_path)
                uploaded += 1
            except Exception as e:
                print(f"  ! upload failed {filename}: {e}")
                failed += 1

        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{total}  uploaded={uploaded} skipped={skipped_exists} missing={skipped_missing} failed={failed}")

    print(f"\nDone.")
    print(f"  Uploaded:        {uploaded}")
    print(f"  Already in GCS:  {skipped_exists}")
    print(f"  Missing locally: {skipped_missing}")
    print(f"  Failed:          {failed}")

    # upload catalog JSON with today's date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stem = Path(args.labels).stem
    prefix = (args.prefix.rstrip("/") + "/") if args.prefix else ""
    catalog_blob_name = f"{prefix}metadata/catalog/{stem}_{today}.json"
    blob = bucket.blob(catalog_blob_name)
    blob.upload_from_filename(str(Path(args.labels).resolve()))
    print(f"\n  Catalog saved → gs://{bucket_name}/{catalog_blob_name}")

    print(f"\nBucket: gs://{bucket_name}/")
    for label in sorted({(e.get('label','other') if isinstance(e,dict) else e) for e in labels.values()}):
        print(f"  gs://{bucket_name}/Vision-{label.capitalize()}/")
    print(f"  gs://{bucket_name}/metadata/catalog/")


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Rule-based photo organizer for Apple Photos.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="Scan library, apply rules, emit JSON plan")
    pp.add_argument("--out", default="plan.json")
    pp.add_argument("--library", default=None, help="Photos library path (default: system library)")
    pp.add_argument("--since", default=None, help="Only consider photos on/after ISO date")
    pp.add_argument("--limit", type=int, default=None, help="Limit to first N (for testing)")
    pp.set_defaults(func=plan_cmd)

    pd = sub.add_parser("download", help="Download all iCloud-only originals to this Mac")
    pd.add_argument("--library", default=None, help="Photos library path (default: system library)")
    pd.set_defaults(func=download_cmd)

    pv = sub.add_parser("vision", help="Classify photos with Ollama vision model, save labels")
    pv.add_argument("--out", default="rajeev_vision_labels.json", help="Output JSON file for labels")
    pv.add_argument("--library", default=None, help="Photos library path (default: system library)")
    pv.add_argument("--model", default="llama3.2-vision", help="Ollama model to use")
    pv.add_argument("--limit", type=int, default=None, help="Limit to first N (for testing)")
    pv.add_argument("--owner", default="", help="Photo owner name e.g. 'rajeev' or 'anu'")
    pv.set_defaults(func=vision_cmd)

    pva = sub.add_parser("vision-albums", help="Create Photos.app albums from vision labels")
    pva.add_argument("labels", help="rajeev_vision_labels.json path")
    pva.set_defaults(func=vision_albums_cmd)

    pvf = sub.add_parser("vision-folder", help="Classify photos from a local folder (no Photos.app needed)")
    pvf.add_argument("folder", help="Folder containing photos (searched recursively)")
    pvf.add_argument("--out", default="rajeev_vision_labels.json", help="Catalog JSON file to append to")
    pvf.add_argument("--model", default="llama3.2-vision", help="Ollama model to use")
    pvf.add_argument("--owner", default="", help="Photo owner name e.g. 'rajeev' or 'anu'")
    pvf.set_defaults(func=vision_folder_cmd)

    pb = sub.add_parser("backup", help="Upload photos to GCS bucket organized by vision label")
    pb.add_argument("labels", help="rajeev_vision_labels.json path")
    pb.add_argument("--prefix", default="", help="GCS folder prefix e.g. 'rajeev' or 'anu'")
    pb.set_defaults(func=backup_cmd)

    pe = sub.add_parser("execute", help="Create albums in Photos.app from the plan")
    pe.add_argument("plan", help="Plan JSON path")
    pe.add_argument("--dry-run", action="store_true")
    pe.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    pe.set_defaults(func=execute_cmd)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
