"""CORS: en prod el widget queda restringido a los dominios propios (no abierto a '*').

Regresión de seguridad: el default de allowed_origins dejó de ser "*". Estos tests
fijan la lógica de origins_list() y se pasan los orígenes explícitos para no depender
de un .env ambiental.
"""
from config import Settings

# database_url y anthropic_api_key son obligatorios (sin default) → los pasamos como
# kwargs (precedencia sobre env/.env) para poder instanciar en los tests.
BASE = {"database_url": "sqlite://", "anthropic_api_key": "x"}


def _settings(**kw):
    return Settings(**BASE, **kw)


def test_default_declarado_no_es_wildcard():
    # El default declarado del campo (sin instanciar, a prueba de env) no es "*".
    default = Settings.model_fields["allowed_origins"].default
    assert default != "*"
    assert "gonzaloferraroautomoviles.ar" in default
    assert "mesa-production-d6a9.up.railway.app" in default


def test_prod_parsea_lista_por_comas():
    s = _settings(allowed_origins="https://a.com, https://b.com")
    assert s.origins_list() == ["https://a.com", "https://b.com"]
    assert "*" not in s.origins_list()


def test_wildcard_explicito_se_respeta():
    # Si alguien lo pone a "*" a propósito en prod, se honra (escape hatch).
    s = _settings(allowed_origins="*")
    assert s.origins_list() == ["*"]


def test_dev_abre_cors():
    s = _settings(allowed_origins="https://a.com")
    s.dev = True
    assert s.origins_list() == ["*"]


def test_prod_vacio_es_fail_closed():
    s = _settings(allowed_origins="")
    assert s.origins_list() == []
