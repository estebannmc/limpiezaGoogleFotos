"""Verificación de que cada archivo local está completo y se puede abrir."""

from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .takeout import EXT_VIDEO

OK = "ok"
PARCIAL = "parcial"  # el archivo existe pero no se pudo comprobar su contenido
ERROR = "error"
SIN_ARCHIVO = "sin_archivo"

EXT_PILLOW = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
EXT_HEIF = {".heic", ".heif", ".avif"}

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_DISPONIBLE = True
except ImportError:
    _HEIF_DISPONIBLE = False


@dataclass(frozen=True)
class Resultado:
    estado: str
    detalle: str = ""


def verificar_archivo(ruta: Path) -> Resultado:
    try:
        tamano = ruta.stat().st_size
    except OSError as e:
        return Resultado(ERROR, f"No se puede leer: {e}")
    if tamano == 0:
        return Resultado(ERROR, "Archivo vacío")

    ext = ruta.suffix.lower()
    if ext in EXT_PILLOW or (ext in EXT_HEIF and _HEIF_DISPONIBLE):
        return _verificar_imagen(ruta)
    if ext in EXT_HEIF:
        return Resultado(PARCIAL, "Falta pillow-heif para verificar HEIC")
    if ext in EXT_VIDEO:
        return _verificar_video(ruta)
    return Resultado(PARCIAL, "Formato sin verificación de contenido (solo tamaño)")


def _verificar_imagen(ruta: Path) -> Resultado:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # panorámicas grandes no son un error
    try:
        with Image.open(ruta) as imagen:
            imagen.verify()
        # verify() no decodifica los píxeles: load() detecta archivos cortados.
        with Image.open(ruta) as imagen:
            imagen.load()
    except Exception as e:
        return Resultado(ERROR, f"Imagen dañada: {e}")
    return Resultado(OK)


def _verificar_video(ruta: Path) -> Resultado:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return Resultado(PARCIAL, "ffprobe no instalado: video verificado solo por tamaño")
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(ruta)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Resultado(ERROR, f"ffprobe falló: {e}")
    if r.returncode != 0:
        return Resultado(ERROR, f"Video dañado: {_primera_linea(r.stderr)}")
    try:
        duracion = float(r.stdout.split()[0])
    except (IndexError, ValueError):
        return Resultado(PARCIAL, "No se pudo leer la duración del video")
    if duracion <= 0:
        return Resultado(ERROR, "Video sin duración")

    # Decodificar los últimos segundos detecta videos cortados a la mitad.
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return Resultado(PARCIAL, "ffmpeg no instalado: no se comprobó el final del video")
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-sseof", "-2", "-i", str(ruta), "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Resultado(ERROR, f"ffmpeg falló: {e}")
    if r.returncode != 0 or r.stderr.strip():
        return Resultado(ERROR, f"Final del video dañado: {_primera_linea(r.stderr)}")
    return Resultado(OK)


def _primera_linea(texto: str) -> str:
    texto = texto.strip()
    return texto.splitlines()[0][:200] if texto else "sin detalle"


def evaluar_grupo(archivos: list[Path], resultados: dict[Path, Resultado]) -> Resultado:
    """Combina los resultados de todos los archivos de un elemento.

    Cada nombre de archivo distinto (original, editado, video de Live Photo)
    necesita al menos una copia verificada; si la misma foto está en varias
    carpetas alcanza con que una de las copias esté bien.
    """
    if not archivos:
        return Resultado(SIN_ARCHIVO, "No se encontró el archivo local")

    por_nombre: dict[str, list[Resultado]] = defaultdict(list)
    for archivo in archivos:
        por_nombre[archivo.name.lower()].append(resultados[archivo])

    estado = OK
    detalles = []
    for nombre, lista in sorted(por_nombre.items()):
        estados = {r.estado for r in lista}
        if OK in estados:
            continue
        peor = ERROR if ERROR in estados else PARCIAL
        motivo = next(r.detalle for r in lista if r.estado == peor)
        detalles.append(f"{nombre}: {motivo}")
        if peor == ERROR:
            estado = ERROR
        elif estado == OK:
            estado = PARCIAL
    return Resultado(estado, "; ".join(detalles))
