"""Lectura de una exportación de Google Takeout (Google Fotos).

Takeout guarda, junto a cada foto o video, un archivo JSON con sus metadatos.
Ese JSON trae el título original y la URL del elemento en Google Fotos
(``https://photos.google.com/photo/...``), que es lo que permite relacionar
cada archivo local con su copia en la nube.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

EXT_IMAGEN = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw",
}
EXT_VIDEO = {
    ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".wmv",
}
EXT_MEDIOS = EXT_IMAGEN | EXT_VIDEO

# Sufijo que Takeout agrega a la versión editada de una foto, según el idioma.
SUFIJOS_EDITADO = (
    "edited", "editado", "editada", "bearbeitet", "modifié", "modificato", "bewerkt",
)

# "IMG.jpg(1).json" o "IMG.jpg.supplemental-metadata(1).json" -> "IMG(1).jpg"
_RE_DUPLICADO = re.compile(r"\((\d+)\)\.json$", re.IGNORECASE)
_RE_STEM_DUPLICADO = re.compile(r"^(.*)\((\d+)\)$")

# Takeout recorta los nombres largos; solo se busca por prefijo si el título
# original supera este largo y el prefijo encontrado no es demasiado corto.
_LARGO_TITULO_RECORTABLE = 40
_LARGO_MINIMO_PREFIJO = 30


@dataclass
class GrupoFoto:
    """Un elemento de Google Fotos y todos sus archivos locales."""

    url: str
    titulo: str
    archivos: list[Path] = field(default_factory=list)


def escanear(raiz: Path) -> tuple[list[GrupoFoto], list[Path]]:
    """Recorre la carpeta de Takeout.

    Devuelve los elementos de Google Fotos encontrados (agrupados por URL, ya
    que la misma foto puede aparecer en la carpeta del año y en la de un
    álbum) y la lista de archivos multimedia que no se pudieron asociar a
    ningún JSON.
    """
    grupos: dict[str, GrupoFoto] = {}
    sin_json: list[Path] = []

    for carpeta, _, nombres in os.walk(raiz):
        carpeta = Path(carpeta)
        medios = {
            n.lower(): carpeta / n
            for n in nombres
            if Path(n).suffix.lower() in EXT_MEDIOS
        }
        reclamados: set[str] = set()
        encontrados: list[tuple[GrupoFoto, list[str]]] = []

        for nombre in sorted(nombres):
            if not nombre.lower().endswith(".json"):
                continue
            meta = _leer_json(carpeta / nombre)
            if meta is None:
                continue
            url, titulo = meta
            claves = _archivos_de_json(nombre, titulo, medios)
            reclamados.update(claves)
            grupo = grupos.setdefault(url, GrupoFoto(url, titulo))
            encontrados.append((grupo, claves))

        # Versiones editadas y videos de Live Photos no tienen JSON propio.
        for grupo, claves in encontrados:
            for clave in list(claves):
                for extra in _companeros(clave, medios):
                    if extra not in reclamados:
                        reclamados.add(extra)
                        claves.append(extra)
            grupo.archivos.extend(medios[c] for c in claves)

        sin_json.extend(medios[c] for c in sorted(medios) if c not in reclamados)

    return list(grupos.values()), sin_json


def _leer_json(ruta: Path) -> tuple[str, str] | None:
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict):
        return None
    url = datos.get("url")
    titulo = datos.get("title")
    if not isinstance(url, str) or "/photo/" not in url:
        return None
    if not isinstance(titulo, str) or not titulo:
        return None
    return url, titulo


def _archivos_de_json(nombre_json: str, titulo: str, medios: dict[str, Path]) -> list[str]:
    stem, ext = os.path.splitext(titulo)
    duplicado = _RE_DUPLICADO.search(nombre_json)
    candidato = f"{stem}({duplicado.group(1)}){ext}" if duplicado else titulo
    clave = candidato.lower()
    if clave in medios:
        return [clave]

    if not ext:
        sin_ext = [k for k in medios if os.path.splitext(k)[0] == clave]
        return sin_ext if len(sin_ext) == 1 else []

    if duplicado is None and len(titulo) > _LARGO_TITULO_RECORTABLE:
        stem_l, ext_l = stem.lower(), ext.lower()
        prefijos = [
            k for k in medios
            if os.path.splitext(k)[1] == ext_l
            and len(os.path.splitext(k)[0]) >= _LARGO_MINIMO_PREFIJO
            and stem_l.startswith(os.path.splitext(k)[0])
        ]
        if len(prefijos) == 1:
            return prefijos
    return []


def _companeros(clave: str, medios: dict[str, Path]) -> list[str]:
    stem, ext = os.path.splitext(clave)
    candidatos = [f"{stem}-{suf}{ext}" for suf in SUFIJOS_EDITADO]
    duplicado = _RE_STEM_DUPLICADO.match(stem)
    if duplicado:
        base, n = duplicado.groups()
        candidatos += [f"{base}-{suf}({n}){ext}" for suf in SUFIJOS_EDITADO]
    if ext in EXT_IMAGEN:
        candidatos += [stem + ".mp4", stem + ".mov"]
    return [c for c in candidatos if c in medios]
