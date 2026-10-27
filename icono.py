"""Convierte una imagen (SVG, PNG, JPG, WEBP, ICO…) en el icono de la app (icono.ico).

    python icono.py ruta\\a\\imagen.png     usa esa imagen
    python icono.py                       busca logo.svg / logo.png / logo.jpg… junto a este archivo
    python icono.py original              vuelve al icono que trae la app

Recorta los márgenes transparentes, la centra en un cuadrado y genera todos los tamaños
que usa Windows (16 a 256 px). Requiere Pillow, y resvg-py para SVG
(pip install pillow resvg-py).
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICO = HERE / "icono.ico"
BACKUP = HERE / "icono_original.ico"
SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def find_source(arg: str | None) -> Path | None:
    if arg:
        p = Path(arg.strip('"'))
        return p if p.exists() else None
    for name in ("logo.svg", "logo.svgz", "logo.png", "logo.jpg", "logo.jpeg", "logo.webp", "logo.ico", "logo.bmp"):
        p = HERE / name
        if p.exists():
            return p
    return None


def render_svg(path: Path):
    """SVG -> imagen RGBA de 1024 px en su lado mayor, con fondo transparente."""
    import io

    import resvg_py
    from PIL import Image

    # Primero a tamaño natural para conocer la proporción; luego al tamaño final.
    data = resvg_py.svg_to_bytes(svg_path=str(path))
    im = Image.open(io.BytesIO(bytes(data)))
    w, h = im.size
    if not w or not h:
        raise ValueError("el SVG no tiene tamaño")
    scale = 1024 / max(w, h)
    data = resvg_py.svg_to_bytes(svg_path=str(path), zoom=scale)
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    if arg and arg.lower() == "original":
        if BACKUP.exists():
            shutil.copyfile(BACKUP, ICO)
            print("Restaurado el icono original.")
            return 0
        print("No hay copia del icono original: ya estás usando el de la app.")
        return 0

    src = find_source(arg)
    if not src:
        print("No encuentro la imagen. Guárdala como 'logo.png' en esta carpeta,")
        print("o arrástrala encima de cambiar_icono.bat.")
        return 1

    try:
        from PIL import Image
    except ImportError:
        print("Falta Pillow: ejecuta  python -m pip install pillow")
        return 1

    if src.suffix.lower() in (".svg", ".svgz"):
        try:
            im = render_svg(src)
        except ImportError:
            print("Falta resvg-py para leer SVG: ejecuta  python -m pip install resvg-py")
            return 1
        except Exception as e:
            print(f"No se pudo convertir el SVG: {e}")
            return 1
    else:
        im = Image.open(src)
    if getattr(im, "n_frames", 1) > 1:  # ICO o GIF con varias imágenes: la mayor
        im.seek(0)
    im = im.convert("RGBA")

    # Recorta el borde transparente (o el fondo blanco uniforme si no hay transparencia)
    bbox = im.getbbox()
    if bbox and bbox != (0, 0, *im.size):
        im = im.crop(bbox)

    # Cuadrado centrado con un pequeño margen
    side = max(im.size)
    pad = round(side * 0.04)
    canvas = Image.new("RGBA", (side + 2 * pad, side + 2 * pad), (0, 0, 0, 0))
    canvas.paste(im, ((canvas.width - im.width) // 2, (canvas.height - im.height) // 2), im)
    if canvas.width > 1024:
        canvas = canvas.resize((1024, 1024), Image.LANCZOS)

    if ICO.exists() and not BACKUP.exists():
        shutil.copyfile(ICO, BACKUP)
    canvas.save(ICO, format="ICO", sizes=SIZES)
    print(f"Icono creado a partir de {src.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
