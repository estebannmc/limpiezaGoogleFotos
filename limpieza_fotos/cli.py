"""Línea de comandos: ``python -m limpieza_fotos verificar|eliminar``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import reporte, takeout, verificar
from .verificar import OK, PARCIAL

CARPETA_PREDETERMINADA = Path(r"F:\Takeout")
MAXIMO_FALLOS_SEGUIDOS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="limpieza_fotos",
        description="Verifica la copia local de Google Takeout y elimina de Google Fotos "
                    "lo que ya está respaldado en la PC.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    v = sub.add_parser("verificar", help="Revisa los archivos de Takeout y genera el reporte.")
    v.add_argument("carpeta", type=Path, nargs="?", default=CARPETA_PREDETERMINADA,
                   help=f"Carpeta de Takeout (por defecto {CARPETA_PREDETERMINADA}).")
    v.add_argument("--reporte", type=Path, default=Path("reporte.csv"))
    v.add_argument("--hilos", type=int, default=os.cpu_count() or 4)

    e = sub.add_parser("eliminar", help="Abre Chrome y manda a la papelera lo verificado.")
    e.add_argument("--reporte", type=Path, default=Path("reporte.csv"))
    e.add_argument("--registro", type=Path, default=Path("eliminados.csv"))
    e.add_argument("--limite", type=int, default=20,
                   help="Cantidad máxima de elementos por ejecución (por defecto 20).")
    e.add_argument("--simulacro", action="store_true",
                   help="Abre cada foto y busca el botón de eliminar, pero no borra nada.")
    e.add_argument("--incluir-parcial", action="store_true",
                   help="También elimina elementos con verificación parcial (solo tamaño).")
    e.add_argument("--chrome", type=Path, help="Ruta a chrome.exe si no se detecta sola.")
    e.add_argument("--perfil", type=Path, help="Carpeta del perfil de Chrome para este script.")
    e.add_argument("--puerto", type=int, default=9222)
    e.add_argument("--pausa", type=float, default=2.0,
                   help="Segundos de espera entre elementos (por defecto 2).")

    args = parser.parse_args(argv)
    if args.comando == "verificar":
        return comando_verificar(args)
    return comando_eliminar(args)


def comando_verificar(args) -> int:
    if not args.carpeta.is_dir():
        print(f"No existe la carpeta {args.carpeta}", file=sys.stderr)
        return 1

    print(f"Buscando fotos en {args.carpeta} ...")
    grupos, sin_json = takeout.escanear(args.carpeta)
    archivos = sorted({a for g in grupos for a in g.archivos})
    print(f"{len(grupos)} elementos de Google Fotos, {len(archivos)} archivos para verificar.")

    resultados: dict[Path, verificar.Resultado] = {}
    with ThreadPoolExecutor(max_workers=args.hilos) as ejecutor:
        for i, (ruta, res) in enumerate(
            zip(archivos, ejecutor.map(verificar.verificar_archivo, archivos)), start=1
        ):
            resultados[ruta] = res
            if i % 200 == 0 or i == len(archivos):
                print(f"  verificados {i}/{len(archivos)}", flush=True)

    filas = []
    for grupo in grupos:
        res = verificar.evaluar_grupo(grupo.archivos, resultados)
        filas.append(reporte.Fila(
            url=grupo.url,
            titulo=grupo.titulo,
            estado=res.estado,
            detalle=res.detalle,
            archivos=[(str(a), _tamano(a)) for a in grupo.archivos],
        ))
    reporte.escribir_reporte(args.reporte, filas)

    conteo = Counter(f.estado for f in filas)
    print(f"\nReporte guardado en {args.reporte}")
    for estado in (OK, PARCIAL, verificar.ERROR, verificar.SIN_ARCHIVO):
        print(f"  {estado:12} {conteo.get(estado, 0)}")
    if sin_json:
        ruta_sin_json = args.reporte.with_name("sin_json.csv")
        reporte.escribir_lista(ruta_sin_json, sin_json)
        print(f"  {len(sin_json)} archivos sin JSON de Google Fotos (ver {ruta_sin_json}); "
              "no se tocan.")
    return 0


def comando_eliminar(args) -> int:
    from .navegador import GooglePhotos, SesionCerrada, buscar_chrome, perfil_predeterminado

    if not args.reporte.exists():
        print(f"No existe {args.reporte}. Primero ejecutá: python -m limpieza_fotos verificar",
              file=sys.stderr)
        return 1

    estados_validos = {OK} | ({PARCIAL} if args.incluir_parcial else set())
    procesadas = reporte.urls_procesadas(args.registro)
    pendientes = [
        f for f in reporte.leer_reporte(args.reporte)
        if f.estado in estados_validos and f.url not in procesadas
    ][: args.limite]
    if not pendientes:
        print("No hay elementos verificados pendientes de eliminar.")
        return 0

    chrome = args.chrome or buscar_chrome()
    if chrome is None or not chrome.exists():
        print("No se encontró Chrome. Indicá la ruta con --chrome", file=sys.stderr)
        return 1

    if args.simulacro:
        print(f"SIMULACRO: se van a revisar {len(pendientes)} elementos sin borrar nada.")
    else:
        print(f"Se van a MOVER A LA PAPELERA de Google Fotos {len(pendientes)} elementos "
              "cuya copia local fue verificada.")
        print("La papelera de Google Fotos los conserva 60 días antes de borrarlos del todo.")
        if input('Escribí "SI" para continuar: ').strip().upper() != "SI":
            print("Cancelado.")
            return 1

    conteo: Counter[str] = Counter()
    fallos_seguidos = 0
    with GooglePhotos(chrome, args.perfil or perfil_predeterminado(), args.puerto) as fotos:
        fotos.asegurar_sesion()
        for i, fila in enumerate(pendientes, start=1):
            problema = _archivos_cambiados(fila)
            if problema:
                resultado, detalle = "omitido", problema
            else:
                try:
                    accion = fotos.simular if args.simulacro else fotos.eliminar
                    resultado, detalle = accion(fila.url)
                except SesionCerrada as ex:
                    print(f"\n{ex}. Se detiene el proceso.", file=sys.stderr)
                    break

            conteo[resultado] += 1
            print(f"[{i}/{len(pendientes)}] {resultado:13} {fila.titulo}  {detalle}")
            if not args.simulacro:
                reporte.registrar(args.registro, resultado, fila, detalle)

            fallos_seguidos = fallos_seguidos + 1 if resultado == "fallo" else 0
            if fallos_seguidos >= MAXIMO_FALLOS_SEGUIDOS:
                print(f"\n{MAXIMO_FALLOS_SEGUIDOS} fallos seguidos: es probable que Google Fotos "
                      "haya cambiado su página. Se detiene el proceso.", file=sys.stderr)
                break
            time.sleep(args.pausa)

    print("\nResumen: " + ", ".join(f"{k}={v}" for k, v in sorted(conteo.items())))
    if not args.simulacro:
        print(f"Registro guardado en {args.registro}")
    return 0


def _archivos_cambiados(fila: reporte.Fila) -> str:
    """Vuelve a comprobar que los archivos locales siguen igual que al verificar."""
    for ruta, tamano in fila.archivos:
        actual = _tamano(Path(ruta))
        if actual != tamano:
            return f"El archivo local cambió o ya no está: {ruta}. Volvé a ejecutar verificar."
    return ""


def _tamano(ruta: Path) -> int:
    try:
        return ruta.stat().st_size
    except OSError:
        return -1
