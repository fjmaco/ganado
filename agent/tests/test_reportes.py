"""Report arithmetic, checked against numbers worked out by hand.

The model is never involved in any of this — that is the whole point of the
split, and these tests are what make the claim true rather than aspirational.
"""

from __future__ import annotations

from datetime import date, timedelta

from app import reportes

NOMBRES = {"477": "Carmen", "348": "Lucía", "312": "Rosario"}


def _reg(fecha: date, vaca: str, peso: float, anulado: bool = False) -> dict:
    return {"fecha": fecha, "vaca": vaca, "peso": peso, "anulado": anulado,
            "origen": "texto", "msg_id": "", "nota": "", "fila": 0}


# Carmen:  300 -> 330 en 30 días  = +30 kg, 1000 g/día
# Lucía:   400 -> 410 en 30 días  = +10 kg,  333.3 g/día
# Rosario: 500 -> 490 en 30 días  = -10 kg, -333.3 g/día
def _historial() -> list[dict]:
    hoy = date.today()
    antes = hoy - timedelta(days=30)
    return [
        _reg(antes, "477", 300), _reg(hoy, "477", 330),
        _reg(antes, "348", 400), _reg(hoy, "348", 410),
        _reg(antes, "312", 500), _reg(hoy, "312", 490),
    ]


def test_resumen_totales_a_mano():
    r = reportes.resumen(_historial(), NOMBRES, periodo="todo")
    assert r.conteo == 3
    # Últimos pesos: 330 + 410 + 490 = 1230
    assert r.total_kg == 1230
    assert r.promedio_kg == 410
    assert r.mas_pesada[0] == "312" and r.mas_pesada[2] == 490
    assert r.mas_liviana[0] == "477" and r.mas_liviana[2] == 330


def test_ganancias_ordenadas_y_calculadas():
    df = reportes._marco(_historial())
    gs = reportes.ganancias(df, NOMBRES, periodo="todo")

    assert [g.vaca for g in gs] == ["477", "348", "312"]

    carmen = gs[0]
    assert carmen.kg == 30
    assert carmen.dias == 30
    assert round(carmen.gramos_dia) == 1000

    rosario = gs[-1]
    assert rosario.kg == -10
    assert round(rosario.gramos_dia) == -333


def test_vaca_con_un_solo_pesaje_no_entra_al_ranking():
    """Sin dos pesadas no hay ganancia medible: cero la haría competir injustamente."""
    datos = _historial() + [_reg(date.today(), "999", 350)]
    gs = reportes.ganancias(reportes._marco(datos), NOMBRES, periodo="todo")
    assert "999" not in [g.vaca for g in gs]


def test_los_anulados_no_cuentan():
    hoy = date.today()
    datos = [_reg(hoy, "477", 330), _reg(hoy, "477", 9999, anulado=True)]
    r = reportes.resumen(datos, NOMBRES, periodo="todo")
    assert r.total_kg == 330
    assert r.conteo == 1


def test_sin_pesar_lista_las_que_faltan():
    hoy = date.today()
    inicio_mes = hoy.replace(day=1)
    datos = [_reg(hoy, "477", 330)]
    faltan = reportes.sin_pesar(reportes._marco(datos), NOMBRES, desde=inicio_mes)
    assert {n for n, _ in faltan} == {"348", "312"}


def test_alertas_solo_las_que_bajaron():
    alertas = reportes.alertas(reportes._marco(_historial()), NOMBRES)
    assert [a[0] for a in alertas] == ["312"]
    assert alertas[0][2] == -10


def test_historia_en_orden_cronologico():
    hist = reportes.historia(_historial(), "477")
    assert [p for _, p in hist] == [300, 330]
    assert hist[0][0] < hist[1][0]


def test_sin_registros_no_revienta():
    r = reportes.resumen([], NOMBRES)
    assert r.conteo == 0
    assert reportes.texto_hato(r)
    assert reportes.ganancias(reportes._marco([]), NOMBRES) == []
    assert reportes.alertas(reportes._marco([]), NOMBRES) == []


def test_periodo_recorta_la_ventana():
    """Un pesaje de hace un año no debe contar dentro de 'mes'."""
    hoy = date.today()
    datos = [_reg(hoy - timedelta(days=300), "477", 200), _reg(hoy, "477", 330)]
    df = reportes._marco(datos)
    assert reportes.ganancias(df, NOMBRES, periodo="todo")[0].kg == 130
    assert reportes.ganancias(df, NOMBRES, periodo="mes") == []


# --- redacción -------------------------------------------------------------

def test_textos_van_en_espanol_y_traen_las_cifras():
    r = reportes.resumen(_historial(), NOMBRES, periodo="todo")
    texto = reportes.texto_hato(r)
    assert "Peso total" in texto and "1.230" in texto
    assert "Carmen" in texto and "Rosario" in texto

    detalle = reportes.texto_vaca("477", "Carmen", reportes.historia(_historial(), "477"))
    assert "Carmen" in detalle and "330" in detalle

    ranking = reportes.texto_ranking(
        reportes.ganancias(reportes._marco(_historial()), NOMBRES, "todo")
    )
    assert "Carmen" in ranking


async def test_comentario_descarta_cifras_inventadas(monkeypatch):
    """Si el modelo empieza a repetir números, se descarta la frase entera."""
    async def con_numeros(*a, **k):
        return "El hato subió 500 kg este mes"

    monkeypatch.setattr("app.reportes.llm.chat", con_numeros)
    assert await reportes.comentario("reporte") == ""


async def test_comentario_acepta_frase_limpia(monkeypatch):
    async def limpia(*a, **k):
        return "Vas muy bien, el hato viene levantando parejo."

    monkeypatch.setattr("app.reportes.llm.chat", limpia)
    assert await reportes.comentario("reporte") == "Vas muy bien, el hato viene levantando parejo."


async def test_comentario_silencioso_si_falla_el_modelo(monkeypatch):
    async def explotar(*a, **k):
        raise RuntimeError("gateway caído")

    monkeypatch.setattr("app.reportes.llm.chat", explotar)
    assert await reportes.comentario("reporte") == ""
