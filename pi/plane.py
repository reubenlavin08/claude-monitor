"""
Airplane scrolling across the 8x16 AIP1640 LED matrix.
Run on the Pi:  python3 plane.py
Ctrl-C to stop. The display is cleared and GPIO released on exit.

If the plane comes out upside-down: pass `flip_v=True` to render().
If it comes out mirrored: pass `flip_h=True`.
"""

import time
from aip1640 import AIP1640

# 8 rows x 12 cols, facing right. '#' = lit pixel.
PLANE = [
    "............",
    ".#..........",
    ".##....#....",
    ".##########.",
    "############",
    ".##########.",
    ".......#....",
    "............",
]

SCREEN_W = 16
SCREEN_H = 8


def sprite_to_columns(rows):
    width = max(len(r) for r in rows)
    rows = [r.ljust(width) for r in rows]
    cols = []
    for x in range(width):
        b = 0
        for y in range(SCREEN_H):
            if y < len(rows) and x < len(rows[y]) and rows[y][x] == "#":
                b |= 1 << y
        cols.append(b)
    return cols


def render(matrix, sprite_cols, sprite_x, flip_h=False, flip_v=False):
    """Place sprite_cols at column sprite_x on a blank 16-col canvas, then push."""
    frame = [0] * SCREEN_W
    for i, byte in enumerate(sprite_cols):
        px = sprite_x + i
        if 0 <= px < SCREEN_W:
            frame[px] = byte
    if flip_v:
        frame = [_bit_reverse_8(b) for b in frame]
    if flip_h:
        frame.reverse()
    matrix.write_frame(frame)


def _bit_reverse_8(b):
    b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
    b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
    b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
    return b


def main():
    matrix = AIP1640(clk_pin=6, din_pin=5, brightness=4)
    plane = sprite_to_columns(PLANE)
    plane_w = len(plane)
    # Scroll right-to-left: nose enters from the right, exits left.
    # Sprite x goes from +16 (fully off right) down to -plane_w (fully off left).
    try:
        while True:
            for x in range(SCREEN_W, -plane_w - 1, -1):
                render(matrix, plane, x)
                time.sleep(0.06)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.cleanup()


if __name__ == "__main__":
    main()
