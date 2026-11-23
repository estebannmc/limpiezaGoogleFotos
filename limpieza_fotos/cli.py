"""Línea de comandos: ``python -m limpieza_fotos verificar|eliminar|inventario``."""

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
    # Títulos con emojis no deben cortar el proceso en consolas de Windows.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(errors="replace")

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

    n = sub.add_parser(
        "inventario",
        help="Lista lo que queda en Google Fotos y lo compara con los Takeout (no borra nada).",
    )
    n.add_argument("--reporte", type=Path, action="append",
                   help="Reporte de un Takeout; se puede repetir. "
                        "Por defecto, reporte.csv y reporte2.csv si existen.")
    n.add_argument("--registro", type=Path, default=Path("eliminados.csv"))
    n.add_argument("--salida", type=Path, default=Path("inventario.csv"))
    n.add_argument("--sin-archivo-google", action="store_true",
                   help="No revisar la sección Archivo de Google Fotos.")
    n.add_argument("--chrome", type=Path, help="Ruta a chrome.exe si no se detecta sola.")
    n.add_argument("--perfil", type=Path, help="Carpeta del perfil de Chrome para este script.")
    n.add_argument("--puerto", type=int, default=9222)

    args = parser.parse_args(argv)
    if args.comando == "verificar":
        return comando_verificar(args)
    if args.comando == "inventario":
        return comando_inventario(args)
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
    from playwright.sync_api import Error as ErrorPlaywright

    from .navegador import GooglePhotos, SesionCerrada, buscar_chrome, perfil_predeterminado

    if not args.reporte.exists():
        print(f"No existe {args.reporte}. Primero ejecutá: python -m limpieza_fotos verificar",
              file=sys.stderr)
        return 1

    estados_validos = {OK} | ({PARCIAL} if args.incluir_parcial else set())
    procesadas = reporte.urls_procesadas(args.registro)
    fallidas = reporte.urls_con_resultado(args.registro, {"fallo"})
    pendientes = [
        f for f in reporte.leer_reporte(args.reporte)
        if f.estado in estados_validos and f.url not in procesadas
    ]
    # Lo que ya falló se reintenta al final, para que no frene al resto.
    pendientes.sort(key=lambda f: f.url in fallidas)
    pendientes = pendientes[: args.limite]
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
        print("Se pueden restaurar desde la papelera de Google Fotos durante el plazo que "
              "indica Google (hoy, 30 días).")
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
                except ErrorPlaywright as ex:
                    # Una página que no carga (por ejemplo, por la conexión) no
                    # corta todo el proceso: se anota y se sigue con la próxima.
                    resultado = "fallo"
                    detalle = f"Error del navegador: {str(ex).splitlines()[0]}"

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


SIGUE_PESE_A_ELIMINADO = "figura como eliminada pero sigue en Google"
FALTA_BORRAR = "verificada en un Takeout: falta borrarla"
DANADA = "dañada en los Takeout"
SIN_FOTO = "el Takeout trajo el JSON sin la foto"
SIN_VERIFICAR = "en un Takeout, sin verificar del todo"
NO_ESTA = "no aparece en ningún Takeout"


def clasificar(resultado_registro: str | None, estados: list[tuple[str, str]]) -> str:
    """Explica por qué una foto sigue en Google Fotos.

    estados: (nombre del reporte, estado) de cada Takeout donde aparece.
    """
    if resultado_registro == "eliminado":
        return SIGUE_PESE_A_ELIMINADO
    if not estados:
        return NO_ESTA
    vistos = {estado for _, estado in estados}
    if OK in vistos:
        return FALTA_BORRAR
    if verificar.ERROR in vistos:
        return DANADA
    if PARCIAL in vistos:
        return SIN_VERIFICAR
    return SIN_FOTO


def comando_inventario(args) -> int:
    from .navegador import (
        URL_ARCHIVO, URL_INICIO, GooglePhotos, SesionCerrada, buscar_chrome, id_foto,
        perfil_predeterminado,
    )

    reportes = args.reporte or [p for p in (Path("reporte.csv"), Path("reporte2.csv")) if p.exists()]
    estados: dict[str, list[tuple[str, str]]] = {}
    for ruta in reportes:
        if not ruta.exists():
            print(f"No existe {ruta}", file=sys.stderr)
            return 1
        for fila in reporte.leer_reporte(ruta):
            estados.setdefault(id_foto(fila.url), []).append((ruta.stem, fila.estado))
    registro = {id_foto(u): fila for u, fila in reporte.ultimos_resultados(args.registro).items()}
    print(f"Comparando con: {', '.join(str(r) for r in reportes) or 'ningún reporte'}")

    chrome = args.chrome or buscar_chrome()
    if chrome is None or not chrome.exists():
        print("No se encontró Chrome. Indicá la ruta con --chrome", file=sys.stderr)
        return 1

    paginas = [("Fotos", URL_INICIO)]
    if not args.sin_archivo_google:
        paginas.append(("Archivo", URL_ARCHIVO))
    en_google: dict[str, tuple[str, str]] = {}
    try:
        with GooglePhotos(chrome, args.perfil or perfil_predeterminado(), args.puerto) as fotos:
            fotos.asegurar_sesion()
            for donde, url in paginas:
                print(f"Recorriendo {donde} en Google Fotos (solo se lee, no se borra nada)...")
                for id_, descripcion in fotos.listar(url).items():
                    en_google.setdefault(id_, (descripcion, donde))
    except SesionCerrada as ex:
        print(f"\n{ex}. Se detiene el proceso.", file=sys.stderr)
        return 1

    filas = []
    reabiertas = 0
    for id_, (descripcion, donde) in en_google.items():
        lista = estados.get(id_, [])
        registrada = registro.get(id_)
        situacion = clasificar(registrada["resultado"] if registrada else None, lista)
        filas.append({
            "situacion": situacion,
            "descripcion": descripcion,
            "donde": donde,
            "en_takeouts": ", ".join(f"{nombre}={estado}" for nombre, estado in lista),
            "registro": (
                f"{registrada['fecha']} {registrada['resultado']}: {registrada['detalle'][:150]}"
                if registrada else ""
            ),
            "url": f"https://photos.google.com/photo/{id_}",
        })
        if situacion == SIGUE_PESE_A_ELIMINADO:
            # Se vuelve a dejar pendiente para que "eliminar" la intente de nuevo.
            fila = reporte.Fila(registrada["url"], registrada["titulo"], "", "", [])
            reporte.registrar(args.registro, "sigue_en_google", fila,
                              "El inventario la encontró en Google: el borrado no se completó")
            reabiertas += 1
    filas.sort(key=lambda f: (f["situacion"], f["descripcion"]))
    reporte.escribir_inventario(args.salida, filas)

    print(f"\n{len(filas)} fotos siguen en Google Fotos. Detalle en {args.salida}")
    for situacion, cantidad in Counter(f["situacion"] for f in filas).most_common():
        print(f"  {cantidad:6}  {situacion}")
    if reabiertas:
        print(f"\n{reabiertas} fotos que figuraban como eliminadas quedaron pendientes otra vez "
              f"en {args.registro}: 'eliminar' las va a reintentar.")
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
