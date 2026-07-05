"""Photo quality scoring: blur, darkness, perceptual hash, near-duplicate grouping."""
import math
from pathlib import Path


def laplacian_blur_score(path: str) -> float:
    """Higher = sharper. < 50 is noticeably blurry."""
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def darkness_score(path: str) -> float:
    """Mean brightness 0-255. < 40 is very dark."""
    import cv2
    img = cv2.imread(path)
    if img is None:
        return 128.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return float(hsv[:, :, 2].mean())


def perceptual_hash(path: str, hash_size: int = 8) -> str:
    """Returns a hex perceptual hash string for the image."""
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return "0" * (hash_size * hash_size // 4)
    img = cv2.resize(img, (hash_size + 1, hash_size))
    diff = img[:, 1:] > img[:, :-1]
    bits = diff.flatten()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    hex_len = hash_size * hash_size // 4
    return format(val, f"0{hex_len}x")


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def group_near_duplicates(
    hashes: dict[str, str],
    time_buckets: dict[str, str],
    max_distance: int = 5,
) -> dict[str, str]:
    """
    Group UUIDs whose perceptual hashes are within max_distance AND taken in
    the same hour. Returns {uuid: group_id} where group_id is the first UUID
    seen in the group.
    """
    uuids = list(hashes.keys())
    group_of: dict[str, str] = {}

    for i, u in enumerate(uuids):
        if u in group_of:
            continue
        group_of[u] = u
        for v in uuids[i + 1:]:
            if v in group_of:
                continue
            if time_buckets.get(u) != time_buckets.get(v):
                continue
            if _hamming(hashes[u], hashes[v]) <= max_distance:
                group_of[v] = u

    return group_of
