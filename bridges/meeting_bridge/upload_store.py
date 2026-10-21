"""Durable store for audio segments uploaded by the phone, with a receipt that cannot lie.

The phone records every capture locally and uploads each closed segment here. It deletes its own
copy only after this store proves it holds the exact bytes, so the proof is load-bearing. For one
upload, :func:`store`:

1. checks the received bytes against the declared length and sha256, and that they are ADTS AAC
   frames (a torn last frame from a phone killed mid-write is accepted; anything else is rejected);
2. writes the sidecar first (an orphan sidecar is harmless; audio without its sidecar is not);
3. writes the audio to a unique temp file and fsyncs it;
4. ``os.link``s it to the final name, which fails rather than replacing a different file;
5. fsyncs the directory AFTER the link, so a crash cannot roll the name back once we answer;
6. re-reads the final file from disk and re-hashes it, and only then reports ``stored``.

If anything fails after the link, the link is undone, so a failed attempt never leaves a final
file that would block or falsely satisfy a retry. :func:`verify` answers "do you still hold this
exact file?" by re-hashing from disk; the phone asks again right before it deletes. The re-read can
be served from the page cache: it proves what the filesystem holds, not the platter, which is why
the phone also waits out a grace period and re-asks before deleting.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime

# seg_<UTC yyyyMMddTHHmmss>_<run uuid>_s<sample position>  (ASCII digits only, full match)
NAME_RE = re.compile(
    r"seg_([0-9]{8})T[0-9]{6}_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_s[0-9]{1,12}"
)
SHA_RE = re.compile(r"[0-9a-f]{64}")
ADTS_HEADER = 7
CHUNK = 64 * 1024


class UploadError(Exception):
    """A rejected upload: ``status`` is the HTTP status to answer with (5xx = try again later)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Stored:
    sha256: str
    size: int
    created: bool  # False when an identical file was already held


def valid_sha(sha: str) -> bool:
    return bool(SHA_RE.fullmatch(sha))


def valid_name(name: str) -> bool:
    m = NAME_RE.fullmatch(name)
    if not m:
        return False
    try:
        datetime.strptime(m.group(1), "%Y%m%d")
    except ValueError:
        return False
    return True


def final_path(root: str, name: str) -> str:
    """``<root>/<YYYY-MM-DD>/<name>.aac`` for a name that already passed :func:`valid_name`."""
    day = NAME_RE.fullmatch(name).group(1)
    return os.path.join(root, f"{day[:4]}-{day[4:6]}-{day[6:]}", f"{name}.aac")


def adts_ok(data: bytes) -> bool:
    """True when ``data`` is one or more ADTS frames, optionally ending in a single torn frame."""
    i = 0
    frames = 0
    n = len(data)
    while i + ADTS_HEADER <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xF6) != 0xF0:
            return False
        length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | ((data[i + 5] & 0xE0) >> 5)
        if length < ADTS_HEADER:
            return False
        if i + length > n:
            break  # torn last frame
        frames += 1
        i += length
    tail = n - i
    if 0 < tail < ADTS_HEADER:
        # A torn header must still look like the start of a frame, not arbitrary trailing bytes.
        if data[i] != 0xFF or (tail >= 2 and (data[i + 1] & 0xF6) != 0xF0):
            return False
    return frames > 0


def file_sha256(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def free_bytes(root: str) -> int:
    os.makedirs(root, exist_ok=True)
    return shutil.disk_usage(root).free


def sweep_temps(root: str) -> int:
    """Remove temp files a crash or a killed request left behind (run at startup)."""
    removed = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if ".tmp-" in f:
                try:
                    os.remove(os.path.join(dirpath, f))
                    removed += 1
                except OSError:
                    pass
    return removed


def _sidecar_path(final: str) -> str:
    return final[: -len(".aac")] + ".json"


def _write_sidecar(final: str, sidecar: dict) -> None:
    side = _sidecar_path(final)
    tmp = f"{side}.tmp-{uuid.uuid4().hex}"
    try:
        _write_durable(tmp, json.dumps(sidecar).encode())
        os.replace(tmp, side)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def store(root: str, name: str, data: bytes, *, sha256: str, size: int, sidecar: dict | None) -> Stored:
    """Durably store one segment and prove it. Blocking: call it off the event loop, one per name."""
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise UploadError(422, "length or sha256 does not match what was sent")
    if not adts_ok(data):
        raise UploadError(422, "not ADTS AAC audio")
    final = final_path(root, name)
    try:
        return _store_checked(final, data, sha256=sha256, size=size, sidecar=sidecar)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise UploadError(507, "edge storage is full") from None
        raise UploadError(500, "edge storage error") from None


def _store_checked(final: str, data: bytes, *, sha256: str, size: int, sidecar: dict | None) -> Stored:
    directory = os.path.dirname(final)
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        # A new day directory is itself a directory entry: persist it in its parent too, or a power
        # loss after the receipt could lose the whole directory with the file in it.
        _fsync_dir(os.path.dirname(directory))
    if os.path.exists(final):
        held, held_size = file_sha256(final)
        if held != sha256 or held_size != size:
            raise UploadError(409, "a different file already holds this name")
        if sidecar is not None and not os.path.exists(_sidecar_path(final)):
            _write_sidecar(final, sidecar)
        # An earlier attempt may have linked this file and crashed before its directory fsync:
        # make the entry durable before this answer lets the phone delete its copy.
        _fsync_dir(directory)
        return Stored(sha256=held, size=held_size, created=False)

    side = _sidecar_path(final)
    if os.path.exists(side):
        # A sidecar with no audio is left over from a failed attempt: never let it describe new bytes.
        os.remove(side)
    wrote_sidecar = False
    if sidecar is not None:
        _write_sidecar(final, sidecar)
        wrote_sidecar = True
    temp = f"{final}.tmp-{uuid.uuid4().hex}"
    linked = False
    proven = False
    try:
        _write_durable(temp, data)
        try:
            os.link(temp, final)
        except FileExistsError:
            # Lost a race to another writer of this name: same rules as an existing file.
            held, held_size = file_sha256(final)
            if held != sha256 or held_size != size:
                raise UploadError(409, "a different file already holds this name") from None
            _fsync_dir(directory)
            proven = True
            return Stored(sha256=held, size=held_size, created=False)
        linked = True
        _fsync_dir(directory)
        held, held_size = file_sha256(final)  # read back what the filesystem now holds
        if held != sha256 or held_size != size:
            raise UploadError(500, "stored file does not read back identically")
        linked = False  # proven: keep it
        proven = True
        return Stored(sha256=held, size=held_size, created=True)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
        if linked:
            # Any failure after the link undoes it, so no unproven final file survives.
            try:
                os.remove(final)
                _fsync_dir(directory)
            except OSError:
                pass
        if wrote_sidecar and not proven and not os.path.exists(final):
            try:
                os.remove(side)
            except OSError:
                pass


def verify(root: str, name: str, sha256: str) -> bool:
    """Re-hash the held file from disk: True only for the exact bytes the phone holds."""
    path = final_path(root, name)
    if not os.path.isfile(path):
        return False
    held, _size = file_sha256(path)
    return held == sha256
