"""Archivos CSV del reporte de verificación y del registro de eliminaciones.

Se usa ";" como separador para que Excel en español los abra en columnas.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SEPARADOR = ";"
CODIFICACION = "utf-8-sig"

CAMPOS_REPORTE = ["estado", "titulo", "url", "detalle", "archivos"]
CAMPOS_REGISTRO = ["fecha", "resultado", "titulo", "url", "detalle"]

# Resultados del registro que no hace falta volver a intentar.
RESULTADOS_FINALES = {"eliminado", "no_encontrado", "ya_en_papelera"}


@dataclass
class Fila:
    url: str
    titulo: str
    estado: str
    detalle: str
    archivos: list[tuple[str, int]]  # (ruta, tamaño en bytes)


def escribir_reporte(ruta: Path, filas: list[Fila]) -> None:
    with open(ruta, "w", newline="", encoding=CODIFICACION) as f:
        w = csv.DictWriter(f, CAMPOS_REPORTE, delimiter=SEPARADOR)
        w.writeheader()
        for fila in filas:
            w.writerow({
                "estado": fila.estado,
                "titulo": fila.titulo,
                "url": fila.url,
                "detalle": fila.detalle,
                "archivos": json.dumps(fila.archivos, ensure_ascii=False),
            })


def leer_reporte(ruta: Path) -> list[Fila]:
    with open(ruta, newline="", encoding=CODIFICACION) as f:
        return [
            Fila(
                url=r["url"],
                titulo=r["titulo"],
                estado=r["estado"],
                detalle=r["detalle"],
                archivos=[(a, int(t)) for a, t in json.loads(r["archivos"])],
            )
            for r in csv.DictReader(f, delimiter=SEPARADOR)
        ]


def escribir_lista(ruta: Path, rutas: list[Path]) -> None:
    with open(ruta, "w", newline="", encoding=CODIFICACION) as f:
        w = csv.writer(f, delimiter=SEPARADOR)
        w.writerow(["archivo"])
        w.writerows([str(r)] for r in rutas)


def registrar(ruta: Path, resultado: str, fila: Fila, detalle: str = "") -> None:
    nuevo = not ruta.exists()
    with open(ruta, "a", newline="", encoding=CODIFICACION) as f:
        w = csv.DictWriter(f, CAMPOS_REGISTRO, delimiter=SEPARADOR)
        if nuevo:
            w.writeheader()
        w.writerow({
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "resultado": resultado,
            "titulo": fila.titulo,
            "url": fila.url,
            "detalle": detalle,
        })


def urls_procesadas(ruta: Path) -> set[str]:
    return urls_con_resultado(ruta, RESULTADOS_FINALES)


def urls_con_resultado(ruta: Path, resultados: set[str]) -> set[str]:
    if not ruta.exists():
        return set()
    with open(ruta, newline="", encoding=CODIFICACION) as f:
        return {
            r["url"]
            for r in csv.DictReader(f, delimiter=SEPARADOR)
            if r["resultado"] in resultados
        }
