"""Airplane flies left-to-right across the 8x16 AIP1640 matrix, hits the
right wall, explodes (white flash -> fireball -> sparks), then loops."""

import math
import random
import time

from aip1640 import AIP1640

PLANE = [
    ".............",
    ".#...........",
    ".##....#.....",
    ".###########.",
    "#############",
    ".###########.",
    ".......#.....",
    ".............",
]

SCREEN_W = 16
SCREEN_H = 8
IMPACT_C = 14
IMPACT_R = 3


def sprite_to_columns(rows):
    width = len(rows[0])
    cols = []
    for x in range(width):
        b = 0
        for y in range(SCREEN_H):
            if rows[y][x] == "#":
                b |= 1 << y
        cols.append(b)
    return cols


def fly_frame(plane_cols, x):
    frame = [0] * SCREEN_W
    for i, b in enumerate(plane_cols):
        px = x + i
        if 0 <= px < SCREEN_W:
            frame[px] = b
    return frame


def fireball_frame(radius):
    frame = [0] * SCREEN_W
    rsq = radius * radius
    for r in range(SCREEN_H):
        for c in range(SCREEN_W):
            dr = r - IMPACT_R
            dc = c - IMPACT_C
            if dr * dr + dc * dc <= rsq:
                frame[c] |= 1 << r
    return frame


def spark_frame(spread, density, rng):
    frame = [0] * SCREEN_W
    for _ in range(density):
        angle = rng.random() * 2 * math.pi
        r = spread * (0.5 + rng.random())
        sr = int(round(IMPACT_R + math.sin(angle) * r))
        sc = int(round(IMPACT_C + math.cos(angle) * r))
        if 0 <= sr < SCREEN_H and 0 <= sc < SCREEN_W:
            frame[sc] |= 1 << sr
    return frame


def main():
    matrix = AIP1640(clk_pin=6, din_pin=5, brightness=5)
    plane = sprite_to_columns(PLANE)
    plane_w = len(plane)
    impact_x = SCREEN_W - plane_w
    rng = random.Random()

    try:
        while True:
            for x in range(-plane_w, impact_x + 1):
                matrix.write_frame(fly_frame(plane, x))
                time.sleep(0.06)

            for _ in range(2):
                matrix.write_frame([0xFF] * SCREEN_W)
                time.sleep(0.04)

            for radius in (1.0, 2.0, 3.0, 4.0, 4.5, 4.0, 3.0):
                matrix.write_frame(fireball_frame(radius))
                time.sleep(0.08)

            for stage in range(9):
                spread = 2 + stage * 0.6
                density = max(2, 18 - stage * 2)
                matrix.write_frame(spark_frame(spread, density, rng))
                time.sleep(0.07)

            matrix.write_frame([0] * SCREEN_W)
            time.sleep(0.7)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.cleanup()


if __name__ == "__main__":
    main()
