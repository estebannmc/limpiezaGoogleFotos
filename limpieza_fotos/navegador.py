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

# Botón de la barra superior de la foto. Solo se acepta "Mover a la papelera":
# un "Eliminar" a secas podría ser el borrado definitivo de la vista de papelera.
RE_BOTON_ELIMINAR = re.compile(r"^(Mover a la papelera|Move to trash)$", re.IGNORECASE)
# Botones que aceptan un diálogo después de eliminar ("Entendido; continuar"...).
# Solo se buscan dentro de diálogos.
RE_CONFIRMAR = re.compile(
    r"entend|entiendo|continu|confirm|acept|understand|got it|papelera|trash|^ok$",
    re.IGNORECASE,
)
# Nunca se aprieta nada que cancele o que borre para siempre.
RE_PROHIBIDO = re.compile(
    r"cancel|definitiv|permanent|para siempre|forever|vaciar|empty", re.IGNORECASE
)
# Aviso que muestra Google Fotos cuando la foto ya se movió a la papelera.
RE_AVISO_PAPELERA = re.compile(
    r"(movid|movi[óo]|movieron|enviad|envi[óo]|moved|sent).{0,40}(papelera|trash)"
    r"|\b(deshacer|undo)\b",
    re.IGNORECASE,
)
RE_DESHACER = re.compile(r"^(deshacer|undo)$", re.IGNORECASE)

# Anota los textos que aparecen en la página (por ejemplo el aviso de abajo).
_JS_OBSERVAR_TEXTOS = """() => {
  window.__textosNuevos = [];
  if (window.__observadorTextos) window.__observadorTextos.disconnect();
  const anotar = t => { t = (t || '').trim().replace(/\\s+/g, ' ');
                        if (t) window.__textosNuevos.push(t.slice(0, 150)); };
  window.__observadorTextos = new MutationObserver(cambios => {
    for (const c of cambios) {
      if (c.type === 'characterData') anotar(c.target.textContent);
      for (const n of c.addedNodes) anotar(n.innerText || n.textContent);
    }
  });
  window.__observadorTextos.observe(document.body,
      {childList: true, subtree: true, characterData: true});
}"""
_JS_TEXTOS_NUEVOS = "() => (window.__textosNuevos || []).slice(-15)"
# Botón que aparece al ver una foto que ya está en la papelera.
RE_RESTAURAR = re.compile(r"restaur|restore", re.IGNORECASE)
SELECTOR_DIALOGO = '[role="dialog"], [role="alertdialog"], [aria-modal="true"]'
CARPETA_DIAGNOSTICO = Path("diagnostico")

# Lista los botones y diálogos visibles para entender la página cuando algo falla.
_JS_DIAGNOSTICO = """() => {
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const texto = (e, n) => (e.getAttribute('aria-label') || e.innerText || '')
      .trim().replace(/\\s+/g, ' ').slice(0, n);
  const botones = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible).map(e => texto(e, 50)).filter(Boolean);
  const dialogos = [...document.querySelectorAll(
      '[role="dialog"], [role="alertdialog"], [aria-modal="true"]')]
      .filter(visible).map(e => (e.getAttribute('role') || 'modal') + ': ' + texto(e, 150));
  return {botones: [...new Set(botones)].slice(0, 30), dialogos: dialogos.slice(0, 5)};
}"""


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


def url_en_papelera(url: str) -> str:
    """https://photos.google.com/photo/ID -> https://photos.google.com/trash/ID"""
    return f"https://{HOST_FOTOS}/trash/{id_foto(url)}"


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
        while True:
            for elemento in locator.all():
                if elemento.is_visible():
                    return elemento
            if time.monotonic() >= fin:
                return None
            self.page.wait_for_timeout(300)

    def _boton_eliminar(self):
        return self._primer_visible(
            self.page.get_by_role("button", name=RE_BOTON_ELIMINAR), segundos=15
        )

    def simular(self, url: str) -> tuple[str, str]:
        """Abre la foto y busca el botón de eliminar, sin tocarlo."""
        if not self._abrir_foto(url):
            return "no_encontrado", "Google Fotos no muestra este elemento"
        if self._en_papelera():
            return "ya_en_papelera", "La foto ya está en la papelera de Google Fotos"
        if self._boton_eliminar() is None:
            return "fallo", "No se encontró el botón de eliminar"
        return "listo", "Se encontró el botón de eliminar (no se tocó)"

    def eliminar(self, url: str) -> tuple[str, str]:
        from playwright.sync_api import Error as ErrorPlaywright

        if not self._abrir_foto(url):
            return "no_encontrado", "Google Fotos no muestra este elemento"
        if self._en_papelera():
            return "ya_en_papelera", "La foto ya está en la papelera de Google Fotos"
        boton = self._boton_eliminar()
        if boton is None:
            return "fallo", "No se encontró el botón de eliminar. " + self._diagnostico(url)

        try:
            self.page.evaluate(_JS_OBSERVAR_TEXTOS)
            boton.click(timeout=10_000)
            # Se aceptan los diálogos que aparezcan. Si Google da una señal clara
            # (cambia la URL o aparece el aviso con "Deshacer") no hace falta más.
            fin = time.monotonic() + 3
            while time.monotonic() < fin:
                self.page.wait_for_timeout(400)
                confirmar = self._boton_confirmar()
                if confirmar is not None:
                    confirmar.click(timeout=5_000)
                    fin = max(fin, time.monotonic() + 2)
                    continue
                if self._aviso_de_eliminacion(url):
                    self.page.wait_for_timeout(500)
                    return "eliminado", "Movido a la papelera de Google Fotos"
            textos = self._textos_nuevos()

            # Google suele mover la foto sin ningún aviso: se comprueba si ya
            # aparece en la papelera (solo se mira, no se aprieta nada ahí).
            if self._aparece_en_papelera(url):
                return "eliminado", "Movido a la papelera de Google Fotos (comprobado en la papelera)"
        except ErrorPlaywright as e:
            return "fallo", f"Error en la página: {str(e).splitlines()[0]}. " + self._diagnostico(url)

        return "fallo", (
            "La foto no aparece en la papelera de Google Fotos. "
            f"Textos nuevos en la página: {textos or 'ninguno'}. " + self._diagnostico(url)
        )

    def _aparece_en_papelera(self, url: str) -> bool:
        self.page.wait_for_timeout(1000)
        self.page.goto(url_en_papelera(url), wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        if not self._en_google_fotos():
            raise SesionCerrada("Google pidió iniciar sesión de nuevo")
        if id_foto(url) not in self.page.url:
            return False
        restaurar = self.page.get_by_role("button", name=RE_RESTAURAR)
        return self._primer_visible(restaurar, segundos=5) is not None

    def _aviso_de_eliminacion(self, url: str) -> bool:
        if id_foto(url) not in self.page.url:
            return True
        if any(RE_AVISO_PAPELERA.search(t) for t in self._textos_nuevos()):
            return True
        deshacer = self.page.get_by_role("button", name=RE_DESHACER)
        return self._primer_visible(deshacer, segundos=0) is not None

    def _textos_nuevos(self) -> list[str]:
        try:
            return self.page.evaluate(_JS_TEXTOS_NUEVOS)
        except Exception:
            return []

    def _en_papelera(self) -> bool:
        if "/trash" in self.page.url:
            return True
        restaurar = self.page.get_by_role("button", name=RE_RESTAURAR)
        return self._primer_visible(restaurar, segundos=1) is not None

    def _boton_confirmar(self):
        botones = self.page.locator(SELECTOR_DIALOGO).get_by_role("button", name=RE_CONFIRMAR)
        for elemento in botones.all():
            if elemento.is_visible() and not RE_PROHIBIDO.search(_nombre(elemento)):
                return elemento
        return None

    def _diagnostico(self, url: str) -> str:
        """Guarda una captura y resume los botones y diálogos visibles."""
        CARPETA_DIAGNOSTICO.mkdir(exist_ok=True)
        captura = CARPETA_DIAGNOSTICO / f"{id_foto(url)[:40]}.png"
        try:
            self.page.screenshot(path=str(captura))
            datos = self.page.evaluate(_JS_DIAGNOSTICO)
        except Exception as e:
            return f"(sin diagnóstico: {e})"
        return (
            f"Diálogos visibles: {datos['dialogos'] or 'ninguno'}. "
            f"Botones visibles: {datos['botones']}. URL: {self.page.url}. Captura: {captura}"
        )


def _nombre(elemento) -> str:
    try:
        return (elemento.get_attribute("aria-label") or elemento.inner_text() or "").strip()
    except Exception:
        return ""
