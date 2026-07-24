"""Cow names: unique, stable, and findable in a sentence."""

from __future__ import annotations

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
