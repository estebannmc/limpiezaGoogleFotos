"""Verificación de que cada archivo local está completo y se puede abrir."""

from __future__ import annotations

import re
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
EXT_RAW = {".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw"}

# Mensajes de FFmpeg que indican un video cortado o dañado. Otros avisos
# (por ejemplo "non monotonically increasing dts" o "Could not find ref with
# POC", que salen al empezar a leer a mitad del video) no son un problema.
_RE_VIDEO_DANADO = re.compile(
    r"partial file|invalid data found|invalid nal unit size|error splitting the input"
    r"|moov atom not found|error reading header|truncat",
    re.IGNORECASE,
)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_DISPONIBLE = True
except ImportError:
    _HEIF_DISPONIBLE = False

try:
    import rawpy

    _RAWPY_DISPONIBLE = True
except ImportError:
    _RAWPY_DISPONIBLE = False

# Segunda opción para DNG: lee también los ProRAW comprimidos con JPEG XL de los
# iPhone recientes, que rawpy no siempre entiende.
try:
    import imagecodecs  # noqa: F401  (decodificadores que usa tifffile)
    import tifffile

    _TIFFFILE_DISPONIBLE = True
except ImportError:
    _TIFFFILE_DISPONIBLE = False


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
    if ext in EXT_RAW:
        # Google Fotos guarda en "Ahorro de espacio" las RAW convertidas a JPG
        # (u otro formato) pero con el nombre original: se mira el contenido.
        formato = formato_real(ruta)
        if formato in ("JPEG", "PNG", "WEBP") or (formato == "HEIF" and _HEIF_DISPONIBLE):
            resultado = _verificar_imagen(ruta)
            if resultado.estado == OK:
                return Resultado(OK, f"El archivo es {formato} aunque se llame {ext}")
            return resultado
    if ext in EXT_RAW and _RAWPY_DISPONIBLE:
        return _verificar_raw(ruta)
    if ext in EXT_RAW:
        return Resultado(PARCIAL, "Falta rawpy para verificar fotos RAW (solo tamaño)")
    if ext in EXT_VIDEO:
        return _verificar_video(ruta)
    return Resultado(PARCIAL, "Formato sin verificación de contenido (solo tamaño)")


def formato_real(ruta: Path) -> str:
    """Identifica el formato por los primeros bytes del archivo, no por su nombre."""
    try:
        with open(ruta, "rb") as f:
            cabecera = f.read(16)
    except OSError:
        return ""
    if cabecera.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if cabecera.startswith(b"\x89PNG"):
        return "PNG"
    if cabecera.startswith(b"RIFF") and cabecera[8:12] == b"WEBP":
        return "WEBP"
    if cabecera[4:8] == b"ftyp" and cabecera[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis",
    ):
        return "HEIF"
    if cabecera[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    return ""


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


def _verificar_raw(ruta: Path) -> Resultado:
    try:
        with rawpy.imread(str(ruta)) as raw:
            raw.raw_image_visible.max()  # obliga a leer todos los datos
        return Resultado(OK)
    except Exception as e:
        error_rawpy = e
    if ruta.suffix.lower() == ".dng" and _TIFFFILE_DISPONIBLE:
        try:
            _decodificar_dng(ruta)
            return Resultado(OK)
        except Exception as e:
            return Resultado(
                ERROR, f"RAW dañado ({_cabecera(ruta)}): {error_rawpy}; tifffile: {str(e)[:80]}"
            )
    return Resultado(ERROR, f"RAW dañado ({_cabecera(ruta)}): {error_rawpy}")


def _cabecera(ruta: Path) -> str:
    """Primeros bytes del archivo, para identificar formatos desconocidos."""
    try:
        with open(ruta, "rb") as f:
            return "primeros bytes " + f.read(12).hex(" ")
    except OSError:
        return "sin cabecera"


def _decodificar_dng(ruta: Path) -> None:
    """Decodifica todas las imágenes del DNG (la RAW y las vistas previas).

    Falla si no hay ninguna imagen principal (NewSubfileType 0): las vistas
    previas solas no prueban que la foto RAW esté completa.
    """
    principales = 0
    tamano = ruta.stat().st_size
    with tifffile.TiffFile(ruta) as dng:
        for pagina in dng.pages:
            for imagen in [pagina, *(pagina.pages or [])]:
                # tifffile rellena en silencio lo que falta de un archivo cortado,
                # así que se comprueba que todos los datos estén dentro del archivo.
                if any(o + c > tamano for o, c in zip(imagen.dataoffsets, imagen.databytecounts)):
                    raise ValueError("faltan datos de la imagen: el archivo está cortado")
                datos = imagen.asarray()
                if imagen.subfiletype == 0 and datos.size:
                    principales += 1
    if not principales:
        raise ValueError("el DNG no tiene imagen principal")


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
    if r.returncode != 0 or video_danado(r.stderr):
        motivo = next(
            (linea for linea in r.stderr.splitlines() if _RE_VIDEO_DANADO.search(linea)),
            _primera_linea(r.stderr),
        )
        return Resultado(ERROR, f"Final del video dañado: {motivo.strip()[:200]}")
    return Resultado(OK)


def video_danado(salida_ffmpeg: str) -> bool:
    return _RE_VIDEO_DANADO.search(salida_ffmpeg) is not None


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
