"""Draws the app icon (norrin_tpm.ico, multi-size) with Pillow, so the repository needs no binary design source."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("norrin_tpm.ico")


def draw(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.2)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(16, 42, 67, 255))
    # a process trend with one spike, and a check mark: "monitor the process, trust the data"
    w = max(2, size // 22)
    pts = [(0.10, 0.62), (0.26, 0.58), (0.36, 0.64), (0.46, 0.30), (0.54, 0.70), (0.64, 0.56), (0.90, 0.52)]
    d.line([(int(x * size), int(y * size)) for x, y in pts], fill=(126, 200, 227, 255), width=w, joint="curve")
    chk = [(0.56, 0.80), (0.66, 0.90), (0.88, 0.68)]
    d.line([(int(x * size), int(y * size)) for x, y in chk], fill=(72, 199, 142, 255), width=w + max(1, size // 64), joint="curve")
    return img


if __name__ == "__main__":
    draw(256).save(OUT, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
