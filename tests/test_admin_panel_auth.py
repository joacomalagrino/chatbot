"""El panel estático /admin se sirve detrás de auth server-side (HTTP Basic
contra ADMIN_API_KEY). Antes el shell del panel quedaba accesible sin auth.

conftest setea ADMIN_API_KEY=test-admin.
"""
import base64

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def _basic(user, password):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": "Basic " + raw}


def test_admin_sin_credenciales_devuelve_401(client):
    r = client.get("/admin/")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_admin_con_password_correcta_sirve_el_panel(client):
    r = client.get("/admin/", headers=_basic("admin", "test-admin"))
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_admin_con_password_incorrecta_devuelve_401(client):
    r = client.get("/admin/", headers=_basic("admin", "wrong"))
    assert r.status_code == 401


def test_admin_assets_tambien_protegidos(client):
    # app.js no debe servirse sin auth (si no, se filtra la lógica del panel).
    r = client.get("/admin/app.js")
    assert r.status_code == 401
    r = client.get("/admin/app.js", headers=_basic("admin", "test-admin"))
    assert r.status_code == 200


def test_admin_user_se_ignora_solo_importa_la_password(client):
    # El componente user del Basic se ignora: vale cualquier usuario.
    r = client.get("/admin/", headers=_basic("cualquiera", "test-admin"))
    assert r.status_code == 200


def test_otras_rutas_no_se_ven_afectadas(client):
    # El gate es solo para /admin: /health sigue abierto.
    r = client.get("/health")
    assert r.status_code == 200
