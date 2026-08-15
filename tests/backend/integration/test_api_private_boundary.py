from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from itertools import count

import pytest
from fastapi.testclient import TestClient

import market_voice_forecast_ledger.api as api_package
from market_voice_forecast_ledger.api import dependencies
from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.api.dependencies import PublicReadAdapter
from market_voice_forecast_ledger.cli import (
    main,
    validate_bind_host,
    validate_port,
)
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.job_state import JobStateService


FORBIDDEN_KEYS = {
    "text_body",
    "input_text",
    "metadata_json",
    "local_path",
    "audio_path",
    "prompt_body",
    "embedding",
    "manifest_hash",
    "declared_input_hash",
    "execution_contract_hash",
    "output_hash",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.for_data_dir(tmp_path / "runtime")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as value:
        yield value


def _walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _private_job(settings: Settings) -> int:
    conn = open_database(settings.database_path)
    try:
        return JobStateService(conn).create(
            JobManifest.build(
                JobKind.VIDEO_PIPELINE,
                (
                    ManifestUnit(
                        unit_key="transcript_full_test_sentinel",
                        stage=JobStage.VIDEO_METADATA,
                        ordinal=1,
                        declared_input_hash="input_text_private_sentinel",
                        dependency_keys=(),
                        execution_contract_hash="C:private_path_sentinel",
                    ),
                ),
            )
        )
    finally:
        conn.close()


def test_read_responses_recursively_exclude_private_keys_and_values(
    client: TestClient, settings: Settings
):
    job_id = _private_job(settings)

    payloads = (
        client.get("/api/health").json(),
        client.get("/api/subjects").json(),
        client.get(
            "/api/heatmaps?cutoff=2026-08-14&granularity=month"
        ).json(),
        client.get(f"/api/jobs/{job_id}").json(),
    )

    for payload in payloads:
        assert all(key not in FORBIDDEN_KEYS for key, _ in _walk(payload))
    serialized = json.dumps(payloads, ensure_ascii=False)
    for forbidden_value in (
        "transcript_full_test_sentinel",
        "input_text_private_sentinel",
        "C:private_path_sentinel",
        str(settings.database_path),
        str(settings.temp_audio_dir),
    ):
        assert forbidden_value not in serialized


def test_validation_error_never_echoes_input_reason_token_or_context(
    client: TestClient,
):
    secret_reason = "private transcript reason sentinel"
    secret_token = "token-private-sentinel"
    response = client.post(
        "/api/retention/delete?secret_query=hidden",
        json={
            "cutoff": "not-a-private-date-value",
            "preview_token": secret_token,
            "reason": secret_reason,
            "nested": {"input": "full transcript sentinel"},
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert set(payload) == {"error", "fields"}
    assert payload["error"] == "REQUEST_VALIDATION_FAILED"
    assert all(set(field) == {"location", "type"} for field in payload["fields"])
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        secret_reason,
        secret_token,
        "not-a-private-date-value",
        "secret_query",
        "full transcript sentinel",
        '"input"',
        '"ctx"',
    ):
        assert forbidden not in serialized


def test_unexpected_exception_is_exact_private_internal_error(
    client: TestClient, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError(
            "C:/private/ledger.sqlite3 text_body stack trace token-secret"
        )

    monkeypatch.setattr(HeatmapService, "read_cutoff", fail)

    response = client.get(
        "/api/heatmaps?cutoff=2026-08-14&granularity=week"
    )

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


@pytest.mark.parametrize(
    "code",
    (
        "ASSET_MAPPING_EVIDENCE_INVALID",
        "CURRENT_RESULT_STATE_INVALID",
        "RETENTION_STORED_HASH_INVALID",
        "RETENTION_STORED_STATE_INVALID",
        "RETENTION_STORED_TIME_INVALID",
    ),
)
def test_stored_corruption_domain_codes_are_exact_internal_errors(
    client: TestClient, monkeypatch, code: str
):
    def fail(*_args, **_kwargs):
        raise DomainError(code, "C:/private/ledger.sqlite3 raw stored value")

    monkeypatch.setattr(HeatmapService, "read_cutoff", fail)

    response = client.get(
        "/api/heatmaps?cutoff=2026-08-14&granularity=week"
    )

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


def test_malformed_stored_job_row_fails_closed(client: TestClient, settings: Settings):
    job_id = _private_job(settings)
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE job_units SET error_code='C:/private/ledger.sqlite3' "
            "WHERE job_id=?",
            (job_id,),
        )
    finally:
        conn.close()

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert "private" not in response.text.lower()


def test_job_read_revalidates_the_sealed_manifest(
    client: TestClient, settings: Settings
):
    job_id = _private_job(settings)
    conn = open_database(settings.database_path)
    try:
        conn.execute("DROP TRIGGER job_units_manifest_immutable")
        conn.execute(
            "UPDATE job_units SET execution_contract_hash='different-safe-hash' "
            "WHERE job_id=?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


def test_job_read_rejects_incoherent_header_and_unit_state(
    client: TestClient, settings: Settings
):
    job_id = _private_job(settings)
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE jobs SET status='succeeded' WHERE id=?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


def test_subject_read_revalidates_the_sealed_policy(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE subject_channel_policies SET policy_hash=? "
            "WHERE subject_id=(SELECT MIN(id) FROM analysis_subjects)",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/subjects")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


@pytest.mark.parametrize(
    "path",
    (
        "/api/subjects",
        "/api/heatmaps?cutoff=2026-08-14&granularity=week",
    ),
)
def test_subject_path_corruption_fails_closed_on_every_public_read(
    client: TestClient, settings: Settings, path: str
):
    private_path = "C:/private/ledger.sqlite3"
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE analysis_subjects SET canonical_name=? "
            "WHERE id=(SELECT MIN(id) FROM analysis_subjects)",
            (private_path,),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(path)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert private_path not in response.text


class _TrackedConnection:
    def __init__(self, conn, closed: list[int], identity: int) -> None:
        self._conn = conn
        self._closed = closed
        self.identity = identity

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        self._conn.close()
        self._closed.append(self.identity)


def test_one_fresh_connection_closes_for_every_outcome(
    tmp_path, monkeypatch
):
    opened: list[int] = []
    closed: list[int] = []
    real_open = open_database
    identities = count(1)

    def tracked_open(path):
        wrapper = _TrackedConnection(real_open(path), closed, next(identities))
        opened.append(wrapper.identity)
        return wrapper

    monkeypatch.setattr(dependencies, "open_database", tracked_open)
    settings = Settings.for_data_dir(tmp_path / "tracked-runtime")
    app = create_app(settings)
    assert opened == closed

    with TestClient(app, raise_server_exceptions=False) as local_client:
        for request in (
            lambda: local_client.get("/api/health"),
            lambda: local_client.post(
                "/api/mappings/1/reviews",
                json={"decision": "approve", "reason": "\u3000"},
            ),
            lambda: local_client.get("/api/jobs/999999"),
        ):
            before = len(opened)
            request()
            assert len(opened) == before + 1
            assert opened[-1] in closed

        def unexpected(_self):
            raise RuntimeError("C:/private/unexpected.sqlite3")

        monkeypatch.setattr(PublicReadAdapter, "list_subjects", unexpected)
        before = len(opened)
        response = local_client.get("/api/subjects")
        assert response.status_code == 500
        assert len(opened) == before + 1
        assert opened[-1] in closed

    assert sorted(opened) == sorted(closed)
    assert len(set(opened)) == len(opened)


def test_concurrent_requests_use_distinct_closed_connections(tmp_path, monkeypatch):
    opened: list[int] = []
    closed: list[int] = []
    real_open = open_database
    identities = count(1)

    def tracked_open(path):
        wrapper = _TrackedConnection(real_open(path), closed, next(identities))
        opened.append(wrapper.identity)
        return wrapper

    monkeypatch.setattr(dependencies, "open_database", tracked_open)
    app = create_app(Settings.for_data_dir(tmp_path / "concurrent-runtime"))
    initialization_count = len(opened)

    def request_once(_ordinal):
        with TestClient(app) as local_client:
            return local_client.get("/api/health").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = tuple(pool.map(request_once, range(8)))

    request_connections = opened[initialization_count:]
    assert statuses == (200,) * 8
    assert len(request_connections) == 8
    assert len(set(request_connections)) == 8
    assert all(identity in closed for identity in request_connections)


def test_database_initialization_failure_is_sanitized_and_closed(
    tmp_path, monkeypatch
):
    closed: list[int] = []
    real_open = open_database
    identities = count(1)

    def tracked_open(path):
        return _TrackedConnection(real_open(path), closed, next(identities))

    def fail_migration(_conn):
        raise sqlite3.OperationalError(
            "unable to open C:/private/ledger.sqlite3 raw sqlite message"
        )

    monkeypatch.setattr(dependencies, "open_database", tracked_open)
    monkeypatch.setattr(dependencies, "apply_migrations", fail_migration)

    with pytest.raises(DomainError) as error:
        create_app(Settings.for_data_dir(tmp_path / "broken-runtime"))

    assert error.value.code == "DATABASE_INITIALIZATION_FAILED"
    assert str(error.value) == "database initialization failed"
    assert closed


@pytest.mark.parametrize(
    "host",
    (
        "0.0.0.0",
        "localhost",
        "::1",
        " 127.0.0.1",
        "127.0.0.1 ",
        "192.168.1.5",
        "8.8.8.8",
        "",
        None,
        127,
    ),
)
def test_cli_rejects_every_nonexact_loopback_host(host):
    with pytest.raises(DomainError) as error:
        validate_bind_host(host)
    assert error.value.code == "NON_LOOPBACK_BIND_FORBIDDEN"


def test_cli_accepts_only_exact_integer_port():
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_port(1) == 1
    assert validate_port(65535) == 65535
    for port in (0, 65536, True, 8765.0, "8765", None):
        with pytest.raises(DomainError) as error:
            validate_port(port)
        assert error.value.code == "INVALID_SERVER_PORT"


def test_cli_import_has_no_server_side_effect_and_serve_passes_validated_values(
    tmp_path, monkeypatch
):
    import market_voice_forecast_ledger.cli as cli

    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    importlib.reload(cli)
    assert calls == []

    sentinel_app = object()
    monkeypatch.setattr(cli, "default_settings", lambda: Settings.for_data_dir(tmp_path / "cli"))
    monkeypatch.setattr(cli, "create_app", lambda _settings: sentinel_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert main(["serve", "--host", "127.0.0.1", "--port", "8765"]) == 0
    assert calls == [((sentinel_app,), {"host": "127.0.0.1", "port": 8765})]


def test_app_state_contains_only_public_metadata(settings: Settings):
    app = create_app(settings)
    serialized = repr(app.state._state)

    assert str(settings.data_dir) not in serialized
    assert str(settings.database_path) not in serialized
    assert str(settings.temp_audio_dir) not in serialized
    assert app.state.bind_boundary == "127.0.0.1"
    assert app.state.authentication == "none"


def test_api_module_documents_that_private_data_is_never_committed():
    assert api_package.__doc__ is not None
    assert "never committed" in api_package.__doc__
