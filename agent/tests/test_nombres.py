"""Cow names: unique, stable, and findable in a sentence."""

from __future__ import annotations

import pytest

from app.nombres import NOMBRES, asignar_nombre, buscar_por_nombre
from app.texto import normalizar


def test_lista_sin_duplicados_ni_compuestos():
    normalizados = [normalizar(n) for n in NOMBRES]
    assert len(set(normalizados)) == len(NOMBRES), "hay nombres repetidos en la lista"
    assert all(" " not in n for n in NOMBRES), "hay nombres compuestos en la lista"
    assert len(NOMBRES) >= 150


def test_asignacion_nunca_repite_en_un_hato_grande():
    """Muy por encima de las 40 vacas de mi papá, y sin una sola colisión."""
    usados: set[str] = set()
    for _ in range(200):
        nombre = asignar_nombre(usados)
        assert normalizar(nombre) not in {normalizar(u) for u in usados}
        usados.add(nombre)
    assert len(usados) == 200


def test_asignacion_es_determinista():
    """Un reinicio le asigna a la misma vaca el mismo nombre."""
    assert asignar_nombre(set()) == asignar_nombre(set()) == NOMBRES[0]
    assert asignar_nombre({NOMBRES[0]}) == NOMBRES[1]


def test_asignacion_ignora_tildes_y_mayusculas():
    """Un 'lucia' escrito a mano en la hoja igual cuenta como Lucía tomada."""
    assert asignar_nombre({"lucia"}) != "Lucía"


def test_asignacion_rellena_huecos():
    """Si una vaca se va, su nombre queda libre para la siguiente."""
    usados = set(NOMBRES[:5]) - {NOMBRES[2]}
    assert asignar_nombre(usados) == NOMBRES[2]


def test_buscar_nombre_en_una_frase():
    vacas = {"477": "Carmen", "348": "Lucía", "312": "Rosa", "500": "Rosalía"}
    assert buscar_por_nombre("¿cómo va Carmen?", vacas) == "477"
    assert buscar_por_nombre("carmen pesa 430", vacas) == "477"
    assert buscar_por_nombre("pesé a LUCIA hoy", vacas) == "348"


def test_nombre_largo_gana_al_corto():
    """'Rosalía' no debe resolverse como 'Rosa'."""
    vacas = {"312": "Rosa", "500": "Rosalía"}
    assert buscar_por_nombre("cómo va Rosalía", vacas) == "500"
    assert buscar_por_nombre("cómo va Rosa", vacas) == "312"


def test_no_encuentra_nombre_dentro_de_otra_palabra():
    """'Ana' no debe dispararse dentro de 'mañana'."""
    assert buscar_por_nombre("la peso mañana", {"1": "Ana"}) is None


def test_sin_coincidencia_devuelve_none():
    assert buscar_por_nombre("477 327", {"477": "Carmen"}) is None


# --- machos ----------------------------------------------------------------

def test_un_ternero_no_se_llama_Carmen():
    """No todo animal del hato es una vaca."""
    from app.nombres import MACHO, NOMBRES_MACHO

    nombre = asignar_nombre(set(), MACHO)
    assert nombre in NOMBRES_MACHO
    assert nombre not in NOMBRES


def test_machos_y_hembras_no_chocan():
    """Un solo espacio de nombres: nadie repite, sea del sexo que sea."""
    from app.nombres import HEMBRA, MACHO

    usados: set[str] = set()
    for i in range(100):
        n = asignar_nombre(usados, MACHO if i % 3 == 0 else HEMBRA)
        assert normalizar(n) not in {normalizar(u) for u in usados}
        usados.add(n)
    assert len(usados) == 100


def test_lista_de_machos_sana():
    from app.nombres import NOMBRES_MACHO

    norm = [normalizar(n) for n in NOMBRES_MACHO]
    assert len(set(norm)) == len(NOMBRES_MACHO)
    assert all(" " not in n for n in NOMBRES_MACHO)
    assert not set(norm) & {normalizar(n) for n in NOMBRES}, "no se cruzan con las hembras"


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("Ternero peso 126", "M"),
        ("el ternero pesa 126", "M"),
        ("novillo 5 en 300", "M"),
        ("el toro 900", "M"),
        ("la ternera 88 pesa 120", "H"),
        ("novilla 12 en 250", "H"),
        ("vaca 309 peso 417", "H"),
        ("309 417", None),
    ],
)
def test_sexo_segun_como_lo_nombro(texto, esperado):
    from app.nombres import sexo_mencionado

    assert sexo_mencionado(texto) == esperado
