import json
from pathlib import Path

from limpieza_fotos.takeout import escanear


def _json(ruta: Path, titulo: str, url: str) -> None:
    ruta.write_text(json.dumps({"title": titulo, "url": url}), encoding="utf-8")


def _archivo(ruta: Path) -> Path:
    ruta.write_bytes(b"x")
    return ruta


def _por_url(raiz: Path):
    grupos, sin_json = escanear(raiz)
    return {g.url: sorted(a.name for a in g.archivos) for g in grupos}, sorted(p.name for p in sin_json)


def test_json_clasico_y_supplemental(tmp_path):
    _archivo(tmp_path / "IMG_1.jpg")
    _json(tmp_path / "IMG_1.jpg.json", "IMG_1.jpg", "https://photos.google.com/photo/A")
    _archivo(tmp_path / "IMG_2.jpg")
    _json(tmp_path / "IMG_2.jpg.supplemental-metadata.json", "IMG_2.jpg",
          "https://photos.google.com/photo/B")

    grupos, sin_json = _por_url(tmp_path)
    assert grupos == {
        "https://photos.google.com/photo/A": ["IMG_1.jpg"],
        "https://photos.google.com/photo/B": ["IMG_2.jpg"],
    }
    assert sin_json == []


def test_duplicados_con_numero(tmp_path):
    _archivo(tmp_path / "IMG.jpg")
    _archivo(tmp_path / "IMG(1).jpg")
    _archivo(tmp_path / "IMG(2).jpg")
    _json(tmp_path / "IMG.jpg.json", "IMG.jpg", "https://photos.google.com/photo/A")
    _json(tmp_path / "IMG.jpg(1).json", "IMG.jpg", "https://photos.google.com/photo/B")
    _json(tmp_path / "IMG.jpg.supplemental-metadata(2).json", "IMG.jpg",
          "https://photos.google.com/photo/C")

    grupos, _ = _por_url(tmp_path)
    assert grupos["https://photos.google.com/photo/A"] == ["IMG.jpg"]
    assert grupos["https://photos.google.com/photo/B"] == ["IMG(1).jpg"]
    assert grupos["https://photos.google.com/photo/C"] == ["IMG(2).jpg"]


def test_editada_y_live_photo(tmp_path):
    _archivo(tmp_path / "IMG_3.HEIC")
    _archivo(tmp_path / "IMG_3.MOV")
    _archivo(tmp_path / "IMG_4.jpg")
    _archivo(tmp_path / "IMG_4-editado.jpg")
    _json(tmp_path / "IMG_3.HEIC.json", "IMG_3.HEIC", "https://photos.google.com/photo/A")
    _json(tmp_path / "IMG_4.jpg.json", "IMG_4.jpg", "https://photos.google.com/photo/B")

    grupos, sin_json = _por_url(tmp_path)
    assert grupos["https://photos.google.com/photo/A"] == ["IMG_3.HEIC", "IMG_3.MOV"]
    assert grupos["https://photos.google.com/photo/B"] == ["IMG_4-editado.jpg", "IMG_4.jpg"]
    assert sin_json == []


def test_video_con_json_propio_no_se_asocia_a_la_foto(tmp_path):
    _archivo(tmp_path / "VID.jpg")
    _archivo(tmp_path / "VID.mp4")
    _json(tmp_path / "VID.jpg.json", "VID.jpg", "https://photos.google.com/photo/A")
    _json(tmp_path / "VID.mp4.json", "VID.mp4", "https://photos.google.com/photo/B")

    grupos, _ = _por_url(tmp_path)
    assert grupos["https://photos.google.com/photo/A"] == ["VID.jpg"]
    assert grupos["https://photos.google.com/photo/B"] == ["VID.mp4"]


def test_nombre_recortado(tmp_path):
    titulo = "Screenshot_20230101-123456_Una_aplicacion_con_nombre_largo.jpg"
    recortado = "Screenshot_20230101-123456_Una_aplicacion_co.jpg"
    _archivo(tmp_path / recortado)
    _json(tmp_path / "Screenshot_20230101-123456_Una_aplicacion_c.json", titulo,
          "https://photos.google.com/photo/A")

    grupos, sin_json = _por_url(tmp_path)
    assert grupos["https://photos.google.com/photo/A"] == [recortado]
    assert sin_json == []


def test_nombre_corto_no_usa_prefijo(tmp_path):
    _archivo(tmp_path / "IMG_123.jpg")
    _json(tmp_path / "IMG_1234.jpg.json", "IMG_1234.jpg", "https://photos.google.com/photo/A")

    grupos, sin_json = _por_url(tmp_path)
    assert grupos["https://photos.google.com/photo/A"] == []
    assert sin_json == ["IMG_123.jpg"]


def test_misma_foto_en_album_y_en_anio(tmp_path):
    anio = tmp_path / "Photos from 2020"
    album = tmp_path / "Vacaciones"
    anio.mkdir()
    album.mkdir()
    for carpeta in (anio, album):
        _archivo(carpeta / "IMG_5.jpg")
        _json(carpeta / "IMG_5.jpg.json", "IMG_5.jpg", "https://photos.google.com/photo/A")
    _json(album / "metadata.json", "Vacaciones", "")

    grupos, sin_json = escanear(tmp_path)
    assert len(grupos) == 1
    assert len(grupos[0].archivos) == 2
    assert sin_json == []


def test_archivo_sin_json(tmp_path):
    _archivo(tmp_path / "suelto.jpg")
    (tmp_path / "notas.txt").write_text("no es foto")

    grupos, sin_json = _por_url(tmp_path)
    assert grupos == {}
    assert sin_json == ["suelto.jpg"]
