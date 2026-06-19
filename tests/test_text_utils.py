"""Tests de la lógica pura de text_utils (sin red ni DB)."""
import pytest

from services.text_utils import (
    extract_contact,
    normalize_ar_whatsapp,
    parse_model_json,
)


# ─────────────────────────── normalize_ar_whatsapp ───────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("5491165613300", "541165613300"),       # 13d, empieza 549 -> saca el 9
    ("+5491165613300", "541165613300"),       # saca el +, luego colapsa
    ("54 9 11 6561 3300", "541165613300"),     # saca espacios -> 13d -> colapsa
    ("541165613300", "541165613300"),          # 12d ya normalizado, no toca
    ("5411123456789", "5411123456789"),        # 13d pero empieza 541, no toca
    ("549", "549"),                            # len != 13, no colapsa
    ("", ""),                                  # vacío
    ("12025550123", "12025550123"),            # otro país, no toca
])
def test_normalize_ar_whatsapp(raw, expected):
    assert normalize_ar_whatsapp(raw) == expected


def test_normalize_ar_whatsapp_idempotente():
    once = normalize_ar_whatsapp("5491165613300")
    assert normalize_ar_whatsapp(once) == once


# ───────────────────────────── parse_model_json ──────────────────────────────

def test_parse_json_limpio():
    assert parse_model_json('{"variantes": []}') == {"variantes": []}


def test_parse_json_con_fence_json():
    assert parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_con_fence_sin_etiqueta():
    assert parse_model_json('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_con_espacios():
    assert parse_model_json('  {"a": 1}  ') == {"a": 1}


def test_parse_json_fence_abierto_sin_cierre():
    # No debe lanzar; parsea lo que puede.
    assert parse_model_json('```json\n{"a": 1}') == {"a": 1}


def test_parse_json_invalido_devuelve_error():
    out = parse_model_json("no soy json")
    assert out["error"]
    assert out["raw"] == "no soy json"


def test_parse_json_nunca_lanza_con_fence_vacio():
    out = parse_model_json("``````")
    assert "error" in out


def test_parse_json_array_no_es_objeto():
    out = parse_model_json("[1, 2, 3]")
    assert "error" in out


# ────────────────────────────── extract_contact ──────────────────────────────

def test_extract_email():
    out = extract_contact("escribime a juan@mail.com")
    assert out["email"] == "juan@mail.com"
    assert out["instagram"] is None  # el @ del email NO es handle de IG


def test_extract_email_no_contamina_instagram():
    # Bug histórico: 'juan@gmail.com' guardaba 'gmail.com' como instagram.
    out = extract_contact("mi mail es juan@gmail.com")
    assert out["email"] == "juan@gmail.com"
    assert out["instagram"] is None


def test_extract_instagram_real():
    out = extract_contact("mi ig es @juanperez")
    assert out["instagram"] == "juanperez"


def test_extract_instagram_con_puntos():
    out = extract_contact("seguime en @juan.perez.99")
    assert out["instagram"] == "juan.perez.99"


def test_extract_email_y_instagram_juntos():
    out = extract_contact("mail juan@gmail.com y mi ig @juani")
    assert out["email"] == "juan@gmail.com"
    assert out["instagram"] == "juani"


def test_extract_phone_normaliza_a_digitos():
    out = extract_contact("mi cel es 11 2345-6789 0")
    assert out["phone"] is not None
    assert out["phone"].isdigit()


def test_extract_phone_argentino_completo():
    out = extract_contact("llamame al +54 9 11 6561 3300")
    assert out["phone"] == "5491165613300"


def test_extract_no_captura_numeros_cortos():
    assert extract_contact("son 8 personas")["phone"] is None
    assert extract_contact("codigo 12345")["phone"] is None


def test_extract_mensaje_sin_datos():
    out = extract_contact("hola, quería consultar por un auto")
    assert out == {"phone": None, "email": None, "instagram": None}
