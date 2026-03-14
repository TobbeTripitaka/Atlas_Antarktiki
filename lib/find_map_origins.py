#!/usr/bin/env python3
"""
find_map_origins.py

Find the exact (x, y) pixel coordinates where each extracted map
was located on its source scan page.

Strategy (fast, memory-friendly):
    Since maps are pixel-exact crops (no rotation, scaling, or resampling),
    we can avoid full template matching. Instead:

    1. Read only ONE ROW of the map (e.g. the middle row).
    2. Search for that row inside the scan using a 1-D scan of the
       corresponding scanlines — much cheaper than 2-D convolution.
    3. Once we find a candidate Y offset from the 1-D match, verify
       with a few more rows to confirm.

    This is O(scan_height * scan_width) per map rather than
    O(scan_height * scan_width * map_height * map_width) for full
    template matching. For 400 DPI pages, this is the difference
    between seconds and minutes per pair.

    Falls back to OpenCV matchTemplate (on a downsampled pair) if
    the fast path fails — e.g. if JPEG compression was applied.

Usage:
    python find_map_origins.py \
        --scans-dir  "LZW compression TIFF/Atlas Antarktiki I" \
        --maps-dir   "atlas" \
        --lookup      atlas_master.xlsx \
        --output      map_origins.csv

    Or import and call find_origin() directly.


Todo
Size of map
Add to master spreadsheet

Tobias Staal 2026
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import tifffile
    HAS_TIFFFILE = True
except ImportError:
    HAS_TIFFFILE = False

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ============================================================================
# LOW-LEVEL READERS
# ============================================================================

def read_tiff_band(path, band=0):
    """
    Read a single band (or all bands) from a TIFF.
    Returns numpy array in (H, W) or (H, W, C) layout.
    Prefers tifffile (fast, no GDAL overhead), falls back to rasterio.
    """
    if HAS_TIFFFILE:
        img = tifffile.imread(str(path))
        if img.ndim == 3 and img.shape[0] in (1, 3, 4):
            # (C, H, W) -> (H, W, C)
            img = np.moveaxis(img, 0, -1)
        return img

    if HAS_RASTERIO:
        with rasterio.open(str(path)) as src:
            data = src.read()  # (C, H, W)
            if data.shape[0] == 1:
                return data[0]
            return np.moveaxis(data, 0, -1)

    raise ImportError("Need either tifffile or rasterio to read TIFFs.")


def read_rows(path, row_start, row_end):
    """
    Read a horizontal strip [row_start:row_end] from a TIFF.
    Uses tifffile for memory-mapped reading when possible.
    """
    if HAS_TIFFFILE:
        with tifffile.TiffFile(str(path)) as tif:
            page = tif.pages[0]
            # If tiled/stripped, we can use the full array via memmap
            img = page.asarray()
            strip = img[row_start:row_end]
            return strip

    # Fallback: rasterio windowed read
    if HAS_RASTERIO:
        with rasterio.open(str(path)) as src:
            window = rasterio.windows.Window(0, row_start, src.width, row_end - row_start)
            data = src.read(window=window)
            if data.shape[0] == 1:
                return data[0]
            return np.moveaxis(data, 0, -1)

    raise ImportError("Need either tifffile or rasterio.")


def get_dimensions(path):
    """Return (height, width) of a TIFF without reading pixel data."""
    if HAS_TIFFFILE:
        with tifffile.TiffFile(str(path)) as tif:
            page = tif.pages[0]
            return page.shape[0], page.shape[1]

    if HAS_RASTERIO:
        with rasterio.open(str(path)) as src:
            return src.height, src.width

    raise ImportError("Need either tifffile or rasterio.")


# ============================================================================
# FAST EXACT-MATCH FINDER
# ============================================================================

def find_origin_exact(scan_path, map_path, verify_rows=5):
    """
    Find the pixel-exact origin (x, y) of a map crop within a scan.

    Returns:
        (x, y, confidence) where x/y are top-left pixel coords
        confidence is 1.0 for exact match, < 1.0 for partial.
        Returns (None, None, 0.0) on failure.
    """
    scan_h, scan_w = get_dimensions(scan_path)
    map_h, map_w = get_dimensions(map_path)

    if map_h > scan_h or map_w > scan_w:
        print(f"  WARNING: Map larger than scan, skipping.")
        return None, None, 0.0

    # --- Step 1: Read a single "probe" row from the middle of the map ---
    probe_row_idx = map_h // 2
    map_row = read_rows(map_path, probe_row_idx, probe_row_idx + 1)
    if map_row.ndim == 3:
        # Flatten to 1-D for matching: use first band only for speed
        map_row_1d = map_row[0, :, 0].astype(np.int16)
    elif map_row.ndim == 2:
        map_row_1d = map_row[0, :].astype(np.int16)
    else:
        map_row_1d = map_row.flatten().astype(np.int16)

    # --- Step 2: Scan through each possible Y position ---
    #     At each Y, read one row from the scan at (Y + probe_row_idx)
    #     and check all possible X offsets.

    best_x, best_y, best_conf = None, None, 0.0
    map_w_actual = len(map_row_1d)

    # Y range: the probe row in the scan is at scan_y + probe_row_idx
    y_min = 0
    y_max = scan_h - map_h

    for y in range(y_min, y_max + 1):
        scan_row_idx = y + probe_row_idx
        scan_row = read_rows(scan_path, scan_row_idx, scan_row_idx + 1)
        if scan_row.ndim == 3:
            scan_row_1d = scan_row[0, :, 0].astype(np.int16)
        elif scan_row.ndim == 2:
            scan_row_1d = scan_row[0, :].astype(np.int16)
        else:
            scan_row_1d = scan_row.flatten().astype(np.int16)

        # Sliding 1-D match using correlation or direct comparison
        for x in range(0, scan_w - map_w_actual + 1):
            if scan_row_1d[x] == map_row_1d[0]:
                # Quick check: does this slice match?
                if np.array_equal(scan_row_1d[x:x + map_w_actual], map_row_1d):
                    best_x, best_y = x, y
                    best_conf = 1.0
                    break
        if best_conf == 1.0:
            break

    if best_conf < 1.0:
        return None, None, 0.0

    # --- Step 3: Verify with additional rows ---
    verified = 0
    check_rows = np.linspace(0, map_h - 1, verify_rows, dtype=int)

    for mr in check_rows:
        scan_strip = read_rows(scan_path, best_y + mr, best_y + mr + 1)
        map_strip = read_rows(map_path, mr, mr + 1)

        if scan_strip.ndim == 3:
            s = scan_strip[0, best_x:best_x + map_w_actual, 0]
        elif scan_strip.ndim == 2:
            s = scan_strip[0, best_x:best_x + map_w_actual]
        else:
            s = scan_strip[best_x:best_x + map_w_actual]

        if map_strip.ndim == 3:
            m = map_strip[0, :map_w_actual, 0]
        elif map_strip.ndim == 2:
            m = map_strip[0, :map_w_actual]
        else:
            m = map_strip[:map_w_actual]

        if np.array_equal(s, m):
            verified += 1

    best_conf = verified / len(check_rows)
    return best_x, best_y, best_conf


# ============================================================================
# FASTER: NUMPY VECTORISED ROW SEARCH
# ============================================================================

def find_origin_fast(scan_path, map_path, verify_rows=5):
    """
    Vectorised version: reads the full scan into memory once,
    then uses numpy broadcasting to find the probe row.

    Much faster than row-by-row I/O but needs the scan in RAM.
    A 400 DPI A2 colour scan is ~300-500 MB — manageable on most
    workstations with 16+ GB RAM.

    Returns:
        (x, y, confidence)
    """
    scan_h, scan_w = get_dimensions(scan_path)
    map_h, map_w = get_dimensions(map_path)

    if map_h > scan_h or map_w > scan_w:
        return None, None, 0.0

    # Read full images
    scan = read_tiff_band(scan_path)
    map_img = read_tiff_band(map_path)

    # Use single channel for matching
    if scan.ndim == 3:
        scan_gray = scan[:, :, 0]
    else:
        scan_gray = scan

    if map_img.ndim == 3:
        map_gray = map_img[:, :, 0]
    else:
        map_gray = map_img

    # Probe: middle row of map
    probe_row = map_gray[map_h // 2, :]

    # First pixel value for quick rejection
    first_val = probe_row[0]
    probe_len = len(probe_row)

    # Search
    for y in range(scan_h - map_h + 1):
        scan_row = scan_gray[y + map_h // 2, :]

        # Find all positions where first pixel matches
        candidates = np.where(scan_row[:scan_w - probe_len + 1] == first_val)[0]

        for x in int(candidates) if candidates.size == 1 else candidates:
            x = int(x)
            if np.array_equal(scan_row[x:x + probe_len], probe_row):
                # Found candidate — verify with corner pixels first (instant)
                corners_ok = (
                    scan_gray[y, x] == map_gray[0, 0] and
                    scan_gray[y, x + map_w - 1] == map_gray[0, map_w - 1] and
                    scan_gray[y + map_h - 1, x] == map_gray[map_h - 1, 0] and
                    scan_gray[y + map_h - 1, x + map_w - 1] == map_gray[map_h - 1, map_w - 1]
                )
                if not corners_ok:
                    continue

                # Full block verify
                block = scan_gray[y:y + map_h, x:x + map_w]
                if np.array_equal(block, map_gray):
                    return x, y, 1.0

    return None, None, 0.0


# ============================================================================
# FALLBACK: OpenCV TEMPLATE MATCHING (downsampled)
# ============================================================================

def find_origin_opencv(scan_path, map_path, downsample=4):
    """
    Fallback using OpenCV matchTemplate on downsampled images.
    Use when pixel values don't match exactly (e.g. different compression).

    Returns (x, y, confidence) in ORIGINAL pixel coordinates.
    """
    if not HAS_CV2:
        print("  WARNING: OpenCV not available for fallback matching.")
        return None, None, 0.0

    scan = read_tiff_band(scan_path)
    map_img = read_tiff_band(map_path)

    # Convert to grayscale
    if scan.ndim == 3:
        scan_gray = cv2.cvtColor(scan, cv2.COLOR_RGB2GRAY)
    else:
        scan_gray = scan

    if map_img.ndim == 3:
        map_gray = cv2.cvtColor(map_img, cv2.COLOR_RGB2GRAY)
    else:
        map_gray = map_img

    # Downsample for speed
    if downsample > 1:
        scan_small = scan_gray[::downsample, ::downsample]
        map_small = map_gray[::downsample, ::downsample]
    else:
        scan_small = scan_gray
        map_small = map_gray

    # Template match
    result = cv2.matchTemplate(scan_small, map_small, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    # Scale back to original coordinates
    x_approx = max_loc[0] * downsample
    y_approx = max_loc[1] * downsample

    # Refine at full resolution in a small window
    margin = downsample * 2
    x_lo = max(0, x_approx - margin)
    y_lo = max(0, y_approx - margin)
    x_hi = min(scan_gray.shape[1], x_approx + map_gray.shape[1] + margin)
    y_hi = min(scan_gray.shape[0], y_approx + map_gray.shape[0] + margin)

    roi = scan_gray[y_lo:y_hi, x_lo:x_hi]
    if roi.shape[0] >= map_gray.shape[0] and roi.shape[1] >= map_gray.shape[1]:
        result_fine = cv2.matchTemplate(roi, map_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val_fine, _, max_loc_fine = cv2.minMaxLoc(result_fine)
        x_final = x_lo + max_loc_fine[0]
        y_final = y_lo + max_loc_fine[1]
        return x_final, y_final, float(max_val_fine)

    return x_approx, y_approx, float(max_val)


# ============================================================================
# MAIN DISPATCHER
# ============================================================================

def find_origin(scan_path, map_path, method='auto', verify_rows=5, downsample=4):
    """
    Find origin of map within scan.

    method:
        'exact'  — pixel-exact numpy search (fast for lossless crops)
        'opencv' — OpenCV template matching fallback
        'auto'   — try exact first, fall back to opencv
    """
    scan_path = Path(scan_path)
    map_path = Path(map_path)

    if not scan_path.exists():
        print(f"  ERROR: Scan not found: {scan_path}")
        return None, None, 0.0

    if not map_path.exists():
        print(f"  ERROR: Map not found: {map_path}")
        return None, None, 0.0

    if method == 'exact':
        return find_origin_fast(scan_path, map_path, verify_rows=verify_rows)

    if method == 'opencv':
        return find_origin_opencv(scan_path, map_path, downsample=downsample)

    # Auto: try exact first
    x, y, conf = find_origin_fast(scan_path, map_path, verify_rows=verify_rows)
    if conf >= 1.0:
        return x, y, conf

    print(f"  Exact match failed, trying OpenCV fallback...")
    return find_origin_opencv(scan_path, map_path, downsample=downsample)


# ============================================================================
# BATCH PROCESSING
# ============================================================================

def batch_find_origins(lookup_df, scan_col='raw_path', map_col='tiepoint_path',
                       method='auto', output_csv='map_origins.csv'):
    """
    Process all scan/map pairs from a DataFrame.

    Expects columns with file paths to scans and maps.
    Produces a CSV with columns:
        map_file, scan_file, origin_x, origin_y, map_width, map_height, confidence
    """
    results = []

    for idx, row in lookup_df.iterrows():
        scan_path = row[scan_col]
        map_path = row[map_col]

        if pd.isna(scan_path) or pd.isna(map_path):
            continue

        scan_path = str(scan_path).strip()
        map_path = str(map_path).strip()

        if scan_path in ('-', '') or map_path in ('-', ''):
            continue

        print(f"[{idx+1}/{len(lookup_df)}] {Path(map_path).name} in {Path(scan_path).name}...")

        x, y, conf = find_origin(scan_path, map_path, method=method)

        map_h, map_w = get_dimensions(map_path) if Path(map_path).exists() else (0, 0)

        results.append({
            'map_file': map_path,
            'scan_file': scan_path,
            'origin_x': x,
            'origin_y': y,
            'map_width': map_w,
            'map_height': map_h,
            'confidence': conf
        })

        status = "OK" if conf >= 0.99 else ("APPROXIMATE" if conf > 0.7 else "FAILED")
        print(f"  -> ({x}, {y}) [{status}, conf={conf:.3f}]")

    df_out = pd.DataFrame(results)
    df_out.to_csv(output_csv, index=False)
    print(f"\nResults written to {output_csv}")
    print(f"  Exact matches:       {(df_out['confidence'] >= 1.0).sum()}")
    print(f"  Approximate matches: {((df_out['confidence'] > 0.7) & (df_out['confidence'] < 1.0)).sum()}")
    print(f"  Failed:              {(df_out['confidence'] <= 0.7).sum()}")

    return df_out


# ============================================================================
# CLI
# ============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Find pixel coordinates of map crops within scan pages.'
    )
    parser.add_argument('--scans-dir', type=str, help='Directory containing scan TIFFs')
    parser.add_argument('--maps-dir', type=str, help='Directory containing map TIFFs')
    parser.add_argument('--lookup', type=str, default='atlas_master.xlsx',
                        help='Excel file with scan/map path columns')
    parser.add_argument('--scan-col', type=str, default='raw_path',
                        help='Column name for scan file paths')
    parser.add_argument('--map-col', type=str, default='tiepoint_path',
                        help='Column name for map file paths')
    parser.add_argument('--method', choices=['auto', 'exact', 'opencv'], default='auto',
                        help='Matching method (default: auto)')
    parser.add_argument('--output', type=str, default='map_origins.csv',
                        help='Output CSV path')

    # Single-pair mode
    parser.add_argument('--scan', type=str, help='Single scan file')
    parser.add_argument('--map', type=str, help='Single map file')

    args = parser.parse_args()

    if args.scan and args.map:
        # Single pair mode
        x, y, conf = find_origin(args.scan, args.map, method=args.method)
        mh, mw = get_dimensions(args.map)
        print(f"Origin: ({x}, {y})")
        print(f"Map size: {mw} x {mh}")
        print(f"Confidence: {conf:.4f}")
    else:
        # Batch mode from Excel
        df = pd.read_excel(args.lookup)
        batch_find_origins(df, scan_col=args.scan_col, map_col=args.map_col,
                          method=args.method, output_csv=args.output)
