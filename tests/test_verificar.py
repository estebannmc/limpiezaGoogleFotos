import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from limpieza_fotos import cli, reporte
from limpieza_fotos.verificar import (
    ERROR, OK, PARCIAL, SIN_ARCHIVO, Resultado, evaluar_grupo, verificar_archivo,
)


def _jpeg(ruta: Path) -> Path:
    Image.new("RGB", (400, 300), (120, 30, 200)).save(ruta, quality=95)
    return ruta


def test_jpeg_valido(tmp_path):
    assert verificar_archivo(_jpeg(tmp_path / "a.jpg")).estado == OK


def test_jpeg_cortado(tmp_path):
    ruta = _jpeg(tmp_path / "a.jpg")
    datos = ruta.read_bytes()
    ruta.write_bytes(datos[: len(datos) // 2])
    assert verificar_archivo(ruta).estado == ERROR


def test_archivo_vacio(tmp_path):
    ruta = tmp_path / "a.jpg"
    ruta.write_bytes(b"")
    assert verificar_archivo(ruta).estado == ERROR


def test_formato_sin_verificacion(tmp_path):
    ruta = tmp_path / "a.dng"
    ruta.write_bytes(b"raw")
    assert verificar_archivo(ruta).estado == PARCIAL


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requiere ffmpeg")
def test_video_valido_y_cortado(tmp_path):
    ruta = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(ruta)],
        check=True,
    )
    assert verificar_archivo(ruta).estado == OK

    cortado = tmp_path / "cortado.mp4"
    datos = ruta.read_bytes()
    cortado.write_bytes(datos[: int(len(datos) * 0.6)])
    assert verificar_archivo(cortado).estado == ERROR


def test_evaluar_grupo():
    a1, a2, ed = Path("anio/IMG.jpg"), Path("album/IMG.jpg"), Path("anio/IMG-edited.jpg")
    bien, mal, parcial = Resultado(OK), Resultado(ERROR, "dañada"), Resultado(PARCIAL, "solo tamaño")

    assert evaluar_grupo([], {}).estado == SIN_ARCHIVO
    # Una copia sana alcanza aunque otra esté dañada.
    assert evaluar_grupo([a1, a2], {a1: mal, a2: bien}).estado == OK
    # La versión editada también tiene que estar bien.
    assert evaluar_grupo([a1, ed], {a1: bien, ed: mal}).estado == ERROR
    assert evaluar_grupo([a1, ed], {a1: bien, ed: parcial}).estado == PARCIAL


def test_comando_verificar_genera_reporte(tmp_path):
    takeout = tmp_path / "Takeout"
    takeout.mkdir()
    _jpeg(takeout / "bien.jpg")
    (takeout / "bien.jpg.json").write_text(
        json.dumps({"title": "bien.jpg", "url": "https://photos.google.com/photo/A"}))
    roto = _jpeg(takeout / "roto.jpg")
    roto.write_bytes(roto.read_bytes()[:200])
    (takeout / "roto.jpg.json").write_text(
        json.dumps({"title": "roto.jpg", "url": "https://photos.google.com/photo/B"}))
    _jpeg(takeout / "suelto.jpg")

    salida = tmp_path / "reporte.csv"
    assert cli.main(["verificar", str(takeout), "--reporte", str(salida), "--hilos", "2"]) == 0

    filas = {f.titulo: f for f in reporte.leer_reporte(salida)}
    assert filas["bien.jpg"].estado == OK
    assert filas["roto.jpg"].estado == ERROR
    assert filas["bien.jpg"].archivos[0][1] == (takeout / "bien.jpg").stat().st_size
    assert (tmp_path / "sin_json.csv").read_text(encoding="utf-8-sig").count("suelto.jpg") == 1
    assert cli._archivos_cambiados(filas["bien.jpg"]) == ""

    (takeout / "bien.jpg").unlink()
    assert "ya no está" in cli._archivos_cambiados(filas["bien.jpg"])


def test_registro_omite_lo_ya_eliminado(tmp_path):
    registro = tmp_path / "eliminados.csv"
    fila = reporte.Fila("https://photos.google.com/photo/A", "a.jpg", OK, "", [])
    otra = reporte.Fila("https://photos.google.com/photo/B", "b.jpg", OK, "", [])
    reporte.registrar(registro, "eliminado", fila)
    reporte.registrar(registro, "fallo", otra, "sin botón")
    assert reporte.urls_procesadas(registro) == {fila.url}
