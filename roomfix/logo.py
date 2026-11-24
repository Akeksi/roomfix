"""The roomfix logo: a loudspeaker on a green tile, and its sound as a response curve that
starts uneven and ends flat. At 24 pixels and below, where the curve would be a smudge,
the loudspeaker sends sound waves instead (like the icon in the notification area).

Drawn with numpy, no image library. `python -m roomfix.logo <folder>` writes roomfix.ico
(for roomfix.exe and the installer) and logo.png."""
import struct
import sys
from pathlib import Path

import numpy as np

TOP, BOTTOM = np.array((70.0, 206.0, 156.0)), np.array((26.0, 132.0, 98.0))
INK = np.array((250.0, 252.0, 250.0))
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _near_curve(x, y, width, x0=0.485, x1=0.86):
    """Where the response curve is drawn: one peak and one dip that die away into a flat
    line, as a stroke of the given half width with round ends."""
    t = np.linspace(0, 1, 64)
    xs = x0 + (x1 - x0) * t
    ys = 0.5 - 0.16 * np.sin(2 * np.pi * 1.6 * t) * np.clip(1 - t / 0.68, 0, 1) ** 1.25
    box = ((x > xs.min() - width) & (x < xs.max() + width) & (y > ys.min() - width) & (y < ys.max() + width))
    px, py = x[box], y[box]  # only near the curve: the big sizes stay quick
    d = np.full(px.shape, np.inf)
    for ax, ay, bx, by in zip(xs[:-1], ys[:-1], xs[1:], ys[1:]):
        vx, vy = bx - ax, by - ay
        s = np.clip(((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy), 0, 1)
        d = np.minimum(d, np.hypot(px - ax - s * vx, py - ay - s * vy))
    near = np.zeros(x.shape, bool)
    near[box] = d < width
    return near


def picture(size, full=False):
    """The logo as RGBA [size, size, 4] (uint8). full: the tile fills the square, for a
    phone's home screen, which rounds the corners itself."""
    k = 4 if size <= 64 else 2  # drawn larger and averaged down, for smooth edges
    n = size * k
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n
    small = size <= 24
    inset = 0.0 if small or full else 0.04
    radius = 0.0 if full else 0.2
    lo, hi = inset + radius, 1 - inset - radius
    tile = np.hypot(x - np.clip(x, lo, hi), y - np.clip(y, lo, hi)) <= radius + 1e-9
    if full:
        tile = np.ones_like(tile)
    shade = ((y - inset) / (1 - 2 * inset)).clip(0, 1)[..., None]
    rgb = TOP * (1 - shade) + BOTTOM * shade
    if small:
        scale = 0.86  # the loudspeaker with two sound waves, a little smaller than the tile
        u, v = (x - 0.5) / scale + 0.5, (y - 0.5) / scale + 0.5
        dv = np.abs(v - 0.5)
        ink = ((u > 0.06) & (u <= 0.26) & (dv < 0.15)) | ((u > 0.24) & (u < 0.50) & (dv < 0.15 + (u - 0.24) * 1.15))
        r = np.hypot(u - 0.47, v - 0.5)
        ink |= (u > 0.55) & (dv < (u - 0.45) * 1.5) & ((np.abs(r - 0.21) < 0.05) | (np.abs(r - 0.38) < 0.05))
    else:
        dy = np.abs(y - 0.5)
        ink = ((x > 0.13) & (x <= 0.25) & (dy < 0.11)) | ((x > 0.24) & (x < 0.42) & (dy < 0.11 + (x - 0.25) * 1.1))
        ink |= _near_curve(x, y, 0.031)
    rgb = np.where((ink & tile)[..., None], INK, rgb)
    alpha = tile.astype(float)[..., None]
    rgba = np.concatenate([rgb * alpha, alpha], axis=-1).reshape(size, k, size, k, 4).mean(axis=(1, 3))
    color = np.where(rgba[..., 3:] > 0, rgba[..., :3] / np.maximum(rgba[..., 3:], 1e-9), 0)
    return np.concatenate([color, rgba[..., 3:] * 255], axis=-1).round().clip(0, 255).astype(np.uint8)


def png(size, full=False):
    from .tray import _png
    return _png(picture(size, full))


def ico(sizes=ICO_SIZES):
    """Windows .ico with the logo in several sizes (PNG inside, as Windows Vista and later read)."""
    images = [png(s) for s in sizes]
    head, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for size, data in zip(sizes, images):
        head += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + b"".join(images)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "roomfix.ico").write_bytes(ico())
    (folder / "logo.png").write_bytes(png(256))
    print("wrote", folder / "roomfix.ico", "and", folder / "logo.png")
