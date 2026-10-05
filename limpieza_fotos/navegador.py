"""Control de Chrome para eliminar fotos en Google Fotos.

La API de Google Fotos no permite borrar elementos (ni, desde 2025, listar la
biblioteca completa), así que la eliminación se hace en la web, como lo haría
una persona: se abre la foto por su URL y se la manda a la papelera.

Chrome se abre con un perfil propio (separado del perfil habitual) y con el
puerto de depuración activo; Playwright se conecta a esa ventana. Así el
inicio de sesión en Google es el de un Chrome normal y queda guardado para las
siguientes ejecuciones.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

URL_INICIO = "https://photos.google.com/"
HOST_FOTOS = "photos.google.com"

# Botón de la barra superior de la foto (aria-label según el idioma).
RE_BOTON_ELIMINAR = re.compile(
    r"^(Delete|Eliminar|Borrar|Move to trash|Mover a la papelera)$", re.IGNORECASE
)
# Botón del diálogo de confirmación.
RE_CONFIRMAR = re.compile(
    r"^(Move to trash|Mover a la papelera|Enviar a la papelera|Trasladar a la papelera"
    r"|Delete|Eliminar|Borrar)$",
    re.IGNORECASE,
)
SELECTOR_DIALOGO = '[role="dialog"], [role="alertdialog"]'


class SesionCerrada(RuntimeError):
    """Google pidió iniciar sesión en medio del proceso."""


def perfil_predeterminado() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "limpiezaGoogleFotos" / "chrome-perfil"


def buscar_chrome() -> Path | None:
    candidatos = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidatos.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for nombre in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
        encontrado = shutil.which(nombre)
        if encontrado:
            candidatos.append(Path(encontrado))
    return next((c for c in candidatos if c.exists()), None)


def id_foto(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


class GooglePhotos:
    def __init__(self, chrome: Path, perfil: Path, puerto: int = 9222):
        self.chrome = chrome
        self.perfil = perfil
        self.puerto = puerto
        self.page = None
        self._pw = None
        self._browser = None

    def __enter__(self) -> GooglePhotos:
        from playwright.sync_api import sync_playwright

        if not self._puerto_activo():
            self.perfil.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([
                str(self.chrome),
                f"--remote-debugging-port={self.puerto}",
                f"--user-data-dir={self.perfil}",
                "--no-first-run",
                "--no-default-browser-check",
                URL_INICIO,
            ])
            self._esperar_puerto()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.puerto}")
        contexto = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self.page = contexto.pages[0] if contexto.pages else contexto.new_page()
        return self

    def __exit__(self, *exc) -> None:
        # Solo se desconecta: la ventana de Chrome queda abierta.
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def _puerto_activo(self) -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.puerto}/json/version", timeout=1)
            return True
        except OSError:
            return False

    def _esperar_puerto(self, segundos: float = 30) -> None:
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            if self._puerto_activo():
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"Chrome no respondió en el puerto {self.puerto}. "
            "Cerrá las ventanas de Chrome abiertas con este perfil y volvé a intentar."
        )

    def _en_google_fotos(self) -> bool:
        return urlparse(self.page.url).netloc == HOST_FOTOS

    def asegurar_sesion(self) -> None:
        self.page.goto(URL_INICIO, wait_until="domcontentloaded")
        while not self._en_google_fotos():
            input(
                "\nIniciá sesión en Google Fotos en la ventana de Chrome que se abrió "
                "y después presioná Enter acá para continuar..."
            )
            self.page.goto(URL_INICIO, wait_until="domcontentloaded")

    def _abrir_foto(self, url: str) -> bool:
        """Abre la foto; devuelve False si Google Fotos no la encuentra."""
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        if not self._en_google_fotos():
            raise SesionCerrada("Google pidió iniciar sesión de nuevo")
        return id_foto(url) in self.page.url

    def _primer_visible(self, locator, segundos: float):
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            for elemento in locator.all():
                if elemento.is_visible():
                    return elemento
            self.page.wait_for_timeout(300)
        return None

    def _boton_eliminar(self):
        return self._primer_visible(
            self.page.get_by_role("button", name=RE_BOTON_ELIMINAR), segundos=15
        )

    def simular(self, url: str) -> tuple[str, str]:
        """Abre la foto y busca el botón de eliminar, sin tocarlo."""
        if not self._abrir_foto(url):
            return "no_encontrado", "Google Fotos no muestra este elemento"
        if self._boton_eliminar() is None:
            return "fallo", "No se encontró el botón de eliminar"
        return "listo", "Se encontró el botón de eliminar (no se tocó)"

    def eliminar(self, url: str) -> tuple[str, str]:
        if not self._abrir_foto(url):
            return "no_encontrado", "Google Fotos no muestra este elemento"
        boton = self._boton_eliminar()
        if boton is None:
            return "fallo", "No se encontró el botón de eliminar"
        boton.click()

        confirmar = self._primer_visible(
            self.page.locator(SELECTOR_DIALOGO).get_by_role("button", name=RE_CONFIRMAR),
            segundos=10,
        )
        if confirmar is None:
            self.page.keyboard.press("Escape")
            return "fallo", "No apareció el diálogo de confirmación"
        confirmar.click()

        try:
            confirmar.wait_for(state="hidden", timeout=10_000)
        except Exception:
            return "fallo", "El diálogo de confirmación no se cerró"
        self.page.wait_for_timeout(1000)
        return "eliminado", "Movido a la papelera de Google Fotos"
