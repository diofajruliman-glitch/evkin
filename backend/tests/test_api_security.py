import duckdb
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend.module_reset import create_router as create_module_reset_router
from backend.security import (
    API_KEY_HEADER,
    ApiKeyMiddleware,
    CORS_HEADERS,
    CORS_METHODS,
    allowed_origins,
)


def secured_test_app():
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=CORS_METHODS,
        allow_headers=CORS_HEADERS,
    )
    state = {"writes": 0}

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "security-test"}

    @app.get("/api/private")
    def protected_read():
        return {"status": "ok"}

    @app.post("/api/private")
    def protected_write():
        state["writes"] += 1
        return {"writes": state["writes"]}

    return app, state


def test_health_is_public_and_contains_no_sensitive_paths(monkeypatch):
    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    app, _ = secured_test_app()
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "security-test"}


def test_protected_read_requires_correct_key(monkeypatch):
    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    app, _ = secured_test_app()
    client = TestClient(app)
    assert client.get("/api/private").status_code == 401
    assert client.get("/api/private", headers={API_KEY_HEADER: "wrong"}).status_code == 401
    assert client.get("/api/private", headers={API_KEY_HEADER: "unit-test-key"}).status_code == 200


def test_protected_write_requires_key_and_preserves_route_behavior(monkeypatch):
    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    app, state = secured_test_app()
    client = TestClient(app)
    assert client.post("/api/private").status_code == 401
    assert state["writes"] == 0
    response = client.post("/api/private", headers={API_KEY_HEADER: "unit-test-key"})
    assert response.status_code == 200
    assert response.json() == {"writes": 1}


def test_missing_server_key_fails_closed(monkeypatch):
    monkeypatch.delenv("EVKIN_API_KEY", raising=False)
    monkeypatch.delenv("EVKIN_ANALYTICS_API_KEY", raising=False)
    monkeypatch.delenv("EVKIN_ALLOW_INSECURE_LOCAL_DEV", raising=False)
    app, _ = secured_test_app()
    assert TestClient(app).get("/api/private").status_code == 503


def test_insecure_development_bypass_must_be_explicit(monkeypatch):
    monkeypatch.delenv("EVKIN_API_KEY", raising=False)
    monkeypatch.delenv("EVKIN_ANALYTICS_API_KEY", raising=False)
    monkeypatch.setenv("EVKIN_ALLOW_INSECURE_LOCAL_DEV", "1")
    app, _ = secured_test_app()
    assert TestClient(app).get("/api/private").status_code == 200


def test_cors_allows_local_origin_and_rejects_foreign_origin(monkeypatch):
    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    monkeypatch.setenv("EVKIN_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000")
    app, _ = secured_test_app()
    client = TestClient(app)
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": API_KEY_HEADER,
    }
    allowed = client.options("/api/private", headers={"Origin": "http://127.0.0.1:8000", **preflight})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert API_KEY_HEADER.lower() in allowed.headers["access-control-allow-headers"].lower()

    foreign = client.options("/api/private", headers={"Origin": "https://attacker.invalid", **preflight})
    assert foreign.headers.get("access-control-allow-origin") is None


def test_module_reset_requires_key_and_uses_existing_reset_logic(tmp_path, monkeypatch):
    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    db_path = tmp_path / "module-reset-security.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE raw_import(id INTEGER)")
        conn.execute("INSERT INTO raw_import VALUES (1)")

    app = FastAPI()
    app.include_router(create_module_reset_router(lambda: str(db_path)))
    client = TestClient(app)
    assert client.post("/api/admin/module-reset/workflow").status_code == 401
    assert client.get("/api/admin/module-reset/workflow/preview").status_code == 401
    preview = client.get(
        "/api/admin/module-reset/workflow/preview",
        headers={API_KEY_HEADER: "unit-test-key"},
    ).json()
    response = client.post(
        "/api/admin/module-reset/workflow",
        headers={API_KEY_HEADER: "unit-test-key"},
        json={"preview_token": preview["preview_token"], "confirmation": preview["confirmation"]},
    )
    assert response.status_code == 200
    assert response.json()["deleted"]["raw_import"] == 1
    with duckdb.connect(str(db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM raw_import").fetchone() == (0,)


def test_primary_evkin_app_keeps_health_public_and_info_protected(monkeypatch):
    from backend.evkin_export_api import app

    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    monkeypatch.delenv("EVKIN_ALLOW_INSECURE_LOCAL_DEV", raising=False)
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert "duckdb" not in health.json()
    assert client.get("/api/info").status_code == 401
    assert client.get("/api/info", headers={API_KEY_HEADER: "wrong"}).status_code == 401
    assert client.get("/api/info", headers={API_KEY_HEADER: "unit-test-key"}).status_code == 200
    assert client.get("/api/info", cookies={"evkin_local_api_key": "unit-test-key"}).status_code == 200


def test_frontend_only_bootstraps_the_api_key_on_loopback(monkeypatch):
    from backend.evkin_export_api import app

    monkeypatch.setenv("EVKIN_API_KEY", "unit-test-key")
    for path in ("/", "/index.html"):
        for host, peer in (("evkin.example", "127.0.0.1"), ("localhost", "192.0.2.10")):
            client = TestClient(app, base_url=f"http://{host}", client=(peer, 43210))
            response = client.get(path)
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
            assert client.get("/api/info").status_code == 401

    local = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 43210))
    assert local.get("/").cookies.get("evkin_local_api_key") == "unit-test-key"
    assert local.get("/api/info").status_code == 200
