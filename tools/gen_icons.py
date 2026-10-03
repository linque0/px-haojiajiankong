"""生成浏览器扩展图标（纯标准库 PNG，无需 PIL）。

用法：python tools/gen_icons.py
输出：extension/pxb7-extension/icons/icon{16,32,48,128}.png

图形：深色圆角底 + 橙色螃蟹（身体/双钳/腿/眼睛），4× 超采样后盒式降采样（抗锯齿）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "extension" / "pxb7-extension" / "icons"
SIZES = (16, 32, 48, 128)
SS = 4                       # 超采样倍数

BG = (23, 26, 33, 255)       # #171a21 面板底色
BG_EDGE = (42, 47, 60, 255)  # 描边
SHELL = (249, 115, 22, 255)  # #f97316 蟹壳
SHELL_HI = (251, 146, 60, 255)
CLAW = (234, 88, 12, 255)    # #ea580c
EYE = (255, 255, 255, 255)
PUPIL = (15, 17, 21, 255)
LEG = (194, 65, 12, 255)


def _ellipse(x: float, y: float, cx: float, cy: float, rx: float, ry: float) -> bool:
    dx = (x - cx) / rx
    dy = (y - cy) / ry
    return dx * dx + dy * dy <= 1.0


def _rounded_rect(x: float, y: float, r: float) -> bool:
    """单位方形（0..1）带圆角，r 为圆角半径（单位坐标）。"""
    if x < 0 or x > 1 or y < 0 or y > 1:
        return False
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def pixel(u: float, v: float) -> tuple[int, int, int, int]:
    """单位坐标 (u, v) ∈ [0,1]² → RGBA。"""
    # 圆角底
    if not _rounded_rect(u, v, 0.20):
        return (0, 0, 0, 0)
    color = BG
    # 描边（近似：靠近边缘一圈用 BG_EDGE）
    if not _rounded_rect(u, v, 0.20) or u < 0.045 or u > 0.955 or v < 0.045 or v > 0.955:
        color = BG_EDGE
    # 腿：两侧各三根短横条
    for i, yy in enumerate((0.66, 0.74, 0.82)):
        w = 0.20 - i * 0.02
        if (0.06 < u < 0.06 + w or 0.94 - w < u < 0.94) and abs(v - yy) < 0.035:
            color = LEG
    # 双钳（圆）
    if _ellipse(u, v, 0.20, 0.34, 0.13, 0.12) or _ellipse(u, v, 0.80, 0.34, 0.13, 0.12):
        color = CLAW
    if _ellipse(u, v, 0.20, 0.34, 0.09, 0.08) or _ellipse(u, v, 0.80, 0.34, 0.09, 0.08):
        color = SHELL
    # 身体
    if _ellipse(u, v, 0.50, 0.56, 0.30, 0.24):
        color = SHELL
    if _ellipse(u, v, 0.50, 0.50, 0.22, 0.14):
        color = SHELL_HI
    # 眼睛
    for cx in (0.41, 0.59):
        if _ellipse(u, v, cx, 0.50, 0.055, 0.055):
            color = EYE
        if _ellipse(u, v, cx, 0.51, 0.026, 0.026):
            color = PUPIL
    return color


def render_rgba(size: int) -> list[bytes]:
    """渲染一个 size×size 的 PNG 行（每行 RGBA bytes），SS× 超采样降采样。"""
    rows: list[bytes] = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    u = (px + (sx + 0.5) / SS) / size
                    v = (py + (sy + 0.5) / SS) / size
                    r, g, b, a = pixel(u, v)
                    acc[0] += r
                    acc[1] += g
                    acc[2] += b
                    acc[3] += a
            n = SS * SS
            row += bytes((acc[0] // n, acc[1] // n, acc[2] // n, acc[3] // n))
        rows.append(bytes(row))
    return rows


def write_png(path: Path, size: int, rows: list[bytes]) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(payload)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        target = OUT_DIR / f"icon{size}.png"
        write_png(target, size, render_rgba(size))
        print(f"[icons] {target.name} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
