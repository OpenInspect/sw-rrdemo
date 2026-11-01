"""Split one image into a grid of slightly overlapping tiles for testing the stitcher.

Usage:
    python scripts/make_tiles.py input.jpg out_dir --rows 3 --cols 4 --overlap 0.15
    python scripts/make_tiles.py input.jpg out_dir --jitter 20 --rotate 1.5 --scale 0.01 --seed 1

Tiles are written row-major as tile_r{row}_c{col}.png, so they can be fed to
the stitcher with rows/cols set to the same grid.
"""
import argparse
import math
import random
from pathlib import Path

import cv2
import numpy as np


def tile_size(total: int, count: int, overlap: float) -> int:
    """Tile length so `count` tiles overlapping by `overlap` exactly span `total`."""
    return round(total / (count - (count - 1) * overlap))


def sample_tile(image, cx: float, cy: float, tile_w: int, tile_h: int, angle_deg: float, scale: float):
    """Sample a tile_w x tile_h tile centred on (cx, cy) in the source, rotated/scaled about its centre."""
    theta = math.radians(angle_deg)
    a = scale * math.cos(theta)
    b = scale * math.sin(theta)
    hw, hh = (tile_w - 1) / 2, (tile_h - 1) / 2
    # Maps tile pixel -> source pixel (used with WARP_INVERSE_MAP).
    m = np.float32([
        [a, -b, cx - (a * hw - b * hh)],
        [b, a, cy - (b * hw + a * hh)],
    ])
    return cv2.warpAffine(
        image, m, (tile_w, tile_h),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def make_tiles(image, rows: int, cols: int, overlap: float,
               jitter: float = 0.0, rotate: float = 0.0, scale: float = 0.0, seed=None):
    """Yield (row, col, tile).

    jitter: max random shift of each tile's position, in pixels, per axis.
    rotate: max random rotation per tile, in degrees (+/-).
    scale:  max random scale deviation per tile (0.02 -> 0.98..1.02).
    """
    rng = random.Random(seed)
    h, w = image.shape[:2]
    tile_h = tile_size(h, rows, overlap)
    tile_w = tile_size(w, cols, overlap)
    step_y = round(tile_h * (1 - overlap))
    step_x = round(tile_w * (1 - overlap))
    perturbed = jitter or rotate or scale

    for r in range(rows):
        for c in range(cols):
            # Clamp so rounding never pushes the last tile past the image edge.
            y = min(r * step_y, h - tile_h)
            x = min(c * step_x, w - tile_w)
            if not perturbed:
                yield r, c, image[y:y + tile_h, x:x + tile_w]
                continue

            cx = x + (tile_w - 1) / 2 + rng.uniform(-jitter, jitter)
            cy = y + (tile_h - 1) / 2 + rng.uniform(-jitter, jitter)
            yield r, c, sample_tile(
                image, cx, cy, tile_w, tile_h,
                angle_deg=rng.uniform(-rotate, rotate),
                scale=1 + rng.uniform(-scale, scale),
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="source image")
    parser.add_argument("output_dir", type=Path, help="directory to write tiles into")
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--cols", type=int, default=2)
    parser.add_argument("--overlap", type=float, default=0.15,
                        help="fraction of each tile shared with its neighbour, in [0, 1) (default 0.15)")
    parser.add_argument("--ext", default="png", help="output format (default png, lossless)")
    parser.add_argument("--jitter", type=float, default=0.0,
                        help="max random shift of each tile off the uniform grid, in pixels (default 0)")
    parser.add_argument("--rotate", type=float, default=0.0,
                        help="max random rotation per tile, in degrees (default 0)")
    parser.add_argument("--scale", type=float, default=0.0,
                        help="max random scale deviation per tile, e.g. 0.02 for +/-2%% (default 0)")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible output")
    args = parser.parse_args()

    if args.rows < 1 or args.cols < 1:
        parser.error("--rows and --cols must be >= 1")
    if not 0 <= args.overlap < 1:
        parser.error("--overlap must be in [0, 1)")
    if args.jitter < 0 or args.rotate < 0 or not 0 <= args.scale < 1:
        parser.error("--jitter and --rotate must be >= 0 and --scale in [0, 1)")

    image = cv2.imread(str(args.input), cv2.IMREAD_COLOR)
    if image is None:
        parser.error(f"could not read image: {args.input}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    tiles = make_tiles(image, args.rows, args.cols, args.overlap,
                       jitter=args.jitter, rotate=args.rotate, scale=args.scale, seed=args.seed)
    for r, c, tile in tiles:
        cv2.imwrite(str(args.output_dir / f"tile_r{r}_c{c}.{args.ext}"), tile)
        count += 1

    print(f"Wrote {count} tiles ({tile.shape[1]}x{tile.shape[0]}) to {args.output_dir}")


if __name__ == "__main__":
    main()
