from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from azure.core import MatchConditions
from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.storage.blob import BlobServiceClient

from contoso_agents import storage, telemetry
from contoso_agents.models import ArtifactRef, SessionState, Turn
from contoso_agents.storage import (
    ArtifactIntegrityError,
    ArtifactStore,
    AzureBlobStore,
    LeaseLostError,
    LocalStore,
    SessionConflictError,
    create_store,
)

SID = "84d1f4bd-333d-45ef-a438-bcaa5554bdda"
ACCOUNT = "https://example.blob.core.windows.net"


@pytest.fixture
def storage_root():
    root = Path.cwd() / ".storage-test-runs" / str(uuid4())
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)
        try:
            root.parent.rmdir()
        except OSError:
            pass


def azure_error(kind, code: str):
    error = kind(f"Simulated {code}")
    error.error_code = code
    return error


class FakeLease:
    def __init__(self, container, key: str) -> None:
        self.container = container
        self.key = key
        self.renewals = 0
        self.released = False

    def renew(self, **kwargs):
        self.renewals += 1
        self.container.renewed.set()
        if self.container.renew_error is not None:
            raise self.container.renew_error

    def release(self, **kwargs):
        self.released = True
        self.container.held.remove(self.key)


class FakeBlob:
    def __init__(self, container, key: str) -> None:
        self.container = container
        self.key = key

    def upload_blob(self, data: bytes, **kwargs):
        owner = self.container
        with owner.mutex:
            owner.uploads.append((self.key, data, kwargs))
            if self.key.endswith("/session.json") and owner.before_session_upload is not None:
                owner.before_session_upload(self.key)
            previous = owner.entries.get(self.key)
            if previous is not None and not kwargs.get("overwrite", False):
                raise azure_error(ResourceExistsError, "BlobAlreadyExists")
            if kwargs.get("match_condition") == MatchConditions.IfNotModified:
                if previous is None or previous[1] != kwargs["etag"]:
                    raise azure_error(ResourceModifiedError, "ConditionNotMet")
            owner.counter += 1
            owner.entries[self.key] = (data, f'"etag-{owner.counter}"')

    def download_blob(self):
        owner = self.container
        if owner.download_error is not None:
            raise owner.download_error
        with owner.mutex:
            if self.key not in owner.entries:
                raise azure_error(ResourceNotFoundError, "BlobNotFound")
            data, etag = owner.entries[self.key]
        return SimpleNamespace(readall=lambda: data, properties=SimpleNamespace(etag=etag))

    def acquire_lease(self, **kwargs):
        owner = self.container
        with owner.mutex:
            owner.acquisitions.append((self.key, kwargs))
            if owner.acquire_error is not None:
                raise owner.acquire_error
            if self.key in owner.held:
                raise azure_error(HttpResponseError, "LeaseAlreadyPresent")
            owner.held.add(self.key)
            lease = FakeLease(owner, self.key)
            owner.leases.append(lease)
            return lease


class FakeContainer:
    def __init__(self) -> None:
        self.entries = {}
        self.uploads = []
        self.acquisitions = []
        self.leases = []
        self.held = set()
        self.counter = 0
        self.mutex = threading.Lock()
        self.renewed = threading.Event()
        self.renew_error = None
        self.acquire_error = None
        self.download_error = None
        self.before_session_upload = None
        self.public_access = None
        self.properties_error = None

    def get_container_properties(self):
        if self.properties_error is not None:
            raise self.properties_error
        return {"public_access": self.public_access}

    def get_blob_client(self, key: str):
        return FakeBlob(self, key)


@pytest.fixture
def azure_double(monkeypatch):
    container = FakeContainer()
    calls = []

    def service(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(get_container_client=lambda name: container)

    monkeypatch.setattr(storage, "BlobServiceClient", service)
    monkeypatch.setattr(storage, "DefaultAzureCredential", lambda **kwargs: ("default", kwargs))
    monkeypatch.setattr(storage, "ManagedIdentityCredential", lambda **kwargs: ("managed", kwargs))
    monkeypatch.delenv("CONTOSO_AZURE_CREDENTIAL", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    return container, calls


@pytest.fixture(params=["local", "azure"])
def store(request, storage_root, azure_double):
    if request.param == "local":
        return LocalStore(storage_root)
    return AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())


def test_store_protocol_artifact_schema_and_immutable_outputs(store):
    assert isinstance(store, ArtifactStore)
    first = store.put_artifact(SID, "deck.pptx", b"first", "application/octet-stream")
    second = store.put_artifact(SID, "deck.pptx", b"second", "application/octet-stream")
    assert first.key != second.key
    assert first.key.startswith(f"sessions/{SID}/artifacts/")
    assert first.key.endswith("/deck.pptx")
    assert first.model_dump() == {
        "schema_version": 1,
        "key": first.key,
        "sha256": hashlib.sha256(b"first").hexdigest(),
        "size": 5,
        "content_type": "application/octet-stream",
        "filename": "deck.pptx",
    }
    assert store.read_artifact(first) == b"first"
    assert store.read_artifact(second) == b"second"
    empty = store.put_artifact(SID, "empty.bin", b"", "application/octet-stream")
    assert store.read_artifact(empty) == b""
    location = store.artifact_location(first)
    if isinstance(store, LocalStore):
        assert Path(location).is_absolute()
        assert Path(location).read_bytes() == b"first"
    else:
        assert location == f"{ACCOUNT}/private-artifacts/{first.key}"
        assert "?" not in location


@pytest.mark.parametrize("invalid", ["", "../escape", "not-a-uuid", "session/name", None, 3])
def test_invalid_session_ids_rejected_at_all_boundaries(store, invalid):
    operations = [
        lambda: store.put_artifact(invalid, "file.json", b"{}", "application/json"),
        lambda: store.load_session(invalid),
        lambda: store.lock_session(invalid).__enter__(),
        lambda: store.save_session(SessionState.model_construct(session_id=invalid, revision=1)),
    ]
    for operation in operations:
        with pytest.raises(ValueError):
            operation()


def test_session_ids_are_normalized(store):
    ref = store.put_artifact(SID.upper(), "file.bin", b"a", "application/octet-stream")
    assert ref.key.startswith(f"sessions/{SID}/artifacts/")
    store.save_session(SessionState(session_id=SID.upper(), revision=1))
    assert store.load_session(SID.upper()).session_id == SID


@pytest.mark.parametrize(
    "filename", ["../secret", "..", ".", "/root", "dir/file", r"dir\file", "a:b", "a?sig=x"]
)
def test_filename_traversal_is_rejected(store, filename):
    with pytest.raises(ValueError):
        store.put_artifact(SID, filename, b"a", "text/plain")


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "sessions/invalid/artifacts/invalid/file.bin",
        f"sessions/{SID}/artifacts/../file.bin",
        f"sessions/{SID}/artifacts/{SID}/wrong.bin",
        f"sessions/{SID}/artifacts/{SID.upper()}/file.bin",
        f"sessions/{SID}/artifacts/{SID}/file.bin/extra",
        f"sessions\\{SID}\\artifacts\\{SID}\\file.bin",
    ],
)
def test_ref_traversal_and_invalid_keys_are_revalidated(store, key):
    ref = ArtifactRef.model_construct(
        key=key,
        sha256="a" * 64,
        size=1,
        content_type="application/octet-stream",
        filename="file.bin",
    )
    with pytest.raises(ValueError):
        store.read_artifact(ref)
    with pytest.raises(ValueError):
        store.artifact_location(ref)


def test_integrity_checks_size_and_hash(store):
    ref = store.put_artifact(SID, "file.bin", b"abc", "application/octet-stream")
    with pytest.raises(ArtifactIntegrityError):
        store.read_artifact(ref.model_copy(update={"size": 4}))
    with pytest.raises(ArtifactIntegrityError):
        store.read_artifact(ref.model_copy(update={"sha256": "0" * 64}))


def test_content_type_headers_reject_controls(store):
    with pytest.raises(ValueError):
        store.put_artifact(SID, "file.bin", b"a", "text/plain\r\nInjected: header")


def test_durable_revisions_and_typed_artifact_references(store):
    assert store.load_session(SID) is None
    with pytest.raises(SessionConflictError):
        store.save_session(SessionState(session_id=SID, revision=0))
    with pytest.raises(SessionConflictError):
        store.save_session(SessionState(session_id=SID, revision=2))
    ref = store.put_artifact(SID, "research.json", b"{}", "application/json")
    state = SessionState(
        session_id=SID, revision=1, turns=[Turn(role="user", content="hello")], latest_research=ref
    )
    store.save_session(state)
    restored = store.load_session(SID)
    assert isinstance(restored, SessionState)
    assert restored == state
    assert isinstance(restored.latest_research, ArtifactRef)
    restored.turns.append(Turn(role="assistant", content="response"))
    restored.revision = 2
    store.save_session(restored)
    with pytest.raises(SessionConflictError):
        store.save_session(state)
    with pytest.raises(SessionConflictError):
        store.save_session(restored.model_copy(update={"revision": 4}))
    assert store.load_session(SID) == restored
    if isinstance(store, LocalStore):
        assert LocalStore(store.root).load_session(SID) == restored


def test_immutable_collision_does_not_replace_existing_artifact(store, monkeypatch):
    monkeypatch.setattr(storage, "uuid4", lambda: SID)
    ref = store.put_artifact(SID, "file.bin", b"original", "application/octet-stream")
    with pytest.raises((FileExistsError, ResourceExistsError)):
        store.put_artifact(SID, "file.bin", b"replacement", "application/octet-stream")
    assert store.read_artifact(ref) == b"original"


def test_local_atomic_write_failure_preserves_previous_revision(storage_root, monkeypatch):
    store = LocalStore(storage_root)
    state = SessionState(session_id=SID, revision=1)
    store.save_session(state)

    def fail_replace(*args):
        raise OSError("Simulated failed atomic publish")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic publish"):
        store.save_session(state.model_copy(update={"revision": 2}))
    assert store.load_session(SID) == state
    assert not list(storage_root.rglob("*.pending"))


def test_local_rejects_symlink_escape(storage_root):
    outside = storage_root / "outside"
    outside.mkdir()
    store = LocalStore(storage_root / "store")
    try:
        (store.root / "sessions").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This execution host cannot create directory symlinks")
    with pytest.raises(ValueError, match="escapes"):
        store.put_artifact(SID, "file.bin", b"a", "application/octet-stream")
    with pytest.raises(ValueError, match="escapes"):
        store.load_session(SID)
    assert list(outside.iterdir()) == []


def test_local_cas_allows_only_one_same_revision_writer(storage_root):
    first = LocalStore(storage_root)
    second = LocalStore(storage_root)
    barrier = threading.Barrier(2)

    def write(store):
        barrier.wait(timeout=5)
        try:
            store.save_session(SessionState(session_id=SID, revision=1))
            return "saved"
        except SessionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, [first, second]))
    assert sorted(outcomes) == ["conflict", "saved"]
    assert first.load_session(SID).revision == 1


def test_local_lock_does_not_resolve_an_exclusively_locked_leaf(storage_root, monkeypatch):
    store = LocalStore(storage_root)
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == ".turn.lock":
            raise PermissionError("Simulated Windows exclusive leaf handle")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with store.lock_session(SID):
        store.save_session(SessionState(session_id=SID, revision=1))
    assert store.load_session(SID).revision == 1


def test_local_lock_is_reentrant_across_instances_and_released_on_error(storage_root):
    first = LocalStore(storage_root)
    second = LocalStore(storage_root)
    assert first.load_session(SID) is None
    assert second.load_session(SID) is None
    assert first._locks[SID] is second._locks[SID]
    with pytest.raises(RuntimeError, match="turn failed"):
        with first.lock_session(SID), second.lock_session(SID):
            second.save_session(SessionState(session_id=SID, revision=1))
            assert first.load_session(SID).revision == 1
            raise RuntimeError("turn failed")
    assert not first._locks[SID].is_locked
    assert second.load_session(SID).revision == 1


def test_local_whole_turn_locks_across_processes(storage_root):
    code = """
import sys, time
from pathlib import Path
from contoso_agents.models import SessionState, Turn
from contoso_agents.storage import LocalStore
store = LocalStore(Path(sys.argv[1]))
with store.lock_session(sys.argv[2]):
    state = store.load_session(sys.argv[2]) or SessionState(session_id=sys.argv[2])
    time.sleep(0.1)
    state.turns.append(Turn(role="user", content=sys.argv[3]))
    state.revision += 1
    store.save_session(state)
"""
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join([source, env.get("PYTHONPATH", "")])
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(storage_root), SID, str(number)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in range(3)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    restored = LocalStore(storage_root).load_session(SID)
    assert restored.revision == 3
    assert sorted(turn.content for turn in restored.turns) == ["0", "1", "2"]


def test_azure_credentials_public_hooks_and_no_provisioning(azure_double, monkeypatch):
    container, calls = azure_double
    AzureBlobStore(ACCOUNT, "private-artifacts")
    assert calls[-1]["credential"] == ("default", {"exclude_interactive_browser_credential": True})
    assert callable(calls[-1]["raw_request_hook"])
    assert callable(calls[-1]["raw_response_hook"])
    monkeypatch.setenv("CONTOSO_AZURE_CREDENTIAL", "managed_identity")
    AzureBlobStore(ACCOUNT, "private-artifacts")
    assert calls[-1]["credential"] == ("managed", {})
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-client-id")
    AzureBlobStore(ACCOUNT, "private-artifacts")
    assert calls[-1]["credential"] == ("managed", {"client_id": "test-client-id"})
    supplied = object()
    AzureBlobStore(ACCOUNT, "private-artifacts", credential=supplied)
    assert calls[-1]["credential"] is supplied
    assert container.entries == {}
    monkeypatch.setenv("CONTOSO_AZURE_CREDENTIAL", "unsupported")
    with pytest.raises(ValueError, match="CREDENTIAL"):
        AzureBlobStore(ACCOUNT, "private-artifacts")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.blob.core.windows.net",
        ACCOUNT + "?sig=SECRET",
        ACCOUNT + "?",
        "https://user:password@example.blob.core.windows.net",
        ACCOUNT + "/some/path",
        ACCOUNT + "#fragment",
        ACCOUNT + "#",
    ],
)
def test_azure_account_origins_cannot_contain_credentials(azure_double, url):
    with pytest.raises(ValueError):
        AzureBlobStore(url, "private-artifacts", credential=object())
    assert azure_double[1] == []


@pytest.mark.parametrize("name", ["ABc", "a", "a--b", "../bad", "-abc", "abc-", "x" * 64])
def test_azure_container_names_are_validated(azure_double, name):
    with pytest.raises(ValueError):
        AzureBlobStore(ACCOUNT, name, credential=object())


def test_azure_private_container_and_authentication_failures_are_not_suppressed(azure_double):
    container, _ = azure_double
    container.public_access = "blob"
    with pytest.raises(ValueError, match="public access"):
        AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    container.public_access = None
    container.properties_error = ClientAuthenticationError("Simulated auth failure")
    with pytest.raises(ClientAuthenticationError):
        AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())


def test_azure_content_settings_and_etag_cas(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    ref = store.put_artifact(SID, "file.json", b"{}", "application/json")
    _, _, options = container.uploads[-1]
    assert options["overwrite"] is False
    assert options["content_settings"].content_type == ref.content_type
    state = SessionState(session_id=SID, revision=1)
    store.save_session(state)
    session_key = f"sessions/{SID}/session.json"
    old_etag = container.entries[session_key][1]
    state.revision = 2
    store.save_session(state)
    writes = [entry for entry in container.uploads if entry[0] == session_key]
    assert writes[0][2]["overwrite"] is False
    assert writes[1][2]["overwrite"] is True
    assert writes[1][2]["etag"] == old_etag
    assert writes[1][2]["match_condition"] == MatchConditions.IfNotModified
    assert writes[1][2]["content_settings"].content_type == "application/json"
    assert json.loads(writes[1][1])["schema_version"] == 1
    assert container.held == set()
    assert all(lease.released for lease in container.leases)
    assert all(options["lease_duration"] == 60 for _, options in container.acquisitions)


def test_azure_cas_conflict_propagates_without_overwriting_winner(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    state = SessionState(session_id=SID, revision=1)
    store.save_session(state)
    state.revision = 2
    winner = state.model_copy(update={"turns": [Turn(role="user", content="winning writer")]})

    def replace_session(key):
        container.entries[key] = (winner.model_dump_json().encode(), '"external-etag"')

    container.before_session_upload = replace_session
    with pytest.raises(ResourceModifiedError):
        store.save_session(state)
    assert store.load_session(SID) == winner
    assert container.held == set()


def test_azure_missing_container_and_network_errors_propagate(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    assert store.load_session(SID) is None
    container.download_error = azure_error(ResourceNotFoundError, "ContainerNotFound")
    with pytest.raises(ResourceNotFoundError):
        store.load_session(SID)
    container.download_error = OSError("Simulated network outage")
    with pytest.raises(OSError, match="network outage"):
        store.load_session(SID)


def test_azure_whole_turn_lock_serializes_independent_stores(azure_double):
    container, _ = azure_double
    first = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    second = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    second._lease_retry_interval = 0.01
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    second_entered = threading.Event()

    def first_turn():
        with first.lock_session(SID):
            entered.set()
            assert release.wait(timeout=5)
            first.save_session(
                SessionState(session_id=SID, revision=1, turns=[Turn(role="user", content="one")])
            )

    def second_turn():
        assert entered.wait(timeout=5)
        waiting.set()
        with second.lock_session(SID):
            second_entered.set()
            state = second.load_session(SID)
            state.turns.append(Turn(role="user", content="two"))
            state.revision += 1
            second.save_session(state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_turn)
        second_future = pool.submit(second_turn)
        assert waiting.wait(timeout=5)
        try:
            assert not second_entered.wait(timeout=0.1)
        finally:
            release.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    assert first.load_session(SID).revision == 2
    assert [turn.content for turn in first.load_session(SID).turns] == ["one", "two"]
    assert container.held == set()


def test_azure_finite_lease_is_renewed_and_failure_is_surfaced(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    store._lease_renew_interval = 0.01
    with store.lock_session(SID):
        assert container.renewed.wait(timeout=5)
    assert container.leases[-1].renewals > 0
    container.renewed.clear()
    container.renew_error = azure_error(HttpResponseError, "LeaseLost")
    with pytest.raises(LeaseLostError):
        with store.lock_session(SID):
            assert container.renewed.wait(timeout=5)
    assert container.held == set()
    assert store.load_session(SID) is None


def test_azure_failed_lease_confirmation_prevents_session_commit(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    container.renew_error = OSError("Simulated lease network failure")
    with pytest.raises(LeaseLostError):
        store.save_session(SessionState(session_id=SID, revision=1))
    assert f"sessions/{SID}/session.json" not in container.entries
    assert container.held == set()


def test_azure_lock_timeout_and_non_contention_errors_are_explicit(azure_double):
    container, _ = azure_double
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    store._lock_timeout = 0
    container.acquire_error = azure_error(HttpResponseError, "LeaseAlreadyPresent")
    with pytest.raises(TimeoutError):
        with store.lock_session(SID):
            pytest.fail("Must not enter a contended turn")
    container.acquire_error = ClientAuthenticationError("Simulated auth error")
    with pytest.raises(ClientAuthenticationError):
        with store.lock_session(SID):
            pytest.fail("Must not enter an unauthenticated turn")


@pytest.mark.parametrize("backend", ["local", "azure"])
def test_persisted_wrong_session_and_invalid_revision_are_errors(
    storage_root, azure_double, backend
):
    if backend == "local":
        store = LocalStore(storage_root)
    else:
        store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    for data in (
        SessionState(session_id=str(uuid4()), revision=1).model_dump_json().encode(),
        SessionState(session_id=SID, revision=0).model_dump_json().encode(),
        b"not-json",
    ):
        if backend == "local":
            path = storage_root / "sessions" / SID / "session.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        else:
            azure_double[0].entries[f"sessions/{SID}/session.json"] = (data, '"etag"')
        with pytest.raises(ValueError):
            store.load_session(SID)


def test_create_store_environment_selection(storage_root, azure_double, monkeypatch):
    monkeypatch.delenv("CONTOSO_STORE", raising=False)
    monkeypatch.setenv("CONTOSO_DATA_DIR", str(storage_root))
    assert create_store().root == storage_root
    monkeypatch.setenv("CONTOSO_STORE", "azure")
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONTAINER", raising=False)
    with pytest.raises(ValueError, match="requires"):
        create_store()
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", ACCOUNT)
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "private-artifacts")
    assert isinstance(create_store(), AzureBlobStore)
    monkeypatch.setenv("CONTOSO_STORE", "unsupported")
    with pytest.raises(ValueError, match="CONTOSO_STORE"):
        create_store()
    monkeypatch.setenv("CONTOSO_STORE", "local")
    monkeypatch.setenv("CONTOSO_DATA_DIR", " ")
    with pytest.raises(ValueError, match="DATA_DIR"):
        create_store()


def test_http_timing_uses_public_hooks_and_redacts_stderr(azure_double, monkeypatch, capsys):
    logger = logging.Logger("isolated-timing")
    times = iter([10.0, 10.125])
    monkeypatch.setattr(telemetry, "perf_counter", lambda: next(times))
    with monkeypatch.context() as scoped:
        scoped.setattr(telemetry.logging, "getLogger", lambda name=None: logger)
        AzureBlobStore(ACCOUNT, "private-artifacts", credential=object())
    hooks = azure_double[1][-1]
    request = SimpleNamespace(
        context={},
        http_request=SimpleNamespace(
            method="PUT",
            url="https://name:password@example.blob.core.windows.net/a?sig=SAS_SECRET#secret",
            headers={"Authorization": "Bearer AUTH_SECRET"},
            body=b"BODY_SECRET",
        ),
    )
    response = SimpleNamespace(
        context=request.context, http_response=SimpleNamespace(status_code=201)
    )
    hooks["raw_request_hook"](request)
    hooks["raw_response_hook"](response)
    captured = capsys.readouterr()
    assert captured.out == ""
    event = json.loads(captured.err)
    assert event["url"] == f"{ACCOUNT}/a"
    assert event["duration_ms"] == 125
    assert event["status_code"] == 201
    assert event["timestamp"]
    assert not any(
        secret in captured.err
        for secret in ["name", "password", "SAS_SECRET", "AUTH_SECRET", "BODY_SECRET", "?sig"]
    )


def test_http_timing_exception_does_not_log_exception_or_headers(monkeypatch):
    records = []

    class Logger:
        def info(self, record):
            records.append(json.loads(record))

    times = iter([1.0, 1.5])
    monkeypatch.setattr(telemetry, "perf_counter", lambda: next(times))
    policy = telemetry.AzureHttpTimingPolicy(logger=Logger())
    request = SimpleNamespace(
        context={}, http_request=SimpleNamespace(method="GET", url=ACCOUNT + "/a?sig=secret")
    )
    policy.on_request(request)
    policy.on_exception(request)
    assert records[0]["outcome"] == "exception"
    assert records[0]["duration_ms"] == 500
    assert records[0]["status_code"] is None
    assert "secret" not in json.dumps(records)
    assert request.context == {}


def test_real_sdk_public_hooks_run_with_injected_transport_and_token(monkeypatch):
    events = []
    policy = telemetry.AzureHttpTimingPolicy(
        logger=SimpleNamespace(info=lambda message: events.append(json.loads(message)))
    )
    monkeypatch.setattr(storage, "AzureHttpTimingPolicy", lambda: policy)

    class Response(HttpResponse):
        def __init__(self, request):
            super().__init__(request, None)
            self.status_code = 200 if request.method == "GET" else 201
            self.reason = "OK"
            self.headers = {
                "etag": '"mock-etag"',
                "last-modified": "Thu, 08 Oct 2026 12:00:00 GMT",
                "content-length": "0",
                "x-ms-request-id": "mock-request",
            }

        def body(self):
            return b""

    class Transport(HttpTransport):
        def __init__(self):
            self.requests = []

        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def send(self, request, **kwargs):
            self.requests.append(request)
            return Response(request)

    class Credential:
        def get_token(self, *scopes, **kwargs):
            return AccessToken("fake-access-token-not-a-real-credential", 4_000_000_000)

    transport = Transport()
    monkeypatch.setattr(
        storage,
        "BlobServiceClient",
        lambda **kwargs: BlobServiceClient(transport=transport, **kwargs),
    )
    store = AzureBlobStore(ACCOUNT, "private-artifacts", credential=Credential())
    ref = store.put_artifact(SID, "sample.json", b"{}", "application/json")
    assert [request.method for request in transport.requests] == ["GET", "PUT"]
    assert transport.requests[-1].headers["x-ms-blob-content-type"] == "application/json"
    assert transport.requests[-1].headers["If-None-Match"] == "*"
    assert [event["status_code"] for event in events] == [200, 201]
    assert events[-1]["url"] == store.artifact_location(ref)
    assert all("?" not in event["url"] for event in events)
    assert "fake-access-token" not in json.dumps(events)


@pytest.mark.parametrize(
    "url", ["not-a-url", "https://user:secret@example.test:bad/a?sig=secret", "file:///secret"]
)
def test_http_redaction_does_not_echo_malformed_urls(url):
    assert telemetry.redact_url(url) == "[redacted-url]"
