import base64
import io
import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

import qr_codec

# Ring values corresponding to the 6 scoring rows in each Passe table
RING_VALUES = [10, 9, 8, 7, 6, 0]
RING_LABELS = ["10", "9", "8", "7", "6", "0 (M)"]

# Exact physical proportions inside a single Passe box (from official DOCX template):
# Total box width = 82.532 mm:
#   Col 0 (Ringe label): 16.492 mm -> 0.0000 to 0.1998
#   Cols 1..6 (Arrows 1..6): 11.007 mm each -> 0.1998 to 1.0000 (each col is 0.13336 wide)
# Total box height = 48.404 mm:
#   Header row (Pfeil 1..6): 7.197 mm -> 0.0000 to 0.1487
#   6 Ring rows (10, 9, 8, 7, 6, 0(M)): 5.786 mm each -> 0.1487 to 0.8659 (each row is 0.11953 high)
#   Summe row: 6.491 mm -> 0.8659 to 1.0000
PASSE_COL_X_FRACS = [
    0.0,
    0.1998,
    0.3332,
    0.4665,
    0.5999,
    0.7333,
    0.8666,
    1.0000,
]

PASSE_ROW_Y_FRACS = [
    0.0,
    0.1487,
    0.2682,
    0.3877,
    0.5073,
    0.6268,
    0.7463,
    0.8659,
    1.0000,
]

# Relative positions of the 5 Passe boxes within the 5-Passen bounding area
# Width: Left box (82.53mm) + gap (10.00mm) + Right box (82.53mm) = 175.06mm
#   Left box X:  0.0000 .. 0.4714
#   Right box X: 0.5286 .. 1.0000
# Height:
#   Box 1/2 Y: 0.0000 .. 0.2966
#   Box 3/4 Y: 0.3517 .. 0.6483
#   Box 5   Y: 0.7034 .. 1.0000
REL_PASSE_BOXES_IN_GRID_AREA = [
    (0.0000, 0.0000, 0.4714, 0.2966),  # Passe 1 (or 6) - Top Left
    (0.5286, 0.0000, 1.0000, 0.2966),  # Passe 2 (or 7) - Top Right
    (0.0000, 0.3517, 0.4714, 0.6483),  # Passe 3 (or 8) - Mid Left
    (0.5286, 0.3517, 1.0000, 0.6483),  # Passe 4 (or 9) - Mid Right
    (0.0000, 0.7034, 0.4714, 1.0000),  # Passe 5 (or 10) - Bottom Left
]

# Full A4 page relative position of the 5-Passen grid area (when full A4 sheet is photographed):
FULL_PAGE_GRID_AREA = (0.0833, 0.2138, 0.9167, 0.7633)


def _load_and_normalize_image(image_bytes: bytes, max_dim: int = 1600) -> Image.Image:
    """Loads an image from bytes, applies EXIF orientation, and converts to RGB."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize((int(round(w * scale)), int(round(h * scale))), Image.Resampling.LANCZOS)
    return img


def _auto_deskew_image(img: Image.Image) -> Tuple[Image.Image, float]:
    """
    Automatically detects if the photographed scorecard is tilted (up to +/- 14 degrees)
    or rotated 90 degrees, and straightens it so that all Passe table borders are
    perfectly horizontal and vertical.
    """
    w, h = img.size
    # Downsample for fast angle estimation
    small_scale = 480.0 / float(max(w, h))
    sw = max(60, int(round(w * small_scale)))
    sh = max(60, int(round(h * small_scale)))
    small_gray = img.resize((sw, sh), Image.Resampling.BILINEAR).convert("L")
    arr = np.array(small_gray, dtype=np.float32)

    # Compute strong horizontal & vertical edge gradients
    gy = np.abs(arr[2:, 1:-1] - arr[:-2, 1:-1])
    gx = np.abs(arr[1:-1, 2:] - arr[1:-1, :-2])

    # Threshold edges to focus on high-contrast table grid lines
    thresh_y = float(np.percentile(gy, 88))
    thresh_x = float(np.percentile(gx, 88))
    edge_y = Image.fromarray(np.clip((gy - thresh_y) * 4.0, 0, 255).astype(np.uint8))
    edge_x = Image.fromarray(np.clip((gx - thresh_x) * 4.0, 0, 255).astype(np.uint8))

    def _score_angle(ang: float) -> float:
        ry = np.array(edge_y.rotate(ang, resample=Image.Resampling.BILINEAR), dtype=np.float32)
        rx = np.array(edge_x.rotate(ang, resample=Image.Resampling.BILINEAR), dtype=np.float32)
        # Sharp peaks in row/column projections indicate axis-aligned table grid lines
        proj_y = ry.mean(axis=1)
        proj_x = rx.mean(axis=0)
        return float(np.var(proj_y) + np.var(proj_x))

    # Coarse search: -12 to +12 deg in 1.5 deg steps
    best_ang = 0.0
    best_val = _score_angle(0.0)
    for ang in np.linspace(-12.0, 12.0, 17):
        val = _score_angle(float(ang))
        if val > best_val:
            best_val = val
            best_ang = float(ang)

    # Fine search around best_ang in 0.25 deg steps
    for ang in np.linspace(best_ang - 1.5, best_ang + 1.5, 13):
        val = _score_angle(float(ang))
        if val > best_val:
            best_val = val
            best_ang = float(ang)

    if abs(best_ang) >= 0.35:
        # Rotate original image with white/edge background fill
        rotated = img.rotate(best_ang, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(240, 240, 240))
        return rotated, round(best_ang, 2)

    return img, 0.0


def _compute_adaptive_dark_mask(gray: np.ndarray, window: int = 31, offset: float = 15.0) -> np.ndarray:
    """
    Computes a binary mask of dark pixels using integral-image local mean thresholding.
    Works reliably even under uneven shadows from phone cameras.
    """
    h, w = gray.shape
    window = max(11, window | 1)
    pad = window // 2
    padded = np.pad(gray.astype(np.float32), pad, mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant", constant_values=0).cumsum(axis=0).cumsum(axis=1)
    y0 = np.arange(h)
    y1 = y0 + window
    x0 = np.arange(w)
    x1 = x0 + window
    local_sum = (
        integral[y1[:, None], x1[None, :]]
        - integral[y0[:, None], x1[None, :]]
        - integral[y1[:, None], x0[None, :]]
        + integral[y0[:, None], x0[None, :]]
    )
    local_mean = local_sum / float(window * window)
    return gray < (local_mean - offset)


def _extract_hv_lines(dark_mask: np.ndarray, h_len: int, v_len: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Extracts horizontal and vertical table grid lines.
    Includes a 3-pixel perpendicular tolerance band so slightly curved or residual-skewed
    lines from smartphone camera lenses are captured without gaps.
    """
    h, w = dark_mask.shape

    # 3px vertical dilation before horizontal run-length check (tolerates slight lens curve/skew)
    dm_h = dark_mask.copy()
    dm_h[1:, :] |= dark_mask[:-1, :]
    dm_h[:-1, :] |= dark_mask[1:, :]
    mask_hf = dm_h.astype(np.float32)

    h_len = max(8, h_len)
    pad_h = h_len // 2
    cs_h = np.pad(mask_hf, ((0, 0), (pad_h + 1, h_len - pad_h - 1)), mode="constant").cumsum(axis=1)
    run_h = (cs_h[:, h_len:] - cs_h[:, :-h_len]) / float(h_len)
    horiz_eroded = (run_h > 0.68).astype(np.float32)
    cs_hd = np.pad(horiz_eroded, ((0, 0), (pad_h + 1, h_len - pad_h - 1)), mode="constant").cumsum(axis=1)
    horiz_lines = (cs_hd[:, h_len:] - cs_hd[:, :-h_len]) > 0

    # 3px horizontal dilation before vertical run-length check
    dm_v = dark_mask.copy()
    dm_v[:, 1:] |= dark_mask[:, :-1]
    dm_v[:, :-1] |= dark_mask[:, 1:]
    mask_vf = dm_v.astype(np.float32)

    v_len = max(8, v_len)
    pad_v = v_len // 2
    cs_v = np.pad(mask_vf, ((pad_v + 1, v_len - pad_v - 1), (0, 0)), mode="constant").cumsum(axis=0)
    run_v = (cs_v[v_len:, :] - cs_v[:-v_len, :]) / float(v_len)
    vert_eroded = (run_v > 0.68).astype(np.float32)
    cs_vd = np.pad(vert_eroded, ((pad_v + 1, v_len - pad_v - 1), (0, 0)), mode="constant").cumsum(axis=0)
    vert_lines = (cs_vd[v_len:, :] - cs_vd[:-v_len, :]) > 0

    return horiz_lines, vert_lines


def _build_integral(arr: np.ndarray) -> np.ndarray:
    return np.pad(arr.astype(np.float32), ((1, 0), (1, 0)), mode="constant", constant_values=0).cumsum(axis=0).cumsum(axis=1)


def _rect_mean(integral: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
    h_lim, w_lim = integral.shape[0] - 1, integral.shape[1] - 1
    x0 = max(0, min(w_lim - 1, x0))
    x1 = max(x0 + 1, min(w_lim, x1))
    y0 = max(0, min(h_lim - 1, y0))
    y1 = max(y0 + 1, min(h_lim, y1))
    s = float(integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0])
    return s / float((x1 - x0) * (y1 - y0))


def _compute_color_ring_mask(rgb: np.ndarray) -> np.ndarray:
    """
    Detects pixels belonging to the characteristic colored Ring-value column on the left
    of each Passe box (Yellow 10/9, Red/Pink 8/7, Light Blue 6).
    Works as an additional anchor even if black borders are shadowed.
    """
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)

    # Yellow: R and G both significantly higher than B
    yellow = ((r + g) * 0.5 - b > 14.0) & (r > 120) & (g > 110)
    # Red/Pink: R significantly higher than G and B
    red = (r - (g + b) * 0.5 > 10.0) & (r > 120)
    # Blue: B significantly higher than R
    blue = (b - r > 10.0) & (b > 110)

    return (yellow | red | blue).astype(np.float32)


def _find_connected_boxes(grid_mask: np.ndarray, min_w: int, max_w: int, min_h: int, max_h: int) -> List[Tuple[int, int, int, int]]:
    """
    Finds rectangular bounding boxes of connected grid structures using downsampled BFS.
    Also splits boxes if two Passe tables accidentally touched horizontally or vertically.
    """
    h, w = grid_mask.shape
    scale = 2
    sh, sw = h // scale, w // scale
    small = grid_mask[: sh * scale, : sw * scale].reshape(sh, scale, sw, scale).any(axis=(1, 3))

    visited = np.zeros_like(small, dtype=bool)
    boxes = []

    ys, xs = np.where(small)
    for sy, sx in zip(ys, xs):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        min_x = max_x = sx
        min_y = max_y = sy
        count = 0

        while stack:
            cy, cx = stack.pop()
            count += 1
            if cx < min_x:
                min_x = cx
            if cx > max_x:
                max_x = cx
            if cy < min_y:
                min_y = cy
            if cy > max_y:
                max_y = cy

            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < sh and 0 <= nx < sw and not visited[ny, nx] and small[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))

        if count < 50:
            continue

        bx0, by0 = min_x * scale, min_y * scale
        bx1, by1 = (max_x + 1) * scale, (max_y + 1) * scale
        bw = bx1 - bx0
        bh = by1 - by0

        if min_w <= bw <= max_w:
            aspect = bw / float(max(1, bh))
            # 1 single Passe box: aspect ~ 1.705
            if min_h <= bh <= max_h and 1.18 <= aspect <= 2.35:
                boxes.append((bx0, by0, bx1, by1))
            # 2 vertically stacked Passe boxes (e.g. Passe 2 + Passe 4 bridged by 'Passe 4' label): aspect ~ 0.78
            elif min_h * 1.8 <= bh <= max_h * 2.4 and 0.63 <= aspect <= 0.96:
                boxes.append((bx0, by0, bx1, by0 + int(round(0.4576 * bh))))
                boxes.append((bx0, by0 + int(round(0.5424 * bh)), bx1, by1))
            # 3 vertically stacked Passe boxes (e.g. Passe 1 + Passe 3 + Passe 5 bridged by labels): aspect ~ 0.506
            elif min_h * 2.6 <= bh <= max_h * 3.6 and 0.38 <= aspect <= 0.62:
                boxes.append((bx0, by0, bx1, by0 + int(round(0.2966 * bh))))
                boxes.append((bx0, by0 + int(round(0.3517 * bh)), bx1, by0 + int(round(0.6483 * bh))))
                boxes.append((bx0, by0 + int(round(0.7034 * bh)), bx1, by1))
        # Check if 2 boxes merged horizontally (e.g. Left Passe + Right Passe -> aspect ~ 3.4 to 3.9)
        elif min_w * 1.9 <= bw <= max_w * 2.3:
            aspect = bw / float(max(1, bh))
            if min_h <= bh <= max_h and 3.0 <= aspect <= 4.3:
                w_single = int(round(bw * 0.4714))
                boxes.append((bx0, by0, bx0 + w_single, by1))
                boxes.append((bx1 - w_single, by0, bx1, by1))
            # Check if entire 5-Passen block merged into one component (aspect ~ 1.07)
            elif min_h * 2.6 <= bh <= max_h * 3.8 and 0.85 <= aspect <= 1.32:
                for rx0, ry0, rx1, ry1 in REL_PASSE_BOXES_IN_GRID_AREA:
                    boxes.append(
                        (
                            bx0 + int(round(rx0 * bw)),
                            by0 + int(round(ry0 * bh)),
                            bx0 + int(round(rx1 * bw)),
                            by0 + int(round(ry1 * bh)),
                        )
                    )

    return boxes


def _score_passe_box_candidate(
    rgb: np.ndarray,
    horiz_lines: np.ndarray,
    vert_lines: np.ndarray,
    box: Tuple[int, int, int, int],
) -> float:
    """
    Scores how well a candidate rectangle (x0, y0, x1, y1) matches a Blasrohr Passe table:
    1. 9-tooth horizontal comb match against PASSE_ROW_Y_FRACS
    2. 8-tooth vertical comb match against PASSE_COL_X_FRACS
    3. Color signature in the left label column (Yellow -> Red -> Blue)
    4. Aspect ratio closeness to 1.705
    """
    h, w = horiz_lines.shape
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    bw = x1 - x0
    bh = y1 - y0
    if bw < 45 or bh < 28:
        return 0.0

    aspect = bw / float(bh)
    aspect_score = max(0.0, 1.0 - abs(aspect - 1.705) / 0.70)

    h_sub = horiz_lines[y0:y1, x0:x1]
    v_sub = vert_lines[y0:y1, x0:x1]

    h_profile = h_sub.mean(axis=1)
    h_hits = 0.0
    for fy in PASSE_ROW_Y_FRACS:
        py = int(round(fy * (bh - 1)))
        r0 = max(0, py - max(2, int(bh * 0.04)))
        r1 = min(bh, py + max(3, int(bh * 0.04)))
        h_hits += min(1.0, float(h_profile[r0:r1].max()) * 2.2) if r1 > r0 else 0.0
    h_score = h_hits / len(PASSE_ROW_Y_FRACS)

    v_profile = v_sub.mean(axis=0)
    v_hits = 0.0
    for fx in PASSE_COL_X_FRACS:
        px = int(round(fx * (bw - 1)))
        c0 = max(0, px - max(2, int(bw * 0.035)))
        c1 = min(bw, px + max(3, int(bw * 0.035)))
        v_hits += min(1.0, float(v_profile[c0:c1].max()) * 2.2) if c1 > c0 else 0.0
    v_score = v_hits / len(PASSE_COL_X_FRACS)

    # Check color signature in left column (x: 2%..18%)
    cx0 = x0 + int(bw * 0.02)
    cx1 = x0 + int(bw * 0.18)
    color_score = 0.0
    if cx1 > cx0:
        yel = rgb[y0 + int(bh * 0.16) : y0 + int(bh * 0.37), cx0:cx1].astype(np.float32)
        red = rgb[y0 + int(bh * 0.40) : y0 + int(bh * 0.61), cx0:cx1].astype(np.float32)
        blu = rgb[y0 + int(bh * 0.64) : y0 + int(bh * 0.73), cx0:cx1].astype(np.float32)
        if yel.size > 0 and red.size > 0 and blu.size > 0:
            y_diff = float(((yel[:, :, 0] + yel[:, :, 1]) * 0.5 - yel[:, :, 2]).mean())
            r_diff = float((red[:, :, 0] - (red[:, :, 1] + red[:, :, 2]) * 0.5).mean())
            b_diff = float((blu[:, :, 2] - blu[:, :, 0]).mean())
            c_pts = (1.0 if y_diff > 6 else 0.0) + (1.0 if r_diff > 5 else 0.0) + (1.0 if b_diff > 5 else 0.0)
            color_score = c_pts / 3.0

    return 0.40 * h_score + 0.35 * v_score + 0.15 * aspect_score + 0.10 * color_score


def _refine_single_passe_box(
    horiz_lines: np.ndarray,
    vert_lines: np.ndarray,
    box: Tuple[int, int, int, int],
) -> Tuple[int, int, int, int]:
    """
    Refines a single Passe box (x0, y0, x1, y1) using 9-tooth horizontal comb matching
    and 8-tooth vertical comb matching so that even if there is text ('Passe X') above
    the box or a shadow near one edge, the box locks tightly onto the true 8x9 table grid!
    """
    h, w = horiz_lines.shape
    x0, y0, x1, y1 = box
    bw = max(40, x1 - x0)
    bh = max(25, y1 - y0)

    # Search window around initial box
    pad_x = max(8, int(bw * 0.12))
    pad_y = max(8, int(bh * 0.14))

    sx0 = max(0, x0 - pad_x)
    sx1 = min(w, x1 + pad_x)
    sy0 = max(0, y0 - pad_y)
    sy1 = min(h, y1 + pad_y)

    h_prof = horiz_lines[sy0:sy1, max(0, x0) : min(w, x1)].mean(axis=1)
    v_prof = vert_lines[max(0, y0) : min(h, y1), sx0:sx1].mean(axis=0)

    # Smooth 1D profiles slightly (3-tap) so 1-2px offsets still score high in comb search
    if len(h_prof) >= 5:
        h_smooth = np.convolve(h_prof, [0.25, 0.5, 0.25], mode="same")
    else:
        h_smooth = h_prof
    if len(v_prof) >= 5:
        v_smooth = np.convolve(v_prof, [0.25, 0.5, 0.25], mode="same")
    else:
        v_smooth = v_prof

    # 1. First comb-match horizontal position & width (x0, bw) using all 8 vertical lines
    best_x0, best_x1 = x0, x1
    best_v_score = -1.0
    col_fracs = np.array(PASSE_COL_X_FRACS, dtype=np.float32)

    for scale_w in np.linspace(0.90, 1.10, 11):
        cand_bw = int(round(bw * scale_w))
        if cand_bw < 30 or cand_bw >= len(v_smooth):
            continue
        offsets = np.round(col_fracs * (cand_bw - 1)).astype(int)
        max_start = len(v_smooth) - cand_bw
        if max_start <= 0:
            continue
        starts = np.arange(max_start)
        comb_vals = v_smooth[starts[:, None] + offsets[None, :]].mean(axis=1)
        outer_vals = v_smooth[starts[:, None] + offsets[[0, 1, 7]][None, :]].mean(axis=1)
        scores = 0.65 * comb_vals + 0.35 * outer_vals
        idx_best = int(np.argmax(scores))
        if float(scores[idx_best]) > best_v_score and float(scores[idx_best]) > 0.18:
            best_v_score = float(scores[idx_best])
            best_x0 = sx0 + idx_best
            best_x1 = best_x0 + cand_bw

    # Use the true refined width to constrain the expected table height (official aspect = 1.7052)
    refined_bw = max(30, best_x1 - best_x0)
    expected_bh = refined_bw / 1.7052
    # If initial bh was distorted (e.g. by text/boxes above or below), anchor around expected_bh
    base_bh = expected_bh if abs(bh - expected_bh) > 0.08 * expected_bh else float(bh)

    # 2. Comb-match vertical position & height (y0, bh) using all 9 horizontal lines
    best_y0, best_y1 = y0, int(round(y0 + base_bh))
    best_h_score = -1.0
    row_fracs = np.array(PASSE_ROW_Y_FRACS, dtype=np.float32)

    for scale_h in np.linspace(0.92, 1.08, 11):
        cand_bh = int(round(base_bh * scale_h))
        if cand_bh < 20 or cand_bh >= len(h_smooth):
            continue
        offsets = np.round(row_fracs * (cand_bh - 1)).astype(int)
        max_start = len(h_smooth) - cand_bh
        if max_start <= 0:
            continue
        starts = np.arange(max_start)
        comb_vals = h_smooth[starts[:, None] + offsets[None, :]].mean(axis=1)
        outer_vals = h_smooth[starts[:, None] + offsets[[0, 1, 7, 8]][None, :]].mean(axis=1)
        scores = 0.65 * comb_vals + 0.35 * outer_vals
        idx_best = int(np.argmax(scores))
        if float(scores[idx_best]) > best_h_score and float(scores[idx_best]) > 0.18:
            best_h_score = float(scores[idx_best])
            best_y0 = sy0 + idx_best
            best_y1 = best_y0 + cand_bh

    return (best_x0, best_y0, best_x1, best_y1)


def _search_5_boxes_constellation(
    rgb: np.ndarray,
    horiz_lines: np.ndarray,
    vert_lines: np.ndarray,
    seed_boxes: List[Tuple[int, int, int, int]],
) -> Tuple[List[Tuple[int, int, int, int]], str]:
    """
    Autonomously locates the 5 Passe boxes (2x3 layout: P1, P2, P3, P4, P5) anywhere in the image,
    even if the sheet is photographed from further away, shifted off-center, or only partially
    detected by connected components.
    Uses:
      1. Seed hypotheses from any detected individual Passe boxes (matching each seed to slot 0..4)
      2. Multi-scale sliding-window integral-image search across the entire photo
    """
    h, w = horiz_lines.shape
    joints = horiz_lines & vert_lines
    color_mask = _compute_color_ring_mask(rgb)

    int_grid = _build_integral((horiz_lines | vert_lines).astype(np.float32))
    int_joints = _build_integral(joints.astype(np.float32))
    int_color = _build_integral(color_mask)

    def _evaluate_constellation(gx0: int, gy0: int, gw: int, gh: int) -> float:
        if gx0 < 0 or gy0 < 0 or gx0 + gw > w or gy0 + gh > h or gw < int(w * 0.28) or gh < int(h * 0.25):
            return -1.0

        box_energy = 0.0
        color_energy = 0.0
        for rx0, ry0, rx1, ry1 in REL_PASSE_BOXES_IN_GRID_AREA:
            bx0 = gx0 + int(round(rx0 * gw))
            by0 = gy0 + int(round(ry0 * gh))
            bx1 = gx0 + int(round(rx1 * gw))
            by1 = gy0 + int(round(ry1 * gh))
            g_val = _rect_mean(int_grid, bx0, by0, bx1, by1)
            j_val = _rect_mean(int_joints, bx0, by0, bx1, by1)
            # Left 20% of each Passe box has the Yellow/Red/Blue ring column
            c_val = _rect_mean(int_color, bx0, by0, bx0 + max(4, int((bx1 - bx0) * 0.21)), by1)
            box_energy += g_val + 6.0 * j_val
            color_energy += c_val

        # Penalty for grid joints in the empty bottom-right signature quadrant (where no Passe table exists)
        sig_x0 = gx0 + int(round(0.54 * gw))
        sig_y0 = gy0 + int(round(0.72 * gh))
        sig_x1 = gx0 + int(round(0.98 * gw))
        sig_y1 = gy0 + int(round(0.98 * gh))
        sig_joints = _rect_mean(int_joints, sig_x0, sig_y0, sig_x1, sig_y1)

        # Penalty for grid joints in the vertical gap between Left and Right Passe columns
        gap_x0 = gx0 + int(round(0.48 * gw))
        gap_x1 = gx0 + int(round(0.52 * gw))
        gap_joints = _rect_mean(int_joints, gap_x0, gy0, gap_x1, gy0 + gh)

        return (box_energy / 5.0) + 0.35 * (color_energy / 5.0) - 2.5 * sig_joints - 2.0 * gap_joints

    best_constellation = None
    best_const_score = -1e9
    best_mode = "auto_constellation"

    # Strategy A: Generate constellation hypotheses from every detected seed box
    # Each seed box could be slot 0 (P1), slot 1 (P2), slot 2 (P3), slot 3 (P4), or slot 4 (P5)!
    for sb in seed_boxes:
        sbw = sb[2] - sb[0]
        sbh = sb[3] - sb[1]
        est_gw = int(round(sbw / 0.4714))
        est_gh = int(round(sbh / 0.2966))
        for slot_idx, (rx0, ry0, rx1, ry1) in enumerate(REL_PASSE_BOXES_IN_GRID_AREA):
            gx0 = int(round(sb[0] - rx0 * est_gw))
            gy0 = int(round(sb[1] - ry0 * est_gh))
            sc = _evaluate_constellation(gx0, gy0, est_gw, est_gh)
            if sc > best_const_score:
                best_const_score = sc
                best_constellation = (gx0, gy0, est_gw, est_gh)
                best_mode = "auto_seed_constellation"

    # Strategy B: Multi-scale global search over the whole image (handles cases where 0 seeds were isolated)
    # Official 5-Passen grid area is 175.06 mm wide x 163.20 mm high -> aspect GW / GH = 1.0727
    # In image pixel coordinates, gw_frac ranges from 0.36 to 0.96 of image width
    for gw_frac in np.linspace(0.38, 0.94, 13):
        cand_gw = int(round(w * gw_frac))
        # Physical height of 5-Passen block is cand_gw / 1.0727; test slight perspective ratios 0.88..1.02
        for gh_ratio in (0.90, 0.932, 0.97):
            cand_gh = int(round(cand_gw * gh_ratio))
            if cand_gh > h * 0.96 or cand_gh < h * 0.25:
                continue
            step_x = max(8, int(w * 0.025))
            step_y = max(8, int(h * 0.025))
            for gx0 in range(0, max(1, w - cand_gw + 1), step_x):
                for gy0 in range(0, max(1, h - cand_gh + 1), step_y):
                    sc = _evaluate_constellation(gx0, gy0, cand_gw, cand_gh)
                    if sc > best_const_score:
                        best_const_score = sc
                        best_constellation = (gx0, gy0, cand_gw, cand_gh)
                        best_mode = "auto_global_search"

    if best_constellation is None:
        # Ultimate fallback
        fx0, fy0, fx1, fy1 = FULL_PAGE_GRID_AREA if h > w * 1.2 else (0.04, 0.06, 0.96, 0.94)
        best_constellation = (
            int(round(fx0 * w)),
            int(round(fy0 * h)),
            int(round((fx1 - fx0) * w)),
            int(round((fy1 - fy0) * h)),
        )
        best_mode = "template_fallback"

    gx0, gy0, gw, gh = best_constellation

    # Fine-tune each of the 5 Passe boxes individually:
    # If a high-confidence detected seed box already overlaps a slot closely, use that seed box,
    # and then run 9-tooth/8-tooth comb refinement on each of the 5 boxes!
    final_boxes: List[Tuple[int, int, int, int]] = []
    for rx0, ry0, rx1, ry1 in REL_PASSE_BOXES_IN_GRID_AREA:
        pred_box = (
            gx0 + int(round(rx0 * gw)),
            gy0 + int(round(ry0 * gh)),
            gx0 + int(round(rx1 * gw)),
            gy0 + int(round(ry1 * gh)),
        )
        # Check if any seed box overlaps this predicted slot by > 55% IoU
        best_seed = pred_box
        best_iou = 0.45
        for sb in seed_boxes:
            ix0, iy0 = max(pred_box[0], sb[0]), max(pred_box[1], sb[1])
            ix1, iy1 = min(pred_box[2], sb[2]), min(pred_box[3], sb[3])
            if ix1 > ix0 and iy1 > iy0:
                inter = float((ix1 - ix0) * (iy1 - iy0))
                union = float(
                    (pred_box[2] - pred_box[0]) * (pred_box[3] - pred_box[1])
                    + (sb[2] - sb[0]) * (sb[3] - sb[1])
                    - inter
                )
                iou = inter / max(1.0, union)
                if iou > best_iou:
                    best_iou = iou
                    best_seed = sb

        refined = _refine_single_passe_box(horiz_lines, vert_lines, best_seed)
        final_boxes.append(refined)

    return final_boxes, best_mode


def detect_passe_boxes(
    rgb: np.ndarray,
    crop_region: Optional[Dict[str, float]] = None,
) -> Tuple[List[Tuple[int, int, int, int]], str]:
    """
    Autonomously detects the 5 Passe bounding boxes [(x0, y0, x1, y1), ...] in order Passe 1..5 (or 6..10),
    regardless of whether the user aligned the sheet with the camera Zielrahmen or photographed
    the entire scorecard from further away / off-center.
    """
    h, w, _ = rgb.shape
    gray = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.float32)
    dark_mask = _compute_adaptive_dark_mask(gray, window=max(15, int(min(w, h) * 0.025) | 1), offset=14.0)
    horiz_lines, vert_lines = _extract_hv_lines(
        dark_mask,
        h_len=max(14, int(w * 0.032)),
        v_len=max(14, int(h * 0.026)),
    )
    grid_mask = horiz_lines | vert_lines

    # 1. If user explicitly provided a manual crop region, evaluate inside it
    if crop_region and all(k in crop_region for k in ("x0", "y0", "x1", "y1")):
        gx0 = int(round(crop_region["x0"] * w))
        gy0 = int(round(crop_region["y0"] * h))
        gx1 = int(round(crop_region["x1"] * w))
        gy1 = int(round(crop_region["y1"] * h))
        gw = max(50, gx1 - gx0)
        gh = max(50, gy1 - gy0)
        boxes = []
        for rx0, ry0, rx1, ry1 in REL_PASSE_BOXES_IN_GRID_AREA:
            bx0 = gx0 + int(round(rx0 * gw))
            by0 = gy0 + int(round(ry0 * gh))
            bx1 = gx0 + int(round(rx1 * gw))
            by1 = gy0 + int(round(ry1 * gh))
            boxes.append(_refine_single_passe_box(horiz_lines, vert_lines, (bx0, by0, bx1, by1)))
        return boxes, "manual_frame"

    # 2. Find candidate Passe boxes across a wide scale range (from 14% width up to 62% width)
    raw_boxes = _find_connected_boxes(
        grid_mask,
        min_w=int(w * 0.14),
        max_w=int(w * 0.62),
        min_h=int(h * 0.06),
        max_h=int(h * 0.42),
    )

    scored = []
    for b in raw_boxes:
        rb = _refine_single_passe_box(horiz_lines, vert_lines, b)
        sc = _score_passe_box_candidate(rgb, horiz_lines, vert_lines, rb)
        if sc > 0.30:
            scored.append((sc, rb))

    scored.sort(key=lambda item: item[0], reverse=True)
    unique_boxes: List[Tuple[int, int, int, int]] = []
    for sc, b in scored:
        bx0, by0, bx1, by1 = b
        overlap = False
        for ux0, uy0, ux1, uy1 in unique_boxes:
            ix0, iy0 = max(bx0, ux0), max(by0, uy0)
            ix1, iy1 = min(bx1, ux1), min(by1, uy1)
            if ix1 > ix0 and iy1 > iy0:
                inter = (ix1 - ix0) * (iy1 - iy0)
                area = (bx1 - bx0) * (by1 - by0)
                if inter > 0.35 * area:
                    overlap = True
                    break
        if not overlap:
            unique_boxes.append(b)

    # Check if we have 5 clean, consistent Passe boxes of similar size arranged in 2x3 layout
    if len(unique_boxes) >= 5:
        # Filter candidates that agree with the median width/height of the top 5
        top5 = unique_boxes[:5]
        med_w = float(np.median([b[2] - b[0] for b in top5]))
        med_h = float(np.median([b[3] - b[1] for b in top5]))
        consistent = [
            b
            for b in unique_boxes
            if 0.78 * med_w <= (b[2] - b[0]) <= 1.25 * med_w
            and 0.78 * med_h <= (b[3] - b[1]) <= 1.25 * med_h
        ]
        if len(consistent) >= 5:
            candidates = consistent[:5]
            candidates.sort(key=lambda b: (b[1] + b[3]) * 0.5)
            row1 = sorted(candidates[0:2], key=lambda b: b[0])
            row2 = sorted(candidates[2:4], key=lambda b: b[0])
            row3 = [candidates[4]]
            # Verify 2x3 geometric layout (left col X < right col X)
            if (
                row1[1][0] - row1[0][0] > med_w * 0.7
                and row2[1][0] - row2[0][0] > med_w * 0.7
                and abs(row3[0][0] - row1[0][0]) < med_w * 0.35
            ):
                p1, p2, p3, p4, p5 = row1[0], row1[1], row2[0], row2[1], row3[0]
                expected_p5_y0 = p3[1] + (p3[1] - p1[1])
                expected_p5_h = int(round(np.median([b[3] - b[1] for b in (p1, p2, p3, p4)])))
                if abs(p5[1] - expected_p5_y0) > expected_p5_h * 0.08 or abs((p5[3] - p5[1]) - expected_p5_h) > expected_p5_h * 0.08:
                    p5 = _refine_single_passe_box(
                        horiz_lines,
                        vert_lines,
                        (p3[0], expected_p5_y0, p3[2], expected_p5_y0 + expected_p5_h),
                    )
                ordered = [p1, p2, p3, p4, p5]
                return ordered, "auto_5_boxes"

    # 3. Otherwise, run full Constellation + Integral-Image Search seeded by any valid boxes!
    return _search_5_boxes_constellation(rgb, horiz_lines, vert_lines, unique_boxes)


def _snap_internal_grid_lines(
    horiz_lines: np.ndarray,
    vert_lines: np.ndarray,
    box: Tuple[int, int, int, int],
) -> Tuple[List[int], List[int]]:
    """
    Finds the exact pixel coordinates of the 8 vertical grid lines and 9 horizontal grid lines
    inside a single Passe box (x0, y0, x1, y1), snapping template fractions to actual line peaks.
    """
    h, w = horiz_lines.shape
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    bw = max(20, x1 - x0)
    bh = max(20, y1 - y0)

    h_sub = horiz_lines[y0:y1, x0:x1].mean(axis=1)
    v_sub = vert_lines[y0:y1, x0:x1].mean(axis=0)

    col_xs = []
    search_x = max(2, int(bw * 0.025))
    for i, fx in enumerate(PASSE_COL_X_FRACS):
        expected_x = int(round(fx * (bw - 1)))
        if i in (0, len(PASSE_COL_X_FRACS) - 1):
            col_xs.append(x0 + expected_x)
            continue
        c0 = max(0, expected_x - search_x)
        c1 = min(bw, expected_x + search_x + 1)
        if c1 > c0 and v_sub[c0:c1].max() > 0.22:
            best_offset = int(np.argmax(v_sub[c0:c1]))
            col_xs.append(x0 + c0 + best_offset)
        else:
            col_xs.append(x0 + expected_x)

    row_ys = []
    search_y = max(2, int(bh * 0.028))
    for j, fy in enumerate(PASSE_ROW_Y_FRACS):
        expected_y = int(round(fy * (bh - 1)))
        if j in (0, len(PASSE_ROW_Y_FRACS) - 1):
            row_ys.append(y0 + expected_y)
            continue
        r0 = max(0, expected_y - search_y)
        r1 = min(bh, expected_y + search_y + 1)
        if r1 > r0 and h_sub[r0:r1].max() > 0.22:
            best_offset = int(np.argmax(h_sub[r0:r1]))
            row_ys.append(y0 + r0 + best_offset)
        else:
            row_ys.append(y0 + expected_y)

    return col_xs, row_ys


def _measure_cell_cross_score(gray_cell: np.ndarray, dark_cell: np.ndarray) -> float:
    """
    Measures the strength of a handwritten cross ('X') inside the cropped inner region of a grid cell.
    """
    ch, cw = gray_cell.shape
    if ch < 4 or cw < 4:
        return 0.0

    yy = np.linspace(-1.0, 1.0, ch)[:, None]
    xx = np.linspace(-1.0, 1.0, cw)[None, :]
    dist_sq = xx * xx + yy * yy
    weights = np.clip(1.0 - 0.65 * dist_sq, 0.1, 1.0)

    bg_level = float(np.percentile(gray_cell, 82))
    ink_strength = np.clip((bg_level - gray_cell) / 42.0, 0.0, 1.5)
    combined_ink = np.maximum(dark_cell.astype(np.float32), ink_strength)

    weighted_density = float((combined_ink * weights).sum() / weights.sum())

    if ch >= 4 and cw >= 4:
        gy = np.abs(gray_cell[2:, 1:-1] - gray_cell[:-2, 1:-1])
        gx = np.abs(gray_cell[1:-1, 2:] - gray_cell[1:-1, :-2])
        grad_energy = float(np.clip((gx + gy - 18.0) / 75.0, 0.0, 1.5).mean())
    else:
        grad_energy = weighted_density

    mid_y, mid_x = max(1, ch // 2), max(1, cw // 2)
    q_scores = [
        float(combined_ink[:mid_y, :mid_x].mean()),
        float(combined_ink[:mid_y, mid_x:].mean()),
        float(combined_ink[mid_y:, :mid_x].mean()),
        float(combined_ink[mid_y:, mid_x:].mean()),
    ]
    active_quadrants = sum(1 for q in q_scores if q > 0.04)
    quadrant_factor = 1.0 if active_quadrants >= 3 else (0.75 if active_quadrants == 2 else 0.35)

    return (0.65 * weighted_density + 0.35 * grad_energy) * quadrant_factor


def evaluate_scorecard_image(
    image_bytes: bytes,
    half: int = 1,
    crop_region: Optional[Dict[str, float]] = None,
) -> Dict:
    """
    Evaluates a photographed Blasrohr-Ergebniszettel (Half 1 = Passe 1..5, Half 2 = Passe 6..10).
    1. Auto-deskews tilted photos
    2. Autonomously detects the 5 Passe boxes anywhere on the scorecard
    3. Detects and decodes the QR code (containing all header fields + half) if present
    4. Snaps internal 6x6 cells and detects crosses per column
    """
    raw_img_hi = _load_and_normalize_image(image_bytes, max_dim=4200)
    raw_img = _load_and_normalize_image(image_bytes, max_dim=1600)
    img, deskew_angle = _auto_deskew_image(raw_img)
    if abs(deskew_angle) >= 0.35:
        img_hi = raw_img_hi.rotate(deskew_angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(240, 240, 240))
    else:
        img_hi = raw_img_hi

    rgb = np.array(img)
    h, w, _ = rgb.shape
    gray = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.float32)
    dark_mask = _compute_adaptive_dark_mask(gray, window=max(15, int(min(w, h) * 0.022) | 1), offset=13.5)
    horiz_lines, vert_lines = _extract_hv_lines(
        dark_mask,
        h_len=max(14, int(w * 0.032)),
        v_len=max(14, int(h * 0.026)),
    )

    passe_boxes, detection_mode = detect_passe_boxes(rgb, crop_region=crop_region)

    # Attempt to decode QR code from high-res deskewed image (guided by passe_boxes geometry + fallback)
    scale_hi = float(img_hi.size[0]) / float(max(1, w))
    qr_data = qr_codec.decode_scorecard_qr_from_pil(img_hi, passe_boxes=passe_boxes, scale_from_eval_img=scale_hi)
    if qr_data and qr_data.get("half") in (1, 2):
        half = int(qr_data["half"])

    vis_img = img.copy()
    draw = ImageDraw.Draw(vis_img, "RGBA")

    start_passe = 1 if int(half) == 1 else 6
    passes_result = []
    passe_crops_b64 = []

    all_x0 = min(b[0] for b in passe_boxes)
    all_y0 = min(b[1] for b in passe_boxes)
    all_x1 = max(b[2] for b in passe_boxes)
    all_y1 = max(b[3] for b in passe_boxes)

    for p_idx, box in enumerate(passe_boxes):
        passe_num = start_passe + p_idx
        bx0, by0, bx1, by1 = box
        col_xs, row_ys = _snap_internal_grid_lines(horiz_lines, vert_lines, box)

        # Draw Passe outer bounding box on overview image
        draw.rectangle([bx0, by0, bx1, by1], outline=(37, 99, 235, 235), width=3)

        arrows: List[int] = []
        confidences: List[float] = []
        selected_rows: List[int] = []

        for arrow_idx in range(6):
            cx0 = col_xs[arrow_idx + 1]
            cx1 = col_xs[arrow_idx + 2]
            cw = max(1, cx1 - cx0)
            inset_x = max(1, int(round(cw * 0.20)))
            icx0 = min(cx1 - 1, cx0 + inset_x)
            icx1 = max(icx0 + 1, cx1 - inset_x)

            row_scores = []
            cell_centers = []
            for r_idx in range(6):
                ry0 = row_ys[r_idx + 1]
                ry1 = row_ys[r_idx + 2]
                rh = max(1, ry1 - ry0)
                inset_y = max(1, int(round(rh * 0.20)))
                iry0 = min(ry1 - 1, ry0 + inset_y)
                iry1 = max(iry0 + 1, ry1 - inset_y)

                gray_cell = gray[iry0:iry1, icx0:icx1]
                dark_cell = dark_mask[iry0:iry1, icx0:icx1]
                score = _measure_cell_cross_score(gray_cell, dark_cell)
                row_scores.append(score)
                cell_centers.append(((cx0 + cx1) // 2, (ry0 + ry1) // 2, cx0, ry0, cx1, ry1))

            sorted_scores = sorted(row_scores, reverse=True)
            best_score = sorted_scores[0]
            second_score = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
            best_row = int(np.argmax(row_scores))

            if best_score < 0.035:
                ring_val = 0
                best_row = 5
                conf = 0.25
            else:
                ring_val = RING_VALUES[best_row]
                margin = (best_score - second_score) / max(best_score, 1e-5)
                conf = float(np.clip(0.45 + 0.55 * margin, 0.35, 0.99))
                if best_score < 0.060:
                    conf = min(conf, 0.65)

            arrows.append(ring_val)
            confidences.append(round(conf, 2))
            selected_rows.append(best_row)

            ccx, ccy, scx0, sry0, scx1, sry1 = cell_centers[best_row]
            color_fill = (16, 185, 129, 75) if conf >= 0.70 else (245, 158, 11, 95)
            color_border = (5, 150, 105, 240) if conf >= 0.70 else (217, 119, 6, 240)
            draw.rectangle([scx0 + 2, sry0 + 2, scx1 - 2, sry1 - 2], fill=color_fill, outline=color_border, width=2)
            r_rad = max(3, min(scx1 - scx0, sry1 - sry0) // 4)
            draw.ellipse([ccx - r_rad, ccy - r_rad, ccx + r_rad, ccy + r_rad], outline=color_border, width=2)

        pad_crop = 6
        crop_box = (
            max(0, bx0 - pad_crop),
            max(0, by0 - pad_crop),
            min(w, bx1 + pad_crop),
            min(h, by1 + pad_crop),
        )
        p_crop = vis_img.crop(crop_box)
        buf_p = io.BytesIO()
        p_crop.save(buf_p, format="JPEG", quality=82)
        passe_crops_b64.append("data:image/jpeg;base64," + base64.b64encode(buf_p.getvalue()).decode("ascii"))

        passes_result.append(
            {
                "passe_index": p_idx + 1,
                "passe_number": passe_num,
                "arrows": arrows,
                "selected_rows": selected_rows,
                "confidences": confidences,
                "sum": int(sum(arrows)),
            }
        )

    buf_full = io.BytesIO()
    vis_img.save(buf_full, format="JPEG", quality=82)
    annotated_b64 = "data:image/jpeg;base64," + base64.b64encode(buf_full.getvalue()).decode("ascii")

    half_total = int(sum(p["sum"] for p in passes_result))
    avg_conf = round(float(np.mean([c for p in passes_result for c in p["confidences"]])), 2)

    return {
        "half": int(half),
        "passes": passes_result,
        "half_total": half_total,
        "average_confidence": avg_conf,
        "detection_mode": detection_mode,
        "deskew_angle": deskew_angle,
        "qr_data": qr_data,
        "annotated_image": annotated_b64,
        "passe_crops": passe_crops_b64,
        "detected_grid_region": {
            "x0": round(all_x0 / float(w), 4),
            "y0": round(all_y0 / float(h), 4),
            "x1": round(all_x1 / float(w), 4),
            "y1": round(all_y1 / float(h), 4),
        },
    }


def generate_test_scorecard_image(
    half: int = 1,
    shooter_name: str = "Mustermann, Max",
    club: str = "SV Pfeilflug",
    class_name: str = "Herren",
    competition_name: str = "Vereinsmeisterschaft",
    date_str: str = "31.10.2026",
    group_name: str = "Gruppe 1",
    stand_number: str = "4",
    distance: str = "7 m",
    shooter_id: Optional[int] = None,
    competition_id: Optional[int] = None,
    preset_passes: Optional[List[List[int]]] = None,
) -> Tuple[bytes, List[List[int]]]:
    """
    Generates a high-resolution, millimeter-accurate image of the official
    'Blasrohr - Ergebniszettel' (Sheet A for half=1, Sheet B for half=2)
    with a QR code in the top-right header and realistic handwritten crosses ('X') in each Passe.
    """
    scale = 8.0
    W = int(round(210.0 * scale))
    H = int(round(297.0 * scale))

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("arialbd.ttf", int(4.8 * scale))
        font_box_ab = ImageFont.truetype("arialbd.ttf", int(5.2 * scale))
        font_label = ImageFont.truetype("arialbd.ttf", int(2.7 * scale))
        font_hand = ImageFont.truetype("segoepr.ttf", int(2.9 * scale))
    except Exception:
        font_title = ImageFont.load_default()
        font_box_ab = ImageFont.load_default()
        font_label = ImageFont.load_default()
        font_hand = ImageFont.load_default()

    margin_l = int(round(17.5 * scale))
    margin_r = W - margin_l
    draw.text((margin_l, int(11.5 * scale)), "Blasrohr - Ergebniszettel", fill=(0, 0, 0), font=font_title)

    ab_letter = "A" if int(half) == 1 else "B"
    bx0, by0, bx1, by1 = int(151.0 * scale), int(10 * scale), int(162.0 * scale), int(18.8 * scale)
    draw.rectangle([bx0, by0, bx1, by1], outline=(0, 0, 0), width=2)
    draw.text((bx0 + int(3.5 * scale), by0 + int(1.6 * scale)), ab_letter, fill=(0, 0, 0), font=font_box_ab)

    # Render QR code in top-right header (165.0 .. 193.5 mm, y = 15.2 .. 43.7 mm)
    is_5m = "5" in (distance or "") or "5 m" in (class_name or "").lower() or "5m" in (class_name or "").lower()
    resolved_dist = "5 m" if is_5m else "7 m"
    qr_payload = qr_codec.encode_scorecard_qr_payload(
        half=half,
        competition_name=competition_name,
        shooter_name=shooter_name,
        club=club,
        date_str=date_str,
        class_name=class_name,
        distance=resolved_dist,
        group_name=group_name,
        stand_number=stand_number,
        shooter_id=shooter_id,
        competition_id=competition_id,
    )
    qr_pil = qr_codec.make_scorecard_qr_image(qr_payload, box_size=6, border=2)
    qr_px_sz = int(round(28.5 * scale))
    qr_pil_resized = qr_pil.resize((qr_px_sz, qr_px_sz), Image.Resampling.NEAREST)
    img.paste(qr_pil_resized, (int(round(165.0 * scale)), int(round(15.2 * scale))))

    fields_left = [
        ("Wettbewerb", competition_name, 21.0),
        ("Name, Vorname", shooter_name, 26.8),
        ("Verein", club, 32.6),
        ("Gruppe", group_name, 38.4),
    ]
    for lbl, val, ym in fields_left:
        y_px = int(ym * scale)
        draw.text((margin_l, y_px), lbl, fill=(0, 0, 0), font=font_label)
        draw.line([(int(45.0 * scale), y_px + int(3.8 * scale)), (int(96.5 * scale), y_px + int(3.8 * scale))], fill=(0, 0, 0), width=1)
        if val:
            draw.text((int(46.0 * scale), y_px - int(0.4 * scale)), str(val), fill=(25, 50, 130), font=font_hand)

    entf_label = "Entfernung   7 m [ ]  5 m [x]" if is_5m else "Entfernung   7 m [x]  5 m [ ]"
    fields_right = [
        ("Datum", date_str, 21.0, True),
        ("Klasse", class_name, 26.8, True),
        (entf_label, "", 32.6, False),
        ("Standnummer", stand_number, 38.4, True),
    ]
    for lbl, val, ym, has_line in fields_right:
        y_px = int(ym * scale)
        draw.text((int(100.5 * scale), y_px), lbl, fill=(0, 0, 0), font=font_label)
        if has_line:
            draw.line([(int(124.5 * scale), y_px + int(3.8 * scale)), (int(162.0 * scale), y_px + int(3.8 * scale))], fill=(0, 0, 0), width=1)
            if val:
                draw.text((int(125.5 * scale), y_px - int(0.4 * scale)), str(val), fill=(25, 50, 130), font=font_hand)

    draw.text((int(76 * scale), int(46.0 * scale)), "Bitte den Ergebniszettel leserlich ausfüllen.", fill=(30, 30, 30), font=font_label)
    draw.text((int(61 * scale), int(50.5 * scale)), "Bei jedem Schuss einer Passe pro Spalte bitte genau ein Kreuz.", fill=(0, 0, 0), font=font_label)

    if preset_passes is None:
        weights = [0.38, 0.30, 0.16, 0.09, 0.05, 0.02]
        preset_passes = [random.choices(RING_VALUES, weights=weights, k=6) for _ in range(5)]

    gx0 = int(round(FULL_PAGE_GRID_AREA[0] * W))
    gy0 = int(round(FULL_PAGE_GRID_AREA[1] * H))
    gw = int(round((FULL_PAGE_GRID_AREA[2] - FULL_PAGE_GRID_AREA[0]) * W))
    gh = int(round((FULL_PAGE_GRID_AREA[3] - FULL_PAGE_GRID_AREA[1]) * H))

    row_bg_colors = [
        (253, 238, 168),  # 10: Yellow
        (253, 238, 168),  # 9: Yellow
        (246, 198, 196),  # 8: Red/Pink
        (246, 198, 196),  # 7: Red/Pink
        (195, 218, 244),  # 6: Blue
        (255, 255, 255),  # 0 (M): White
    ]

    start_passe = 1 if int(half) == 1 else 6
    for p_idx, (rx0, ry0, rx1, ry1) in enumerate(REL_PASSE_BOXES_IN_GRID_AREA):
        px0 = gx0 + int(round(rx0 * gw))
        py0 = gy0 + int(round(ry0 * gh))
        px1 = gx0 + int(round(rx1 * gw))
        py1 = gy0 + int(round(ry1 * gh))
        pbw = px1 - px0
        pbh = py1 - py0

        draw.text((px0 + 2, py0 - int(4.5 * scale)), f"Passe {start_passe + p_idx}", fill=(0, 0, 0), font=font_label)

        col_xs = [px0 + int(round(f * pbw)) for f in PASSE_COL_X_FRACS]
        row_ys = [py0 + int(round(f * pbh)) for f in PASSE_ROW_Y_FRACS]

        draw.rectangle([col_xs[1], row_ys[0], col_xs[7], row_ys[1]], fill=(217, 217, 217))
        draw.line([(col_xs[0], row_ys[0]), (col_xs[1], row_ys[1])], fill=(0, 0, 0), width=1)
        draw.text((col_xs[0] + int(1.2 * scale), row_ys[0] + int(3.5 * scale)), "Ringe", fill=(0, 0, 0), font=font_label)

        for r_idx, r_label in enumerate(RING_LABELS):
            ry_top = row_ys[r_idx + 1]
            ry_bot = row_ys[r_idx + 2]
            draw.rectangle([col_xs[0], ry_top, col_xs[1], ry_bot], fill=row_bg_colors[r_idx])
            draw.text((col_xs[0] + int(4.5 * scale), ry_top + int(1.3 * scale)), r_label, fill=(0, 0, 0), font=font_label)

        for c_idx in range(6):
            cx_left = col_xs[c_idx + 1]
            draw.text((cx_left + int(4.2 * scale), row_ys[0] + int(2.0 * scale)), str(c_idx + 1), fill=(0, 0, 0), font=font_label)

        draw.text((col_xs[0] + int(2.0 * scale), row_ys[7] + int(1.5 * scale)), "Summe \u03a3", fill=(0, 0, 0), font=font_label)

        for r_idx in range(1, 8):
            w_line = 2 if r_idx in (1, 7) else 1
            draw.line([(px0, row_ys[r_idx]), (px1, row_ys[r_idx])], fill=(0, 0, 0), width=w_line)

        draw.line([(col_xs[1], py0), (col_xs[1], py1)], fill=(0, 0, 0), width=1)
        for c_idx in range(2, 7):
            draw.line([(col_xs[c_idx], py0), (col_xs[c_idx], row_ys[7])], fill=(0, 0, 0), width=1)

        draw.rectangle([px0, py0, px1, py1], outline=(0, 0, 0), width=3)

        passe_rings = preset_passes[p_idx]
        for c_idx, ring_val in enumerate(passe_rings):
            r_idx = RING_VALUES.index(ring_val)
            cx_left = col_xs[c_idx + 1]
            cx_right = col_xs[c_idx + 2]
            ry_top = row_ys[r_idx + 1]
            ry_bot = row_ys[r_idx + 2]

            pad_x = (cx_right - cx_left) * 0.24
            pad_y = (ry_bot - ry_top) * 0.22
            jx1 = random.uniform(-1.5, 1.5)
            jy1 = random.uniform(-1.5, 1.5)
            jx2 = random.uniform(-1.5, 1.5)
            jy2 = random.uniform(-1.5, 1.5)

            pen_color = (20, 45, 135)
            pen_w = max(2, int(round(0.55 * scale)))
            draw.line(
                [(cx_left + pad_x + jx1, ry_top + pad_y + jy1), (cx_right - pad_x + jx2, ry_bot - pad_y + jy2)],
                fill=pen_color,
                width=pen_w,
            )
            draw.line(
                [(cx_right - pad_x + jx2, ry_top + pad_y + jy1), (cx_left + pad_x + jx1, ry_bot - pad_y + jy2)],
                fill=pen_color,
                width=pen_w,
            )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), preset_passes
