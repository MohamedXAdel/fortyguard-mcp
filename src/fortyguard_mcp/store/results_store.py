"""
Result storage - a paid archive that doubles as the cache.

Two facts settled the design:

  * Results cost real money, so nothing is ever evicted by default. That is the
    user's trade to make, not ours.
  * Results are deterministic for HISTORICAL dates - an identical request
    re-issued days later returned byte-identical values across 112 tiles - so a
    stored result is a cache hit. Not verified for future dates or for
    `streetview`, which carries no date; `runtime.py` says which case applies.

Hence a durable data directory rather than a cache directory, which the OS
reclaims under disk pressure.

Entries are scoped to the API key and base URL that paid for them, so several
keys can share one directory without serving each other's results.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from ..config import Settings, get_settings

# Anything that looks like a pre-signed URL: a credential with an expiry, which
# must never land in a durable archive. `client/download.py` saves the linked
# file first, so this is the backstop.
#
# The signature must be a WHOLE query parameter, or ordinary links get mangled
# (`?case=1` matching on `se=`). Public because the log filter uses the same
# pattern; two copies would drift, and the drifted one stops redacting.
SIGNED_URL_RE = re.compile(
    # `(?:https?:)?//` - a protocol-relative link is still a credential, and
    # requiring the scheme let one through both this and the LINK_KEYS check.
    r"(?:https?:)?//[^\s\"']*[?&](?:"
    r"X-Goog-Signature|X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|"
    r"AWSAccessKeyId|Signature|sig|se"
    r")=[^\s\"']*",
    re.IGNORECASE,
)
# Keys whose value is a downloadable artifact behind a signed URL. Shared with
# `client/download.py`, which must fetch exactly what this module redacts.
LINK_KEYS = {"download_link", "downloadLink", "signed_url", "signedUrl"}

# Below this length, substring redaction destroys more than it protects: a real
# key is 32 chars, while a 3-char one turns `"check_status"` into
# `"chec[REDACTED]_status"`. Shared with `logging_setup`.
MIN_SCRUBBABLE_KEY = 8


def is_link_value(v: Any) -> bool:
    """A LINK_KEYS value that names a remote location. Accepts `//host/...`."""
    return isinstance(v, str) and v.lstrip().startswith(("http://", "https://", "//"))


def _scrubbable(api_key: str | None) -> str | None:
    """The key if it is long enough to redact safely, otherwise None."""
    return api_key if api_key and len(api_key) >= MIN_SCRUBBABLE_KEY else None


@dataclass(slots=True)
class StoredResult:
    activity_id: str
    endpoint: str
    request_body: Any
    request_hash: str
    stored_at: str
    size_bytes: int
    redacted: bool = False
    # How many NaN/Infinity values were nulled on the way in. Recorded so the
    # archive never quietly differs from what the API sent.
    non_finite_nulled: int = 0
    # Which key and base URL paid for this. See `ResultStore._scope`. None
    # means it was written before scopes were recorded - see `get()`.
    scope: str | None = None
    _path: Path | None = field(default=None, repr=False)

    def load(self) -> Any:
        """
        Parse the payload. Kept lazy so metadata scans stay cheap.

        This does NOT stream and cannot without a third-party incremental
        parser - CPython's `json.load(fp)` is `loads(fp.read())`, so a 14 MB
        payload costs roughly 50-60 MB while parsing. Acceptable because parsing
        is only needed for the tiles, which requires the whole structure anyway;
        callers passing the payload onwards should use `open_bytes`/`iter_bytes`.

        Unreadable counts as absent: every caller already branches on `None`,
        and a corrupt payload is as useful as a missing one.
        """
        if self._path is None or not self._path.exists():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @contextmanager
    def open_bytes(self) -> Iterator[BinaryIO]:
        """
        Stream the stored payload as raw bytes, never parsing it.

        The true zero-copy path: serving the untouched payload needs no Python
        objects, so a 14 MB result costs a buffer rather than tens of megabytes
        of dicts.
        """
        if self._path is None or not self._path.exists():
            raise FileNotFoundError(f"no stored payload for {self.activity_id}")
        with self._path.open("rb") as fh:
            yield fh

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Chunked byte iterator over the stored payload. Never parses."""
        with self.open_bytes() as fh:
            while chunk := fh.read(chunk_size):
                yield chunk


@dataclass(slots=True)
class StorageInfo:
    path: str
    result_count: int
    total_bytes: int
    # A COUNT per endpoint, not a credit total: per-call cost varies by plan,
    # and this package does not bake in one account's price list - so the honest
    # report is what is here, letting the caller apply their own rate.
    results_by_endpoint: dict[str, int]
    reports_path: str
    report_count: int
    report_bytes: int
    oldest: str | None
    newest: str | None
    max_storage_bytes: int | None
    over_cap: bool


# --------------------------------------------------------------------------- #
# Canonical request hashing
# --------------------------------------------------------------------------- #

def canonical_request_hash(endpoint: str, body: Any, *,
                           scope: str | None = None) -> str:
    """
    Stable key for an identical request, within one account and environment.

    Deliberately CONSERVATIVE: only key ordering and float noise are normalised,
    never ring rotation or winding. Under-normalising costs a cache miss;
    over-normalising collides two requests and returns the WRONG DATA.

    `scope` covers who asked and where - see `ResultStore._scope`. It is a
    digest, never the key: this value lands in on-disk metadata.
    """
    payload = {
        "endpoint": endpoint.strip("/"),
        "scope": scope or "",
        "body": _canonical(body),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _canonical(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            # `int(nan)` raises, and this runs in `find_by_request` before
            # every submit, so one NaN coordinate would take the tool down.
            # Hashing by name is stable and cannot collide with a real number.
            return f"__nonfinite__:{obj!r}"
        # 9dp is ~0.1 mm: kills float-repr noise, far too fine to merge two
        # genuinely different AOIs.
        r = round(obj, 9)
        return int(r) if r == int(r) else r
    return obj


# --------------------------------------------------------------------------- #
# Non-finite sanitising
# --------------------------------------------------------------------------- #

def replace_non_finite(obj: Any, _depth: int = 0) -> tuple[Any, int]:
    """
    Replace NaN/Infinity with None, and count how many. Reports, never hides.

    `json.dumps` emits bare `NaN` by default, which no strict parser accepts -
    but Python's own does, so it round-trips here and breaks elsewhere.
    """
    if _depth > 64:
        return obj, 0
    if isinstance(obj, float) and not math.isfinite(obj):
        return None, 1
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        total = 0
        for k, v in obj.items():
            out[k], n = replace_non_finite(v, _depth + 1)
            total += n
        return out, total
    if isinstance(obj, list):
        items = [replace_non_finite(v, _depth + 1) for v in obj]
        return [v for v, _ in items], sum(n for _, n in items)
    return obj, 0


# --------------------------------------------------------------------------- #
# Redaction backstop
# --------------------------------------------------------------------------- #

def _needs_scrub(obj: Any, api_key: str | None) -> bool:
    """
    Does anything here need redacting? Walks without allocating: almost every
    payload is clean, and copying a 14 MB result to find that out is waste.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in LINK_KEYS and is_link_value(v):
                return True
            if _needs_scrub(v, api_key):
                return True
        return False
    if isinstance(obj, list):
        return any(_needs_scrub(v, api_key) for v in obj)
    if isinstance(obj, str):
        if api_key and api_key in obj:
            return True
        return bool(SIGNED_URL_RE.search(obj))
    return False


def scrub_for_storage(obj: Any, api_key: str | None) -> tuple[Any, bool]:
    """
    Remove credentials before anything is written. Returns (clean, changed),
    with the ORIGINAL object returned untouched when nothing needs redacting.

    A key shorter than `MIN_SCRUBBABLE_KEY` is treated as absent; signed-URL
    redaction still runs, since it matches a pattern rather than a substring.
    """
    api_key = _scrubbable(api_key)
    if not _needs_scrub(obj, api_key):
        return obj, False

    changed = False

    def walk(o: Any) -> Any:
        nonlocal changed
        if isinstance(o, dict):
            out: dict[str, Any] = {}
            for k, v in o.items():
                if k in LINK_KEYS and is_link_value(v):
                    changed = True
                    out[k] = "<REDACTED_SIGNED_URL: expired credential, not archived>"
                else:
                    out[k] = walk(v)
            return out
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str):
            if api_key and api_key in o:
                changed = True
                o = o.replace(api_key, "<REDACTED_API_KEY>")
            if SIGNED_URL_RE.search(o):
                changed = True
                o = SIGNED_URL_RE.sub("<REDACTED_SIGNED_URL>", o)
            return o
        return o

    return walk(obj), changed


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

class ResultStore:
    """
    Files on disk, keyed two ways:

        results/<activity_id>.json        the payload
        results/<activity_id>.meta.json   endpoint, request, size, hash
        reports/<activity_id>.pdf         files fetched from a signed URL
        index/<request_hash>              -> activity_id   (cache lookup)

    Separate index files rather than one manifest: no rewrite-on-every-put, and
    no lock needed for concurrent writers.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.results_dir
        self.index_dir = self.root.parent / "index"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        # Running total so the cap check does not rescan the directory per put.
        self._cached_total: int | None = None

    # -- paths -------------------------------------------------------------- #

    def path_for(self, activity_id: str) -> Path:
        return self.root / f"{safe_filename(activity_id)}.json"

    def _meta_path(self, activity_id: str) -> Path:
        return self.root / f"{safe_filename(activity_id)}.meta.json"

    def _index_path(self, request_hash: str) -> Path:
        return self.index_dir / safe_filename(request_hash)

    def report_path_for(self, activity_id: str, suffix: str = ".pdf") -> Path:
        """
        Where a downloaded report file for this activity lives.

        By convention rather than a field in the sidecar: a recorded path is a
        second source of truth that can disagree with the filesystem. Deriving
        it makes `has_report()` a question about the disk, which is the only
        thing that decides whether the caller can open the file.
        """
        return self.settings.reports_dir / f"{safe_filename(activity_id)}{suffix}"

    def has_report(self, activity_id: str, suffix: str = ".pdf") -> bool:
        return self.report_path_for(activity_id, suffix).exists()

    def _scope(self) -> str:
        """
        Who asked, and where. A DIGEST - never the key itself, because this
        value ends up in filenames and in metadata on disk.

        Both halves matter: entitlements differ per key, so two keys sharing a
        data directory must not serve each other's results; and a staging
        base_url must not answer with production data marked authoritative.

        Enforced on BOTH lookup routes - the request hash AND `get()`. Mixing
        it into the hash alone left "give me activity_id X" open.
        """
        key = self.settings.key
        material = f"{self.settings.base_url}|{key}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    # -- writing ------------------------------------------------------------ #

    def put(
        self,
        activity_id: str,
        endpoint: str,
        request_body: Any,
        result: Any,
    ) -> StoredResult | None:
        """
        Persist a result. Returns None if a storage cap is set and reached -
        not data loss, since the caller still has the result from memory; it
        only means an identical request later costs credits again.

        Two transforms happen here, both recorded rather than silent:
        credentials are redacted, and non-finite numbers are nulled.
        """
        clean, redacted = scrub_for_storage(result, self.settings.key or None)
        clean, nulled = replace_non_finite(clean)
        payload = self.path_for(activity_id)

        # Short-circuits when no cap is configured (the default), so the common
        # path never touches the filesystem to check.
        cap = self.settings.max_storage_bytes
        if cap is not None and self.total_bytes(refresh=False) >= cap:
            return None

        # Straight to a temp file rather than building one large string, then
        # moved into place so a reader never sees a half-written payload.
        _atomic_write_json(payload, clean)

        written = payload.stat().st_size
        # Only maintained when a cap is configured; nothing reads it otherwise.
        if cap is not None:
            if self._cached_total is None:
                self.total_bytes()          # first write: establish the baseline
            else:
                self._cached_total += written

        scope = self._scope()
        req_hash = canonical_request_hash(endpoint, request_body, scope=scope)
        stored = StoredResult(
            activity_id=activity_id,
            endpoint=endpoint,
            request_body=request_body,
            request_hash=req_hash,
            stored_at=datetime.now(UTC).isoformat(),
            size_bytes=payload.stat().st_size,
            redacted=redacted,
            non_finite_nulled=nulled,
            scope=scope,
            _path=payload,
        )
        _atomic_write_json(self._meta_path(activity_id), {
            "activity_id": stored.activity_id,
            "endpoint": stored.endpoint,
            "request_body": stored.request_body,
            "request_hash": stored.request_hash,
            "stored_at": stored.stored_at,
            "size_bytes": stored.size_bytes,
            "redacted": stored.redacted,
            "non_finite_nulled": stored.non_finite_nulled,
            "scope": stored.scope,
        })
        self._index_path(req_hash).write_text(activity_id, encoding="utf-8")
        return stored

    # -- reading ------------------------------------------------------------ #

    def get(self, activity_id: str, *, any_scope: bool = False) -> StoredResult | None:
        """
        The metadata for one stored result, or None if it cannot be read.

        Unreadable counts as absent; so does another key's result - see
        `_scope`. `any_scope=True` is for callers describing the directory
        rather than reading data out of it, like `info()`.

        A sidecar with no `scope` predates the field and is ADOPTED rather than
        orphaned: those results were paid for, and refusing them would be a
        silent, expensive regression at upgrade time.
        """
        meta = self._meta_path(activity_id)
        if not meta.exists():
            return None
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        try:
            got = StoredResult(
                activity_id=str(d["activity_id"]), endpoint=str(d["endpoint"]),
                request_body=d.get("request_body"),
                request_hash=str(d["request_hash"]),
                stored_at=str(d["stored_at"]), size_bytes=int(d["size_bytes"]),
                redacted=bool(d.get("redacted", False)),
                non_finite_nulled=int(d.get("non_finite_nulled", 0) or 0),
                scope=(str(d["scope"]) if d.get("scope") else None),
                _path=self.path_for(activity_id),
            )
        except (KeyError, TypeError, ValueError):
            return None

        if any_scope or got.scope is None or got.scope == self._scope():
            return got
        return None

    def find_by_request(self, endpoint: str, request_body: Any) -> StoredResult | None:
        """
        Cache lookup. Sound because results are deterministic - verified, not
        assumed.
        """
        idx = self._index_path(
            canonical_request_hash(endpoint, request_body, scope=self._scope()))
        if not idx.exists():
            return None
        activity_id = idx.read_text(encoding="utf-8").strip()
        stored = self.get(activity_id)
        # A dangling index entry (payload deleted by hand) is a miss, not an error.
        if stored is None or not self.path_for(activity_id).exists():
            return None
        return stored

    # -- introspection ------------------------------------------------------ #

    def iter_stored(self) -> Iterator[StoredResult]:
        # Every scope. This drives `info()`, which describes the DIRECTORY -
        # disk belongs to the OS user, not to one API key, and a user managing
        # space needs the real total rather than their current key's share. It
        # exposes counts and endpoints, never payloads.
        for meta in self.root.glob("*.meta.json"):
            got = self.get(meta.name[: -len(".meta.json")], any_scope=True)
            if got is not None:
                yield got

    def total_bytes(self, *, refresh: bool = True) -> int:
        """
        Payload bytes on disk. `.meta.json` sidecars are excluded.

        `refresh=False` uses a running total kept current by `put`, so the cap
        check stays O(1). Per-process, so a second writer makes it stale -
        acceptable, since the cap is advisory and `info()` recomputes.
        """
        if not refresh and self._cached_total is not None:
            return self._cached_total
        total = sum(p.stat().st_size for p in self.root.glob("*.json")
                    if not p.name.endswith(".meta.json"))
        self._cached_total = total
        return total

    def info(self) -> StorageInfo:
        """
        What is on disk, so the user can decide - we never decide for them.

        Counts by endpoint rather than credits: cost depends on the plan, which
        this package deliberately does not know.
        """
        items = list(self.iter_stored())
        stamps = sorted(i.stored_at for i in items)
        total = self.total_bytes()
        cap = self.settings.max_storage_bytes

        by_endpoint: dict[str, int] = {}
        for i in items:
            by_endpoint[i.endpoint] = by_endpoint.get(i.endpoint, 0) + 1

        reports = self.settings.reports_dir
        report_files = ([f for f in reports.glob("*") if f.is_file()]
                        if reports.is_dir() else [])

        return StorageInfo(
            path=str(self.root),
            result_count=len(items),
            total_bytes=total,
            results_by_endpoint=dict(sorted(by_endpoint.items())),
            reports_path=str(reports),
            report_count=len(report_files),
            report_bytes=sum(f.stat().st_size for f in report_files),
            oldest=stamps[0] if stamps else None,
            newest=stamps[-1] if stamps else None,
            max_storage_bytes=cap,
            over_cap=cap is not None and total >= cap,
        )


def safe_filename(name: str) -> str:
    """
    Make a value safe as a filename. Shared with the pending store, which keys
    on the same activity_ids - UUIDs in practice, but an external value is never
    trusted into a path.

    If sanitising or truncating changes the name, a digest of the original is
    appended, so two long ids cannot collapse onto one file.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if cleaned == name and len(cleaned) <= 128:
        return cleaned
    digest = hashlib.sha256(name.encode()).hexdigest()[:16]
    return f"{cleaned[:96]}-{digest}"


def _atomic_write_json(path: Path, obj: Any) -> None:
    """
    Write JSON that a strict parser will accept, or do not write at all.

    `allow_nan=False` is the point: `fortyguard://result/{id}` serves these
    bytes verbatim and unparsed, so they must be valid by construction. Callers
    sanitise first via `replace_non_finite`, so raising here is our bug.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"), allow_nan=False)
        # os.replace, not shutil.move: shutil falls back to copy+unlink when
        # os.rename fails, which on Windows it does whenever the destination
        # exists - so an overwrite was not atomic there. Path.replace is.
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
