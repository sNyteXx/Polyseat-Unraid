"""REAL filesystem stage/switch/rollback rehearsal, restricted to disposable /tmp.

This is NOT an Unraid installer, supervisor or runtime. No host services are
started, no external paths are written, and no tar member is blindly extracted.
All state lives beneath an explicitly created, private CI/developer lab root.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .archive import MAX_BYTES, RejectedArchive, verify


SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_COMPRESSED_BYTES = 128 * 1024 * 1024


class LabDenied(ValueError):
    """An unsafe root, manifest, state or requested transition is rejected."""


class CrashInjected(RuntimeError):
    """Simulated crash, leaving a journal for an explicit recovery call."""


class HealthCheckFailed(RuntimeError):
    """Failed activation was rolled back to its previous verified slot."""


def _digest(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise LabDenied("invalid bundle SHA-256")
    return value


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, value: dict) -> None:
    parent = path.parent
    # Only a private, pre-verified lab directory is used as the parent.
    with tempfile.NamedTemporaryFile(mode="wb", dir=parent, prefix=".txn-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
    _fsync_dir(parent)


def _read_json(path: Path) -> dict | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise LabDenied(f"untrusted lab state: {path.name}")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise LabDenied(f"corrupt lab state: {path.name}") from exc
    if not isinstance(result, dict):
        raise LabDenied(f"unexpected lab state type: {path.name}")
    return result


def _require_private_dir(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        raise LabDenied("lab directory missing") from exc
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise LabDenied("lab directory is a symlink, unowned or not private")


class LabTransaction:
    """Atomic bundle-pointer switches with fail-closed recovery on a throwaway VM.

    The caller must create a private direct child of /tmp with the exact prefix
    ``polyseat-transaction-``. No recursive deletion or host path discovery is
    performed by this class. Unraid is rejected even if such a path is supplied.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        if Path("/etc/unraid-version").exists():
            raise LabDenied("filesystem lab cannot run on an Unraid host")
        if (not self.root.is_absolute() or self.root.parent != Path("/tmp")
                or not re.fullmatch(r"polyseat-transaction-[A-Za-z0-9_-]{6,}", self.root.name)):
            raise LabDenied("only a direct, unique /tmp/polyseat-transaction-* lab is allowed")
        _require_private_dir(self.root)
        self.slots = self.root / "slots"
        self.state = self.root / "state"
        _require_private_dir(self.slots, create=True)
        _require_private_dir(self.state, create=True)
        self.active_file = self.state / "active.json"
        self.journal_file = self.state / "journal.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        path = self.state / "lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or st.st_mode & 0o077):
                raise LabDenied("untrusted lab lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _pointer(self) -> dict:
        obj = _read_json(self.active_file)
        if obj is None:
            return {"current": None, "previous": None}
        if set(obj) != {"current", "previous"}:
            raise LabDenied("unknown active pointer fields")
        _digest(obj["current"], nullable=True)
        _digest(obj["previous"], nullable=True)
        return obj

    def _journal(self) -> dict | None:
        obj = _read_json(self.journal_file)
        if obj is None:
            return None
        if (set(obj) != {"phase", "target", "previous"}
                or obj["phase"] not in ("activating", "rolling-back")):
            raise LabDenied("unknown or corrupt transaction journal")
        _digest(obj["target"], nullable=True)
        _digest(obj["previous"], nullable=True)
        return obj

    def _slot(self, digest: str) -> Path:
        _digest(digest)
        return self.slots / digest

    def _verify_slot(self, digest: str) -> Path:
        slot = self._slot(digest)
        _require_private_dir(slot)
        metadata = _read_json(slot / "bundle.json")
        if metadata is None or set(metadata) != {"digest", "manifest"} or metadata["digest"] != digest:
            raise LabDenied("missing or mismatched installed bundle manifest")
        manifest = metadata["manifest"]
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise LabDenied("invalid bundle file inventory")

        expected = {"bundle.json"}
        for entry in manifest["files"]:
            if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "mode"}
                    or not isinstance(entry["path"], str)
                    or not entry["path"].startswith("payload/")
                    or not all(part and part not in (".", "..") and not part.startswith(".")
                                for part in entry["path"].split("/"))
                    or entry["mode"] not in (0o644, 0o755)
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or entry["size"] > MAX_BYTES):
                raise LabDenied("invalid bundle inventory entry")
            _digest(entry["sha256"])
            name = entry["path"]
            if name in expected:
                raise LabDenied("duplicate bundle inventory entry")
            expected.add(name)
            p = slot / name
            # Never follow a substituted parent symlink while checking a file.
            ancestor = p.parent
            while ancestor != slot:
                try:
                    parent_mode = ancestor.lstat().st_mode
                except FileNotFoundError as exc:
                    raise LabDenied("missing bundle directory") from exc
                if not stat.S_ISDIR(parent_mode):
                    raise LabDenied("symlink or special bundle directory")
                ancestor = ancestor.parent
            try:
                st = p.lstat()
            except FileNotFoundError as exc:
                raise LabDenied("missing staged file") from exc
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or st.st_size != entry["size"]
                    or stat.S_IMODE(st.st_mode) != entry["mode"]):
                raise LabDenied("symlink, mode or size mismatch in staged file")
            with p.open("rb") as source:
                actual_sha = hashlib.file_digest(source, "sha256").hexdigest()
            if actual_sha != entry["sha256"]:
                raise LabDenied("staged file checksum mismatch")

        # Reject extra files, special devices and symlinks, including under a
        # directory that was swapped after staging.
        actual = set()
        for base, dirs, files in os.walk(slot, followlinks=False):
            for dirname in dirs:
                child = Path(base) / dirname
                if not stat.S_ISDIR(child.lstat().st_mode):
                    raise LabDenied("unexpected directory symlink in bundle")
            for filename in files:
                child = Path(base) / filename
                actual.add(child.relative_to(slot).as_posix())
                if not stat.S_ISREG(child.lstat().st_mode):
                    raise LabDenied("unexpected non-regular bundle entry")
        if actual != expected:
            raise LabDenied("unexpected files in staged bundle")
        return slot

    def stage(self, archive_bytes: bytes, manifest: dict) -> str:
        """Verify and write a content-addressed bundle inside the disposable lab."""
        if not isinstance(archive_bytes, bytes) or len(archive_bytes) > MAX_COMPRESSED_BYTES:
            raise LabDenied("invalid or oversized compressed lab bundle")
        # Verify before writing ANY payload files.
        try:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
                verify(tar, manifest)
        except (tarfile.TarError, RejectedArchive) as exc:
            raise LabDenied("invalid runtime archive") from exc
        digest = hashlib.sha256(archive_bytes).hexdigest()
        with self._lock():
            if self._journal() is not None:
                raise LabDenied("unfinished transaction requires explicit recovery")
            target = self._slot(digest)
            if target.exists() or target.is_symlink():
                self._verify_slot(digest)
                return digest

            incoming = Path(tempfile.mkdtemp(dir=self.slots, prefix="incoming-"))
            _require_private_dir(incoming)
            try:
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
                    for item in manifest["files"]:
                        name = item["path"]  # verified above; never use extractall.
                        path = incoming / name
                        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        source = tar.extractfile(name)
                        if source is None:
                            raise LabDenied("declared archive payload missing")
                        with source, path.open("xb") as output:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        path.chmod(item["mode"])
                _write_json_atomic(incoming / "bundle.json", {"digest": digest, "manifest": manifest})
                _fsync_dir(incoming)
                os.rename(incoming, target)
                _fsync_dir(self.slots)
            except BaseException:
                # No recursive delete: the orphan stays inside a private,
                # disposable lab for inspection and is discarded with the VM.
                raise
            self._verify_slot(digest)
            return digest

    def active(self) -> str | None:
        with self._lock():
            if self._journal() is not None:
                raise LabDenied("unfinished transaction requires explicit recovery")
            pointer = self._pointer()
            for digest in (pointer["current"], pointer["previous"]):
                if digest is not None:
                    self._verify_slot(digest)
            return pointer["current"]

    def activate(self, digest: str, *, approved: bool,
                 health: Callable[[Path], bool] | None = None,
                 crash_at: str | None = None) -> str | None:
        """Atomically switch, then optionally health-check; roll back on failure."""
        if not approved:
            raise LabDenied("explicit activation approval required")
        if not callable(health):
            raise LabDenied("explicit health check required for lab activation")
        with self._lock():
            if self._journal() is not None:
                raise LabDenied("unfinished transaction requires explicit recovery")
            target = self._verify_slot(digest)
            before = self._pointer()
            previous = before["current"]
            if previous is not None:
                self._verify_slot(previous)
            if previous == digest:
                return previous
            _write_json_atomic(self.journal_file,
                               {"phase": "activating", "previous": previous, "target": digest})
            if crash_at == "after_journal":
                raise CrashInjected("simulated crash after durable activation journal")
            _write_json_atomic(self.active_file, {"current": digest, "previous": previous})
            if crash_at == "after_switch":
                raise CrashInjected("simulated crash after atomic pointer switch")
            try:
                healthy = health(target) is True
            except Exception:
                healthy = False
            if not healthy:
                self._restore(previous)
                raise HealthCheckFailed("health check failed; previous bundle restored")
            self._clear_journal()
            return previous

    def _clear_journal(self) -> None:
        self.journal_file.unlink()
        _fsync_dir(self.state)

    def _restore(self, previous: str | None) -> None:
        if previous is not None:
            self._verify_slot(previous)
        _write_json_atomic(self.active_file, {"current": previous, "previous": None})
        self._clear_journal()

    def recover(self) -> str | None:
        """Fail closed unless a valid journal permits reverting to prior slot."""
        with self._lock():
            journal = self._journal()
            if journal is None:
                pointer = self._pointer()
                for digest in (pointer["current"], pointer["previous"]):
                    if digest is not None:
                        self._verify_slot(digest)
                return pointer["current"]
            before = journal["previous"]
            self._restore(before)
            return before

    def rollback(self, *, approved: bool, crash_at: str | None = None) -> str | None:
        if not approved:
            raise LabDenied("explicit rollback approval required")
        with self._lock():
            if self._journal() is not None:
                raise LabDenied("unfinished transaction requires explicit recovery")
            before = self._pointer()
            previous = before["previous"]
            if before["current"] is None:
                return None
            self._verify_slot(before["current"])
            if previous is not None:
                self._verify_slot(previous)
            _write_json_atomic(self.journal_file,
                               {"phase": "rolling-back", "previous": previous,
                                "target": before["current"]})
            if crash_at == "after_journal":
                raise CrashInjected("simulated rollback crash before switch")
            self._restore(previous)
            return previous
