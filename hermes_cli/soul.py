"""Profile-scoped, audited persistence for the agent's own ``SOUL.md``.

The model-facing tool deliberately exposes no path or profile selector.  Its
caller uses :func:`get_hermes_home`, which is already scoped to the active
profile by the gateway.  Trusted operator surfaces such as the dashboard may
pass an already-validated profile home to the same persistence layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_constants import get_hermes_home

try:  # POSIX production path; the fallback retains process-local serialization.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 65_536
MAX_REVISIONS = 5
_MAX_REASON_CHARS = 500
_REVISION_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_PROCESS_LOCK = threading.RLock()


class SoulError(RuntimeError):
    """A self-SOUL operation was rejected without changing the file."""


class SoulConflict(SoulError):
    """The caller's compare-and-swap version no longer matches disk."""


def _load_effective_config() -> dict:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    return config if isinstance(config, dict) else {}


def _policy_block(config: Optional[dict] = None) -> Optional[dict]:
    config = _load_effective_config() if config is None else config
    block = config.get("soul_edit") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else None


def tool_enabled(config: Optional[dict] = None) -> bool:
    """Return true only for an explicit, well-formed operator enablement."""
    block = _policy_block(config)
    return bool(block is not None and block.get("enabled") is True)


def approval_required(config: Optional[dict] = None) -> bool:
    """Return the operator-owned approval policy, failing closed when malformed."""
    block = _policy_block(config)
    if block is None:
        return True
    return block.get("require_approval", True) is not False


def configured_max_bytes(config: Optional[dict] = None) -> int:
    """Return the bounded SOUL byte ceiling.

    Invalid values fall back to the conservative built-in ceiling rather than
    disabling validation.  The one-MiB upper bound prevents a malformed local
    config from turning the prompt-bearing file into an unbounded write sink.
    """
    block = _policy_block(config)
    raw = block.get("max_bytes") if block is not None else None
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 1_048_576:
        return raw
    return DEFAULT_MAX_BYTES


def _configured_read_only_profiles(config: Optional[dict] = None) -> frozenset[str]:
    block = _policy_block(config)
    if block is None:
        return frozenset({"*"})
    raw = block.get("read_only_profiles", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return frozenset({"*"})
    return frozenset(item.strip().lower() for item in raw if item.strip())


def _profile_name(home: Path) -> str:
    if home.parent.name == "profiles" and home.name:
        return home.name.lower()
    return "default"


def _version(data: bytes, *, exists: bool) -> str:
    if not exists:
        return "missing"
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _new_revision() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _secure_dir(path: Path, *, create: bool = False, mode: int = 0o700) -> None:
    if create:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SoulError(f"Required profile directory does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SoulError(
            f"Refusing non-directory or symlinked profile state path: {path}"
        )


def _open_directory(path: Path) -> int | None:
    """Open *path* without following its final component on POSIX."""
    if os.name != "posix":
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SoulError(
            f"Could not securely open profile directory {path}: {exc}"
        ) from exc


def _stat_at(home: Path, home_fd: int | None, name: str) -> os.stat_result | None:
    try:
        if home_fd is not None:
            return os.stat(name, dir_fd=home_fd, follow_symlinks=False)
        return (home / name).lstat()
    except FileNotFoundError:
        return None


def _open_at(
    home: Path,
    home_fd: int | None,
    name: str,
    flags: int,
    mode: int = 0o600,
) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if home_fd is not None:
            return os.open(name, flags, mode, dir_fd=home_fd)
        return os.open(home / name, flags, mode)
    except OSError as exc:
        raise SoulError(f"Could not securely open {home / name}: {exc}") from exc


def _assert_regular(info: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SoulError(f"Refusing symlink or non-regular SOUL file: {path}")


def _read_target(
    home: Path, home_fd: int | None
) -> tuple[bytes, bool, os.stat_result | None]:
    path = home / "SOUL.md"
    before = _stat_at(home, home_fd, "SOUL.md")
    if before is None:
        return b"", False, None
    _assert_regular(before, path)
    fd = _open_at(home, home_fd, "SOUL.md", os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        _assert_regular(opened, path)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SoulConflict(
                "SOUL.md changed while it was being opened; retry the read."
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), True, opened
    finally:
        os.close(fd)


def _decode_existing(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoulError(
            f"{path} is not valid UTF-8. Refusing to read or overwrite it."
        ) from exc


def _atomic_write(
    home: Path,
    home_fd: int | None,
    data: bytes,
    *,
    previous: os.stat_result | None,
    expected_version: str,
) -> None:
    temp_name = f".SOUL.md.{secrets.token_hex(8)}.tmp"
    temp_fd: int | None = None
    try:
        temp_fd = _open_at(
            home,
            home_fd,
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        view = memoryview(data)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:  # pragma: no cover - defensive OS invariant
                raise SoulError("Short write while persisting SOUL.md.")
            view = view[written:]
        if previous is not None:
            os.fchmod(temp_fd, stat.S_IMODE(previous.st_mode) & 0o777)
            if hasattr(os, "fchown"):
                try:
                    os.fchown(temp_fd, previous.st_uid, previous.st_gid)
                except PermissionError:
                    pass
        else:
            # Match profile creation and the existing dashboard contract:
            # SOUL.md is persona/configuration, not a credential file.
            os.fchmod(temp_fd, 0o644)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None

        current_data, current_exists, _current = _read_target(home, home_fd)
        if _version(current_data, exists=current_exists) != expected_version:
            raise SoulConflict(
                "SOUL.md changed during the atomic replacement. Nothing was written; "
                "read it again and retry."
            )
        if home_fd is not None:
            os.replace(temp_name, "SOUL.md", src_dir_fd=home_fd, dst_dir_fd=home_fd)
            os.fsync(home_fd)
        else:  # pragma: no cover - Windows
            os.replace(home / temp_name, home / "SOUL.md")
    except SoulError:
        raise
    except OSError as exc:
        raise SoulError(f"Atomic SOUL.md replacement failed: {exc}") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            if home_fd is not None:
                os.unlink(temp_name, dir_fd=home_fd)
            else:
                (home / temp_name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not clean up temporary SOUL file %s", temp_name)


def _atomic_remove(home: Path, home_fd: int | None, *, expected_version: str) -> None:
    current_data, current_exists, _current = _read_target(home, home_fd)
    if _version(current_data, exists=current_exists) != expected_version:
        raise SoulConflict(
            "SOUL.md changed during the atomic replacement. Nothing was written; "
            "read it again and retry."
        )
    if not current_exists:
        return
    try:
        if home_fd is not None:
            os.unlink("SOUL.md", dir_fd=home_fd)
            os.fsync(home_fd)
        else:  # pragma: no cover - Windows
            (home / "SOUL.md").unlink()
    except OSError as exc:
        raise SoulError(
            f"Could not atomically restore an absent SOUL.md: {exc}"
        ) from exc


@contextmanager
def _locked_home(home: Path) -> Iterator[int | None]:
    """Serialize every known SOUL writer for one profile."""
    _secure_dir(home)
    with _PROCESS_LOCK:
        home_fd = _open_directory(home)
        lock_fd: int | None = None
        try:
            lock_info = _stat_at(home, home_fd, ".SOUL.md.lock")
            if lock_info is not None and not stat.S_ISREG(lock_info.st_mode):
                raise SoulError("Refusing symlink or non-regular SOUL lock file.")
            lock_fd = _open_at(
                home,
                home_fd,
                ".SOUL.md.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise SoulError("Refusing non-regular SOUL lock file.")
            try:
                os.fchmod(lock_fd, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield home_fd
        finally:
            if lock_fd is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(lock_fd)
            if home_fd is not None:
                os.close(home_fd)


def _state_paths(home: Path, *, create: bool) -> tuple[Path, Path]:
    state = home / "state"
    soul_state = state / "soul"
    revisions = soul_state / "revisions"
    if create:
        _secure_dir(state, create=True)
        _secure_dir(soul_state, create=True)
        _secure_dir(revisions, create=True)
    else:
        for path in (state, soul_state, revisions):
            try:
                path.lstat()
            except FileNotFoundError:
                break
            _secure_dir(path)
    return soul_state, revisions


def _safe_open_file(path: Path, flags: int, mode: int = 0o600) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise SoulError(
            f"Could not securely open SOUL state file {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SoulError(f"Refusing non-regular SOUL state file: {path}")
    return fd


def _write_backup(home: Path, revision: str, before: bytes) -> Path:
    _state, revisions = _state_paths(home, create=True)
    path = revisions / f"{revision}.soul"
    fd = _safe_open_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        view = memoryview(before)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover
                raise SoulError("Short write while snapshotting SOUL.md.")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _append_audit(home: Path, record: dict[str, Any]) -> None:
    soul_state, _revisions = _state_paths(home, create=True)
    path = soul_state / "audit.jsonl"
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    fd = _safe_open_file(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - defensive OS invariant
                raise SoulError("Short write while persisting the SOUL audit log.")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_audit(home: Path) -> list[dict[str, Any]]:
    soul_state, _revisions = _state_paths(home, create=False)
    path = soul_state / "audit.jsonl"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SoulError("Refusing symlink or non-regular SOUL audit log.")
    fd = _safe_open_file(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
            lines = handle.read().splitlines()
    except UnicodeDecodeError as exc:
        raise SoulError("SOUL audit history is not valid UTF-8.") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _read_backup(home: Path, revision: str) -> bytes:
    _state, revisions = _state_paths(home, create=False)
    path = revisions / f"{revision}.soul"
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SoulError(
            f"SOUL revision {revision!r} is unavailable or outside the retained five."
        ) from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SoulError("Refusing symlink or non-regular SOUL revision file.")
    fd = _safe_open_file(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _remove_backup(home: Path, revision: str) -> None:
    _state, revisions = _state_paths(home, create=False)
    path = revisions / f"{revision}.soul"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SoulError("Refusing non-regular orphaned SOUL revision file.")
        path.unlink()
    except FileNotFoundError:
        pass


def _prune_revisions(home: Path, records: list[dict[str, Any]]) -> Optional[str]:
    """Keep the five newest content snapshots after a committed audit record."""
    _state, revisions = _state_paths(home, create=True)
    ordered = [
        str(item.get("revision"))
        for item in records
        if _REVISION_RE.fullmatch(str(item.get("revision") or ""))
    ]
    keep = set(ordered[-MAX_REVISIONS:])
    failures: list[str] = []
    for path in revisions.iterdir():
        match = re.fullmatch(r"(\d{8}T\d{6}Z-[0-9a-f]{8})\.soul", path.name)
        if not match or match.group(1) in keep:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise SoulError("revision path is not a regular file")
            path.unlink()
        except Exception as exc:  # pruning is post-commit and must not undo SOUL
            failures.append(f"{path.name}: {exc}")
    if not failures:
        return None
    warning = (
        "SOUL.md was committed, but one or more old revisions could not be pruned: "
        + "; ".join(failures)
    )
    logger.warning(warning)
    return warning


def _retained_revision_count(home: Path) -> int:
    _state, revisions = _state_paths(home, create=False)
    try:
        entries = list(revisions.iterdir())
    except FileNotFoundError:
        return 0
    count = 0
    for path in entries:
        if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}\.soul", path.name):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SoulError("Refusing symlink or non-regular SOUL revision file.")
        count += 1
    return count


def _looks_like_secret(value: Any) -> bool:
    # Reuse the configuration broker's narrow, battle-tested credential shapes.
    from hermes_cli.agent_config import _value_looks_secret

    return _value_looks_secret(value)


def _validate_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    if not reason:
        raise SoulError("A concise operator-request reason is required.")
    if len(reason) > _MAX_REASON_CHARS:
        raise SoulError(
            f"The SOUL change reason must be at most {_MAX_REASON_CHARS} characters."
        )
    if _looks_like_secret(reason):
        raise SoulError(
            "The SOUL change reason appears to contain credential material."
        )
    return reason


def _validate_content(content: str, *, max_bytes: int) -> bytes:
    if not isinstance(content, str):
        raise SoulError("SOUL content must be a Unicode string.")
    if not content.strip():
        raise SoulError("SOUL content cannot be empty.")
    if "\x00" in content:
        raise SoulError("SOUL content cannot contain NUL characters.")
    try:
        encoded = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SoulError("SOUL content is not valid UTF-8 Unicode.") from exc
    if len(encoded) > max_bytes:
        raise SoulError(
            f"SOUL content is {len(encoded):,} bytes; the configured limit is "
            f"{max_bytes:,} bytes."
        )
    from tools.threat_patterns import scan_for_threats

    findings = scan_for_threats(content, scope="context")
    if findings:
        raise SoulError(
            "SOUL content matched context-security checks: " + ", ".join(findings)
        )
    if _looks_like_secret(content):
        raise SoulError("SOUL content appears to contain credential material.")
    return encoded


def _ownership(home: Path, config: Optional[dict] = None) -> tuple[str, bool, str]:
    profile = _profile_name(home)
    read_only = _configured_read_only_profiles(config)
    if "*" in read_only or profile in read_only:
        return "operator", False, "This profile is read-only by operator policy."

    manifest_path = home / "distribution.yaml"
    if manifest_path.is_symlink():
        return "distribution", False, "Distribution ownership metadata is symlinked."
    if manifest_path.exists():
        try:
            from hermes_cli.profile_distribution import read_manifest

            manifest = read_manifest(home)
            owned = {
                str(item).strip().rstrip("/")
                for item in (manifest.owned_paths() if manifest is not None else [])
            }
        except Exception as exc:
            return (
                "distribution",
                False,
                f"Distribution ownership could not be verified: {exc}",
            )
        if "SOUL.md" in owned:
            return (
                "distribution",
                False,
                "SOUL.md is owned by this profile distribution.",
            )
    return "profile", True, ""


def _assert_writable(home: Path, config: Optional[dict] = None) -> tuple[str, str]:
    ownership, editable, reason = _ownership(home, config)
    if not editable:
        raise SoulError(reason or "SOUL.md is not profile-editable.")
    return _profile_name(home), ownership


def _commit(
    home: Path,
    home_fd: int | None,
    *,
    revision: str,
    before: bytes,
    before_exists: bool,
    before_info: os.stat_result | None,
    before_version: str,
    after: bytes,
    after_exists: bool,
    after_version: str,
    record: dict[str, Any],
) -> None:
    operation = str(record.get("operation") or "update")

    def write_state(
        data: bytes,
        exists: bool,
        *,
        previous: os.stat_result | None,
        expected_version: str,
    ) -> None:
        if exists:
            _atomic_write(
                home,
                home_fd,
                data,
                previous=previous,
                expected_version=expected_version,
            )
        else:
            _atomic_remove(home, home_fd, expected_version=expected_version)

    def restore_before() -> None:
        write_state(
            before,
            before_exists,
            previous=before_info,
            expected_version=after_version,
        )
        _remove_backup(home, revision)

    try:
        write_state(
            after,
            after_exists,
            previous=before_info,
            expected_version=before_version,
        )
    except Exception as mutation_exc:
        latest, latest_exists, _latest_info = _read_target(home, home_fd)
        latest_version = _version(latest, exists=latest_exists)
        if latest_version == before_version:
            _remove_backup(home, revision)
            raise
        if latest_version == after_version:
            try:
                restore_before()
            except Exception as restore_exc:
                if operation == "rollback":
                    raise SoulError(
                        "SOUL.md rollback reported a mutation failure after changing "
                        "the file, and exact restoration also failed. "
                        f"Rollback: {mutation_exc}; restore: {restore_exc}"
                    ) from restore_exc
                raise SoulError(
                    "SOUL.md replacement reported a durability failure after "
                    "changing the file, and exact restoration also failed. "
                    f"Replacement: {mutation_exc}; restore: {restore_exc}"
                ) from restore_exc
            if operation == "rollback":
                raise SoulError(
                    "SOUL.md rollback reported a mutation failure, so the "
                    "pre-rollback state was restored."
                ) from mutation_exc
            raise SoulError(
                "SOUL.md replacement reported a durability failure, so the "
                "original content was restored."
            ) from mutation_exc
        if operation == "rollback":
            raise SoulError(
                "SOUL.md changed unexpectedly during a failed rollback. The "
                "pre-rollback snapshot was retained for manual reconciliation."
            ) from mutation_exc
        raise SoulError(
            "SOUL.md changed unexpectedly during a failed replacement. The "
            "pre-change snapshot was retained for manual reconciliation."
        ) from mutation_exc

    try:
        _append_audit(home, record)
    except Exception as audit_exc:
        try:
            restore_before()
        except Exception as restore_exc:
            if operation == "rollback":
                raise SoulError(
                    "SOUL rollback audit failed, and restoration of the pre-rollback "
                    f"state also failed. Audit: {audit_exc}; restore: {restore_exc}"
                ) from restore_exc
            raise SoulError(
                "SOUL.md was written but audit persistence failed, and exact "
                f"restoration also failed. Reconcile it manually. Audit: {audit_exc}; "
                f"restore: {restore_exc}"
            ) from restore_exc
        if operation == "rollback":
            raise SoulError(
                "SOUL rollback audit persistence failed, so the rollback was undone."
            ) from audit_exc
        raise SoulError(
            "SOUL audit persistence failed, so the update was automatically restored."
        ) from audit_exc


def read_soul(
    *, home: Optional[Path] = None, config: Optional[dict] = None
) -> dict[str, Any]:
    home = Path(home) if home is not None else get_hermes_home()
    config = _load_effective_config() if config is None else config
    max_bytes = configured_max_bytes(config)
    with _locked_home(home) as home_fd:
        data, exists, _info = _read_target(home, home_fd)
        if len(data) > max_bytes:
            raise SoulError(
                f"SOUL.md is {len(data):,} bytes; the configured limit is "
                f"{max_bytes:,} bytes."
            )
        content = _decode_existing(data, home / "SOUL.md") if exists else ""
    ownership, editable, read_only_reason = _ownership(home, config)
    return {
        "success": True,
        "profile": _profile_name(home),
        "content": content,
        "exists": exists,
        "version": _version(data, exists=exists),
        "bytes": len(data),
        "max_bytes": max_bytes,
        "ownership": ownership,
        "editable": editable,
        "read_only_reason": read_only_reason or None,
        "apply": "next_new_session",
    }


def update_soul(
    *,
    content: str,
    expected_version: str,
    reason: str,
    actor: str = "",
    home: Optional[Path] = None,
    config: Optional[dict] = None,
) -> dict[str, Any]:
    home = Path(home) if home is not None else get_hermes_home()
    config = _load_effective_config() if config is None else config
    reason = _validate_reason(reason)
    encoded = _validate_content(content, max_bytes=configured_max_bytes(config))
    expected_version = str(expected_version or "").strip()
    if not expected_version:
        raise SoulError("expected_version from soul(action='read') is required.")

    with _locked_home(home) as home_fd:
        profile, ownership = _assert_writable(home, config)
        before, before_exists, before_info = _read_target(home, home_fd)
        if before_exists:
            _decode_existing(before, home / "SOUL.md")
        before_version = _version(before, exists=before_exists)
        if expected_version != before_version:
            raise SoulConflict(
                "SOUL.md changed after it was read. Nothing was written; read it again and retry."
            )
        if before_exists and before == encoded:
            raise SoulError("SOUL.md already has the requested content.")

        revision = _new_revision()
        _write_backup(home, revision, before)
        after_version = _version(encoded, exists=True)
        record = {
            "revision": revision,
            "timestamp": _now(),
            "operation": "update",
            "profile": profile,
            "ownership": ownership,
            "reason": reason,
            "actor": str(actor or "")[:200],
            "before_version": before_version,
            "after_version": after_version,
            "before_exists": before_exists,
            "after_exists": True,
            "bytes_before": len(before),
            "bytes_after": len(encoded),
            "apply": "next_new_session",
        }
        _commit(
            home,
            home_fd,
            revision=revision,
            before=before,
            before_exists=before_exists,
            before_info=before_info,
            before_version=before_version,
            after=encoded,
            after_exists=True,
            after_version=after_version,
            record=record,
        )

        records = _read_audit(home)
        prune_warning = _prune_revisions(home, records)
    return {
        "success": True,
        "profile": profile,
        "revision": revision,
        "version": after_version,
        "bytes": len(encoded),
        "apply": "next_new_session",
        "prune_warning": prune_warning,
        "message": (
            "SOUL.md updated atomically. The current conversation keeps its cached "
            "identity; the change applies to a new/reset session. No gateway restart "
            "is required."
        ),
    }


def history_soul(
    *,
    limit: int = 20,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    home = Path(home) if home is not None else get_hermes_home()
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise SoulError("History limit must be an integer from 1 to 100.") from exc
    if not 1 <= limit <= 100:
        raise SoulError("History limit must be an integer from 1 to 100.")
    with _locked_home(home):
        records = _read_audit(home)
        retained_revisions = _retained_revision_count(home)
    fields = (
        "revision",
        "timestamp",
        "operation",
        "profile",
        "ownership",
        "reason",
        "actor",
        "before_version",
        "after_version",
        "before_exists",
        "after_exists",
        "bytes_before",
        "bytes_after",
        "apply",
        "rolled_back_revision",
    )
    changes = [
        {field: item.get(field) for field in fields if field in item}
        for item in records[-limit:]
    ]
    return {
        "success": True,
        "profile": _profile_name(home),
        "changes": changes,
        "count": len(changes),
        "content_included": False,
        "retained_revisions": retained_revisions,
    }


def rollback_soul(
    *,
    revision: str,
    expected_version: str,
    reason: str,
    actor: str = "",
    home: Optional[Path] = None,
    config: Optional[dict] = None,
) -> dict[str, Any]:
    home = Path(home) if home is not None else get_hermes_home()
    config = _load_effective_config() if config is None else config
    reason = _validate_reason(reason)
    revision = str(revision or "").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise SoulError("A valid SOUL revision ID is required.")
    expected_version = str(expected_version or "").strip()
    if not expected_version:
        raise SoulError("expected_version from soul(action='read') is required.")

    with _locked_home(home) as home_fd:
        profile, ownership = _assert_writable(home, config)
        current, current_exists, current_info = _read_target(home, home_fd)
        if current_exists:
            _decode_existing(current, home / "SOUL.md")
        current_version = _version(current, exists=current_exists)
        if expected_version != current_version:
            raise SoulConflict(
                "SOUL.md changed after it was read. Nothing was written; read it again and retry."
            )
        records = _read_audit(home)
        target = next(
            (item for item in reversed(records) if item.get("revision") == revision),
            None,
        )
        if target is None:
            raise SoulError(f"Unknown SOUL revision {revision!r}.")
        restore_exists = bool(target.get("before_exists", True))
        restore = _read_backup(home, revision)
        if restore_exists:
            _decode_existing(restore, home / "SOUL.md revision")
            _validate_content(
                restore.decode("utf-8"),
                max_bytes=configured_max_bytes(config),
            )
        elif restore:
            raise SoulError("Absent-file SOUL revision contains unexpected bytes.")

        rollback_revision = _new_revision()
        _write_backup(home, rollback_revision, current)
        after_version = _version(restore, exists=restore_exists)
        record = {
            "revision": rollback_revision,
            "timestamp": _now(),
            "operation": "rollback",
            "profile": profile,
            "ownership": ownership,
            "reason": reason,
            "actor": str(actor or "")[:200],
            "rolled_back_revision": revision,
            "before_version": current_version,
            "after_version": after_version,
            "before_exists": current_exists,
            "after_exists": restore_exists,
            "bytes_before": len(current),
            "bytes_after": len(restore),
            "apply": "next_new_session",
        }
        _commit(
            home,
            home_fd,
            revision=rollback_revision,
            before=current,
            before_exists=current_exists,
            before_info=current_info,
            before_version=current_version,
            after=restore,
            after_exists=restore_exists,
            after_version=after_version,
            record=record,
        )

        records = _read_audit(home)
        prune_warning = _prune_revisions(home, records)
    return {
        "success": True,
        "profile": profile,
        "revision": rollback_revision,
        "rolled_back_revision": revision,
        "version": after_version,
        "exists": restore_exists,
        "bytes": len(restore),
        "apply": "next_new_session",
        "prune_warning": prune_warning,
        "message": (
            "SOUL.md rollback applied atomically. The current conversation keeps "
            "its cached identity; the restored content applies to a new/reset session."
        ),
    }
