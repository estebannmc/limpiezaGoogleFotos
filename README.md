# limpiezaGoogleFotos

Libera espacio en Google Fotos eliminando **solo** lo que ya está respaldado
en la PC con Google Takeout.

1. **`verificar`** recorre la carpeta de Takeout (por ejemplo `F:\Takeout`) y
   comprueba que cada foto o video se pueda abrir completo. Así detecta
   archivos vacíos, cortados o dañados. Cada archivo se relaciona con su foto
   en Google Fotos mediante el JSON que Takeout guarda al lado, que trae la
   URL del elemento (`https://photos.google.com/photo/...`). El resultado
   queda en `reporte.csv`.
2. **`eliminar`** abre una ventana de Chrome, entra a cada foto que dio **OK**
   y la manda a la papelera de Google Fotos, como lo haría una persona.
   Cada acción queda anotada en `eliminados.csv`.

> La API de Google Fotos no permite borrar fotos y, desde 2025, tampoco listar
> la biblioteca completa. Por eso la eliminación se hace en la web, con Chrome.

## Instalación (Windows)

Requiere Python 3.10 o superior y Google Chrome.

```powershell
cd limpiezaGoogleFotos
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Para verificar videos hace falta **FFmpeg**. Sin FFmpeg, los videos quedan
como "parcial" y no se eliminan:

```powershell
winget install Gyan.FFmpeg
```

Las fotos RAW (`.dng`, etc.) se verifican con `rawpy`, que se instala con
`requirements.txt`. Si no se pudo instalar, esas fotos quedan como "parcial".

No hace falta ejecutar `playwright install`, porque el script usa el Chrome
instalado.

Si Takeout sigue en archivos `.zip`, descomprimilos todos dentro de la misma
carpeta antes de empezar.

## Uso

### 1. Verificar la copia local

```powershell
python -m limpieza_fotos verificar "F:\Takeout"
```

Al terminar muestra un resumen por estado:

| Estado        | Significado                                                       | ¿Se elimina? |
|---------------|-------------------------------------------------------------------|--------------|
| `ok`          | Todos los archivos del elemento se abren completos                | Sí           |
| `parcial`     | El archivo existe pero su contenido no se pudo comprobar (video sin FFmpeg, RAW sin rawpy) | Solo con `--incluir-parcial` |
| `error`       | Algún archivo está vacío, cortado o dañado                        | No           |
| `sin_archivo` | Hay JSON pero no se encontró la foto en la carpeta                | No           |

Los archivos que no tienen JSON de Google Fotos se listan en `sin_json.csv` y
no se tocan.

### 2. Probar sin borrar nada (recomendado la primera vez)

```powershell
python -m limpieza_fotos eliminar --simulacro --limite 5
```

Se abre Chrome con un perfil propio, separado de tu Chrome habitual. La
primera vez tenés que **iniciar sesión en Google Fotos** en esa ventana y
después presionar Enter en la consola. La sesión queda guardada para las
siguientes veces.

El simulacro abre cada foto y comprueba que encuentra el botón de eliminar,
sin tocarlo.

### 3. Eliminar

```powershell
python -m limpieza_fotos eliminar --limite 20
```

Antes de empezar pide escribir `SI`. Por cada elemento:

- vuelve a comprobar que los archivos locales siguen igual que cuando se
  verificaron (si cambiaron, lo omite);
- abre la foto en Google Fotos y comprueba que es la misma URL;
- la manda a la papelera y lo anota en `eliminados.csv`.

Si lo volvés a ejecutar, sigue con lo que falta. Si hay 3 fallos seguidos
(por ejemplo, porque Google cambió su página) o Google pide volver a iniciar
sesión, el proceso se detiene.

Opciones útiles: `--limite N`, `--pausa 3` (segundos entre fotos),
`--incluir-parcial`, `--chrome "C:\ruta\chrome.exe"`.

## Si algo falla

- **Google no deja iniciar sesión** ("este navegador puede no ser seguro"):
  cerrá esa ventana de Chrome y abrila a mano con el mismo perfil, iniciá
  sesión y dejala abierta. Después ejecutá el script, que se conecta a esa
  ventana:

  ```powershell
  & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\limpiezaGoogleFotos\chrome-perfil" https://photos.google.com
  ```

- **"No se encontró el botón de eliminar"**: Google Fotos puede estar en
  otro idioma o haber cambiado su página. Copiá el mensaje y ajustá los
  textos en `limpieza_fotos/navegador.py`.
- **Muchos videos como `parcial`**: falta FFmpeg. Después de instalarlo,
  cerrá y volvé a abrir la consola, y ejecutá `verificar` otra vez.

## Precauciones

- Lo eliminado va a la **papelera de Google Fotos** y se puede recuperar
  durante 60 días.
- Takeout es tu única copia de esas fotos: conviene tener otra copia en
  otro disco o en otra nube.
- Si una foto está en un álbum compartido, al eliminarla también desaparece
  de ese álbum.
- La página de Google Fotos puede cambiar. Si el simulacro no encuentra el
  botón de eliminar, hay que ajustar los textos en
  `limpieza_fotos/navegador.py`.

## Desarrollo

```powershell
pip install -r requirements-dev.txt
python -m pytest
```
