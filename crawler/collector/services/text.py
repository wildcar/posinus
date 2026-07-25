import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    port = parts.port
    netloc = host if not port or (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"\s+", " ", value).strip()


def content_hash(body: str) -> str:
    return hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    words = re.findall(r"\w+", normalized, flags=re.UNICODE)
    if len(words) >= 8:
        return [" ".join(words[i:i + 3]) for i in range(max(1, len(words) - 2))]
    return [normalized[i:i + 5] for i in range(max(1, len(normalized) - 4))]


def simhash64(text: str) -> int:
    vector = [0] * 64
    for token in _tokens(text):
        digest = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if digest & (1 << bit) else -1
    unsigned = sum(1 << bit for bit, value in enumerate(vector) if value >= 0)
    return unsigned if unsigned < 2**63 else unsigned - 2**64


def hamming_distance(left: int, right: int) -> int:
    return ((left & ((1 << 64) - 1)) ^ (right & ((1 << 64) - 1))).bit_count()


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()

