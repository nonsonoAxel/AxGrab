"""
Dal logo (assets/logo.png, su fondo nero) genera:
  - ui/logo.png   logo con sfondo trasparente, per la barra dell'app
  - axgrab.ico    icona dell'EXE: logo su riquadro scuro arrotondato
Uso: python make_icon.py
"""
from PIL import Image, ImageDraw

SRC = "assets/logo.png"
TILE_BG = (14, 14, 16, 255)   # stesso fondo dell'app (#0e0e10)


def transparent_logo():
    """Il logo è su nero puro: l'opacità di ogni pixel è il suo canale più chiaro."""
    im = Image.open(SRC).convert("RGB")
    im = im.crop(im.point(lambda v: 255 if v > 24 else 0).getbbox())
    out = Image.new("RGBA", im.size)
    src, dst = im.load(), out.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = src[x, y]
            a = max(r, g, b)
            dst[x, y] = (0, 0, 0, 0) if a == 0 else (r * 255 // a, g * 255 // a, b * 255 // a, a)
    return out


def tile(logo, size=1024, pad=0.17, radius=0.22):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius), fill=255)
    img.paste(Image.new("RGBA", (size, size), TILE_BG), (0, 0), mask)
    inner = int(size * (1 - 2 * pad))
    scale = inner / max(logo.size)
    lg = logo.resize((round(logo.width * scale), round(logo.height * scale)), Image.LANCZOS)
    img.alpha_composite(lg, ((size - lg.width) // 2, (size - lg.height) // 2))
    return img


if __name__ == "__main__":
    logo = transparent_logo()
    h = 128
    logo.resize((round(logo.width * h / logo.height), h), Image.LANCZOS).save("ui/logo.png", optimize=True)
    big = tile(logo)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # i formati piccoli vengono ridotti da un riquadro con meno margine, così il logo resta leggibile
    frames = [(tile(logo, pad=0.12) if s <= 32 else big).resize((s, s), Image.LANCZOS) for s in sizes]
    frames[-1].save("axgrab.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    big.save("assets/icona-1024.png")
    print("creati: ui/logo.png, axgrab.ico, assets/icona-1024.png")
