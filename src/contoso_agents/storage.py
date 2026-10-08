"""Immutable artifacts and revision-checked session persistence."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from filelock import FileLock

from .models import ArtifactRef, SessionState
from .telemetry import AzureHttpTimingPolicy


class ArtifactIntegrityError(ValueError):
    """Signals corrupt artifact bytes or inconsistent session state to stop unsafe use."""


class SessionConflictError(RuntimeError):
    """Rejects a session commit that would overwrite or skip the expected next revision."""


class LeaseLostError(RuntimeError):
    """Stops a turn when its Azure session lease can no longer protect a safe state update."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Defines the persistence and artifact operations shared by the workflow and its workers."""

    def put_artifact(
        self, session_id: str, filename: str, data: bytes, content_type: str
    ) -> ArtifactRef: ...

    def read_artifact(self, ref: ArtifactRef) -> bytes: ...

    def load_session(self, session_id: str) -> SessionState | None: ...

    def save_session(self, state: SessionState) -> None: ...

    def lock_session(self, session_id: str) -> AbstractContextManager[None]: ...

    def artifact_location(self, ref: ArtifactRef) -> str: ...


def _session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Session ID must be a UUID string")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Session ID must be a UUID string") from exc


def _artifact_ref(ref: ArtifactRef) -> ArtifactRef:
    checked = ArtifactRef.model_validate(ref.model_dump())
    parts = checked.key.split("/")
    if (
        len(parts) != 5
        or parts[0] != "sessions"
        or parts[2] != "artifacts"
        or parts[4] != checked.filename
    ):
        raise ValueError("Artifact key must identify a session's immutable artifact")
    for identifier in (parts[1], parts[3]):
        if _session_id(identifier) != identifier:
            raise ValueError("Artifact key UUIDs must be canonical")
    return checked


def _new_ref(session_id: str, filename: str, data: bytes, content_type: str) -> ArtifactRef:
    session_id = _session_id(session_id)
    if not isinstance(data, bytes):
        raise TypeError("Artifact data must be bytes")
    if not isinstance(content_type, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in content_type
    ):
        raise ValueError("Content type must not contain control characters")
    return _artifact_ref(
        ArtifactRef(
            key=f"sessions/{session_id}/artifacts/{uuid4()}/{filename}",
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
            filename=filename,
        )
    )


def _check_bytes(ref: ArtifactRef, data: bytes) -> bytes:
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ArtifactIntegrityError("Artifact size or SHA-256 does not match its reference")
    return data


def _state(state: SessionState) -> SessionState:
    _session_id(state.session_id)
    checked = SessionState.model_validate(state.model_dump())
    for ref in (checked.latest_research, checked.latest_deck, checked.latest_pptx):
        if ref is not None:
            _artifact_ref(ref)
    return checked


def _decode_state(data: bytes, session_id: str) -> SessionState:
    state = _state(SessionState.model_validate_json(data))
    if state.session_id != session_id or state.revision < 1:
        raise ArtifactIntegrityError("Persisted session ID or revision is invalid")
    return state


def _check_revision(state: SessionState, previous: SessionState | None) -> None:
    expected = 1 if previous is None else previous.revision + 1
    if state.revision != expected:
        raise SessionConflictError(f"Session revision must be {expected}, not {state.revision}")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _make_directories(path: Path, *, exist_ok: bool = True) -> None:
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=exist_ok)
    for created in reversed(missing):
        _sync_directory(created.parent)


class LocalStore:
    """Persists sessions and immutable artifacts on local disk across workflow process restarts."""

    _lock_registry_guard = threading.Lock()

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        _make_directories(self.root)
        self._locks: dict[str, FileLock] = {}

    def _path(self, key: str) -> Path:
        path = self.root.joinpath(*key.split("/"))
        # Resolving an exclusively locked leaf is unreliable on Windows; resolve its parent.
        if not path.parent.resolve().is_relative_to(self.root):
            raise ValueError("Storage path escapes the configured root")
        if path.is_symlink() and not path.resolve().is_relative_to(self.root):
            raise ValueError("Storage path escapes the configured root")
        return path

    def _session_path(self, session_id: str) -> Path:
        return self._path(f"sessions/{_session_id(session_id)}/session.json")

    def _atomic_write(self, path: Path, data: bytes, *, immutable: bool = False) -> None:
        _make_directories(path.parent)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.pending")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if immutable:
                # An atomic hard link publishes a complete file without ever replacing one.
                os.link(temporary, path)
                temporary.unlink()
            else:
                os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def put_artifact(
        self, session_id: str, filename: str, data: bytes, content_type: str
    ) -> ArtifactRef:
        ref = _new_ref(session_id, filename, data, content_type)
        path = self._path(ref.key)
        _make_directories(path.parent, exist_ok=False)
        self._atomic_write(path, data, immutable=True)
        return ref

    def read_artifact(self, ref: ArtifactRef) -> bytes:
        ref = _artifact_ref(ref)
        return _check_bytes(ref, self._path(ref.key).read_bytes())

    def artifact_location(self, ref: ArtifactRef) -> str:
        return str(self._path(_artifact_ref(ref).key).resolve())

    def load_session(self, session_id: str) -> SessionState | None:
        session_id = _session_id(session_id)
        with self.lock_session(session_id):
            path = self._session_path(session_id)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return None
        return _decode_state(data, session_id)

    def save_session(self, state: SessionState) -> None:
        state = _state(state)
        with self.lock_session(state.session_id):
            _check_revision(state, self.load_session(state.session_id))
            self._atomic_write(
                self._session_path(state.session_id),
                state.model_dump_json().encode("utf-8"),
            )

    @contextmanager
    def lock_session(self, session_id: str) -> Iterator[None]:
        session_id = _session_id(session_id)
        path = self._path(f"sessions/{session_id}/.turn.lock")
        _make_directories(path.parent)
        with self._lock_registry_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = FileLock(str(path), is_singleton=True)
                self._locks[session_id] = lock
        with lock:
            yield


def _property(value, name: str):
    return value.get(name) if isinstance(value, dict) else getattr(value, name)


def _error_code(error: HttpResponseError) -> str | None:
    code = getattr(error, "error_code", None)
    return getattr(code, "value", code)


class _LeaseGuard:
    """Renews the Blob session lease and detects lease loss while a workflow turn is running."""

    _request_options = {
        "timeout": 10,
        "connection_timeout": 5,
        "read_timeout": 5,
        "retry_total": 0,
    }

    def __init__(self, lease, renew_interval: float) -> None:
        self.lease = lease
        self._failure: Exception | None = None
        self._stop = threading.Event()
        self._mutex = threading.Lock()
        self._renew_interval = renew_interval
        self._thread = threading.Thread(target=self._renew, name="contoso-lease-renew", daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(self._renew_interval):
            try:
                self.confirm()
            except Exception as exc:
                self._failure = exc
                self._stop.set()

    def check(self) -> None:
        if self._failure is not None:
            raise LeaseLostError("Azure session lease renewal failed; abort this turn") from (
                self._failure
            )

    def confirm(self) -> None:
        with self._mutex:
            self.check()
            try:
                self.lease.renew(**self._request_options)
            except Exception as exc:
                self._failure = exc
                self._stop.set()
                raise LeaseLostError("Azure session lease could not be confirmed") from exc

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise LeaseLostError("Azure lease renewal did not stop; the finite lease will expire")
        errors: list[Exception] = []
        try:
            self.check()
        except LeaseLostError as exc:
            errors.append(exc)
        try:
            self.lease.release(**self._request_options)
        except Exception as exc:
            errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Azure session lease cleanup failed", errors)


class AzureBlobStore:
    """Shares durable sessions and immutable artifacts through an existing private Blob container.

    With no supplied credential, CONTOSO_AZURE_CREDENTIAL selects ``default``
    or ``managed_identity``; the latter optionally uses AZURE_CLIENT_ID.
    """

    def __init__(self, account_url: str, container: str, credential=None) -> None:
        parts = urlsplit(account_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in account_url
            or "#" in account_url
            or parts.path not in {"", "/"}
        ):
            raise ValueError(
                "Storage account URL must be an HTTPS origin without credentials or SAS"
            )
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", container) or "--" in container:
            raise ValueError("Storage container must be a valid Azure container name")
        self.account_url = account_url.rstrip("/")
        self.container = container
        if credential is None:
            choice = os.getenv("CONTOSO_AZURE_CREDENTIAL", "default").strip().lower()
            if choice == "default":
                credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            elif choice == "managed_identity":
                client_id = os.getenv("AZURE_CLIENT_ID")
                credential = (
                    ManagedIdentityCredential(client_id=client_id)
                    if client_id
                    else ManagedIdentityCredential()
                )
            else:
                raise ValueError("CONTOSO_AZURE_CREDENTIAL must be default or managed_identity")
        timing = AzureHttpTimingPolicy()
        # The Blob SDK exposes these public hooks on each request, including retries.
        service = BlobServiceClient(
            account_url=self.account_url,
            credential=credential,
            raw_request_hook=timing.on_request,
            raw_response_hook=timing.on_raw_response,
        )
        self._container = service.get_container_client(container)
        properties = self._container.get_container_properties()
        if _property(properties, "public_access") is not None:
            raise ValueError("The existing storage container must have public access disabled")
        self._active = threading.local()
        self._lease_duration = 60
        self._lease_renew_interval = 20.0
        self._lock_timeout = 60.0
        self._lease_retry_interval = 0.25

    def _blob(self, key: str):
        return self._container.get_blob_client(key)

    def put_artifact(
        self, session_id: str, filename: str, data: bytes, content_type: str
    ) -> ArtifactRef:
        ref = _new_ref(session_id, filename, data, content_type)
        self._blob(ref.key).upload_blob(
            data,
            overwrite=False,
            content_settings=ContentSettings(content_type=ref.content_type),
            validate_content=True,
        )
        return ref

    def read_artifact(self, ref: ArtifactRef) -> bytes:
        ref = _artifact_ref(ref)
        return _check_bytes(ref, self._blob(ref.key).download_blob().readall())

    def artifact_location(self, ref: ArtifactRef) -> str:
        return f"{self.account_url}/{self.container}/{_artifact_ref(ref).key}"

    def _read_session(self, session_id: str) -> tuple[SessionState | None, str | None]:
        try:
            download = self._blob(f"sessions/{session_id}/session.json").download_blob()
            data = download.readall()
        except ResourceNotFoundError as exc:
            if _error_code(exc) != "BlobNotFound":
                raise
            return None, None
        etag = _property(download.properties, "etag")
        if not etag:
            raise ArtifactIntegrityError("Azure session download did not include an ETag")
        return _decode_state(data, session_id), etag

    def load_session(self, session_id: str) -> SessionState | None:
        return self._read_session(_session_id(session_id))[0]

    def save_session(self, state: SessionState) -> None:
        state = _state(state)
        with self.lock_session(state.session_id):
            previous, etag = self._read_session(state.session_id)
            _check_revision(state, previous)
            guard = self._active.leases[state.session_id]
            guard.confirm()
            options = {
                "overwrite": previous is not None,
                "content_settings": ContentSettings(
                    content_type="application/json", content_encoding="utf-8"
                ),
            }
            if previous is not None:
                options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
            self._blob(f"sessions/{state.session_id}/session.json").upload_blob(
                state.model_dump_json().encode("utf-8"), **options
            )
            guard.check()

    def _acquire_lease(self, session_id: str):
        blob = self._blob(f"sessions/{session_id}/turn.lock")
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError as exc:
            if _error_code(exc) != "BlobAlreadyExists":
                raise
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                return blob.acquire_lease(
                    lease_duration=self._lease_duration, **_LeaseGuard._request_options
                )
            except HttpResponseError as exc:
                if _error_code(exc) != "LeaseAlreadyPresent":
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the Azure session lease") from exc
                time.sleep(self._lease_retry_interval)

    @contextmanager
    def lock_session(self, session_id: str) -> Iterator[None]:
        session_id = _session_id(session_id)
        if not hasattr(self._active, "leases"):
            self._active.leases = {}
        leases: dict[str, _LeaseGuard] = self._active.leases
        if session_id in leases:
            leases[session_id].check()
            yield
            leases[session_id].check()
            return
        guard = _LeaseGuard(self._acquire_lease(session_id), self._lease_renew_interval)
        leases[session_id] = guard
        try:
            guard.check()
            yield
            guard.check()
        finally:
            del leases[session_id]
            guard.close()


def create_store() -> ArtifactStore:
    """Creates the local or Azure Blob store used for durable sessions and worker artifacts."""
    kind = os.getenv("CONTOSO_STORE", "local").strip().lower()
    if kind == "local":
        directory = os.getenv("CONTOSO_DATA_DIR", ".data")
        if not directory.strip():
            raise ValueError("CONTOSO_DATA_DIR must not be empty")
        return LocalStore(Path(directory))
    if kind == "azure":
        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
        container = os.getenv("AZURE_STORAGE_CONTAINER", "")
        if not account_url or not container:
            raise ValueError("Azure storage requires AZURE_STORAGE_ACCOUNT_URL and container")
        return AzureBlobStore(account_url, container)
    raise ValueError("CONTOSO_STORE must be local or azure")
