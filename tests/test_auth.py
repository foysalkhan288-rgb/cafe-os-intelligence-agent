"""API key authentication for the data API and the KDS WebSocket."""

from __future__ import annotations

import os
import tempfile

import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.remove(path)


@pytest.fixture(scope="module")
def app_with_key(temp_db_path):
    import importlib

    import cafe_os.db as db_module
    import cafe_os.tools as tools_module
    import cafe_os.graph as graph_module
    import cafe_os.main as main_module

    db_module = importlib.reload(db_module)
    tools_module = importlib.reload(tools_module)
    graph_module = importlib.reload(graph_module)

    db_module.DB_PATH = temp_db_path
    os.environ["API_KEY"] = "unit-test-key"

    main_module = importlib.reload(main_module)
    yield main_module.app

    os.environ.pop("API_KEY", None)


def test_requests_without_key_are_rejected(app_with_key):
    with TestClient(app_with_key) as client:
        response = client.get("/api/v1/menu")
    assert response.status_code == 401


def test_requests_with_wrong_key_are_rejected(app_with_key):
    with TestClient(app_with_key) as client:
        response = client.get("/api/v1/menu", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_requests_with_header_key_are_accepted(app_with_key):
    with TestClient(app_with_key) as client:
        response = client.get("/api/v1/menu", headers={"X-API-Key": "unit-test-key"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_requests_with_query_key_are_accepted(app_with_key):
    with TestClient(app_with_key) as client:
        response = client.get("/api/v1/menu?api_key=unit-test-key")
    assert response.status_code == 200


def test_health_endpoint_is_public(app_with_key):
    with TestClient(app_with_key) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_websocket_requires_key(app_with_key):
    with TestClient(app_with_key) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/kds"):
                pass


def test_websocket_accepts_key(app_with_key):
    with TestClient(app_with_key) as client:
        with client.websocket_connect("/ws/kds?api_key=unit-test-key") as ws:
            message = ws.receive_json()
    assert message["type"] == "snapshot"


def test_auth_is_disabled_without_env_key(temp_db_path):
    import importlib

    import cafe_os.db as db_module
    import cafe_os.tools as tools_module
    import cafe_os.graph as graph_module
    import cafe_os.main as main_module

    db_module = importlib.reload(db_module)
    tools_module = importlib.reload(tools_module)
    graph_module = importlib.reload(graph_module)

    db_module.DB_PATH = temp_db_path
    os.environ.pop("API_KEY", None)

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/menu")
    assert response.status_code == 200
