import io
import json
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import qrcode
from PIL import Image
from qrcode import base as qr_base
from qrcode import constants as qr_const
from qrcode import util as qr_util

# Map QR format info 2-bit EC indicator to qrcode constants
_FORMAT_EC_TO_CONST = {
    0b01: qr_const.ERROR_CORRECT_L,
    0b00: qr_const.ERROR_CORRECT_M,
    0b11: qr_const.ERROR_CORRECT_Q,
    0b10: qr_const.ERROR_CORRECT_H,
}

# Precompute all 32 valid 15-bit Format Information sequences: (ec_const, mask_pattern) -> 15-bit int
_VALID_FORMAT_WORDS: List[Tuple[int, int, int]] = []
for _ec_bits, _ec_const in _FORMAT_EC_TO_CONST.items():
    for _mask in range(8):
        _data5 = (_ec_bits << 3) | _mask
        _bits15 = qr_util.BCH_type_info(_data5)
        _VALID_FORMAT_WORDS.append((_ec_const, _mask, _bits15))


def encode_scorecard_qr_payload(
    half: int = 1,
    competition_name: str = "",
    shooter_name: str = "",
    club: str = "",
    date_str: str = "",
    class_name: str = "",
    distance: str = "",
    group_name: str = "",
    stand_number: str = "",
    shooter_id: Optional[int] = None,
    competition_id: Optional[int] = None,
) -> str:
    """
    Builds a compact UTF-8 string containing all header fields of the scorecard
    plus the half (1 = Zettel A, 2 = Zettel B).

    Format (pipe separated, compact => smaller QR version => larger modules => more robust scanning):
      BG1|<half>|<Wettbewerb>|<Name, Vorname>|<Verein>|<Datum>|<Klasse>|<Entfernung>|<Gruppe>|<Standnummer>|<sid>|<cid>
    """

    def _clean(val: Any) -> str:
        return str(val or "").replace("|", "/").replace("\n", " ").strip()

    fields = [
        "BG1",
        "2" if int(half) == 2 else "1",
        _clean(competition_name),
        _clean(shooter_name),
        _clean(club),
        _clean(date_str),
        _clean(class_name),
        _clean(distance),
        _clean(group_name),
        _clean(stand_number),
        str(int(shooter_id)) if shooter_id is not None else "",
        str(int(competition_id)) if competition_id is not None else "",
    ]
    return "|".join(fields)


def _parse_pipe_payload(text: str) -> Optional[Dict[str, Any]]:
    parts = text.split("|")
    if len(parts) < 10 or parts[0] != "BG1":
        return None
    parts = parts + [""] * (12 - len(parts))
    return {
        "h": parts[1],
        "w": parts[2],
        "n": parts[3],
        "v": parts[4],
        "d": parts[5],
        "k": parts[6],
        "e": parts[7],
        "g": parts[8],
        "s": parts[9],
        "sid": parts[10] or None,
        "cid": parts[11] or None,
    }


def parse_scorecard_qr_payload(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    Parses a decoded QR string into a normalized dictionary with all scorecard header fields.
    Supports the compact pipe format ('BG1|...'), compact JSON keys ('h','w','n','v','d','k','e','g','s')
    and full German JSON keys.
    """
    if not raw_text or not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    obj: Optional[Dict[str, Any]] = None
    if text.startswith("BG1|"):
        obj = _parse_pipe_payload(text)
    elif text.startswith("{"):
        try:
            obj = json.loads(text)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None

    half_raw = obj.get("h", obj.get("haelfte", obj.get("half", 1)))
    if str(half_raw).upper() == "B" or str(half_raw) == "2":
        half = 2
    else:
        half = 1

    full_name = str(obj.get("n", obj.get("name", obj.get("name_vorname", ""))) or "").strip()
    last_name = str(obj.get("last_name", "") or "").strip()
    first_name = str(obj.get("first_name", "") or "").strip()
    if full_name and (not last_name and not first_name):
        if "," in full_name:
            parts = full_name.split(",", 1)
            last_name = parts[0].strip()
            first_name = parts[1].strip()
        elif " " in full_name:
            parts = full_name.rsplit(" ", 1)
            first_name = parts[0].strip()
            last_name = parts[1].strip()
        else:
            last_name = full_name

    wettbewerb = str(obj.get("w", obj.get("wettbewerb", obj.get("competition", ""))) or "").strip()
    verein = str(obj.get("v", obj.get("verein", obj.get("club", ""))) or "").strip()
    datum = str(obj.get("d", obj.get("datum", obj.get("date", ""))) or "").strip()
    klasse = str(obj.get("k", obj.get("klasse", obj.get("class_name", ""))) or "").strip()
    entfernung = str(obj.get("e", obj.get("entfernung", obj.get("distance", ""))) or "").strip()
    gruppe = str(obj.get("g", obj.get("gruppe", obj.get("group_name", ""))) or "").strip()
    standnummer = str(obj.get("s", obj.get("standnummer", obj.get("stand_number", ""))) or "").strip()

    sid = obj.get("sid", obj.get("shooter_id"))
    cid = obj.get("cid", obj.get("competition_id"))

    return {
        "half": half,
        "half_label": "A" if half == 1 else "B",
        "wettbewerb": wettbewerb,
        "name": full_name or (f"{last_name}, {first_name}" if last_name and first_name else last_name),
        "last_name": last_name,
        "first_name": first_name,
        "verein": verein,
        "datum": datum,
        "klasse": klasse,
        "entfernung": entfernung,
        "gruppe": gruppe,
        "standnummer": standnummer,
        "shooter_id": int(sid) if sid is not None and str(sid).isdigit() else None,
        "competition_id": int(cid) if cid is not None and str(cid).isdigit() else None,
        "raw_payload": raw_text,
    }


def make_scorecard_qr_matrix(payload_str: str) -> List[List[bool]]:
    """Returns the boolean 2D module matrix (True = black) for a QR code."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qr_const.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(payload_str)
    qr.make(fit=True)
    return qr.modules


def make_scorecard_qr_image(payload_str: str, box_size: int = 8, border: int = 2) -> Image.Image:
    """Renders a crisp RGB PIL Image of the QR code."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qr_const.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img


# -------------------------------------------------------------------------
# Pure-Python + NumPy ISO/IEC 18004 QR Decoder with Reed-Solomon ECC
# -------------------------------------------------------------------------
def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return qr_base.EXP_TABLE[(qr_base.LOG_TABLE[a] + qr_base.LOG_TABLE[b]) % 255]


def _gf_div(a: int, b: int) -> int:
    if a == 0:
        return 0
    return qr_base.EXP_TABLE[(qr_base.LOG_TABLE[a] - qr_base.LOG_TABLE[b] + 255) % 255]


def _gf_poly_eval(poly: List[int], x: int) -> int:
    """Evaluates polynomial where poly[0] is constant term: poly[0] + poly[1]*x + ..."""
    if x == 0:
        return poly[0]
    res = 0
    for coeff in reversed(poly):
        res = _gf_mul(res, x) ^ coeff
    return res


def _gf_poly_mul(p1: List[int], p2: List[int]) -> List[int]:
    res = [0] * (len(p1) + len(p2) - 1)
    for i, c1 in enumerate(p1):
        if c1 == 0:
            continue
        for j, c2 in enumerate(p2):
            if c2 != 0:
                res[i + j] ^= _gf_mul(c1, c2)
    return res


def _rs_correct_block(codewords: List[int], num_ecc: int) -> Optional[List[int]]:
    """
    Corrects up to num_ecc // 2 byte errors in a QR Reed-Solomon block using
    Berlekamp-Massey + Chien search + Forney's algorithm over GF(2^8) (poly 0x11d).
    Returns the corrected data bytes, or None if uncorrectable.
    """
    n = len(codewords)
    num_data = n - num_ecc
    if num_ecc <= 0:
        return codewords[:num_data]

    # 1. Compute syndromes S_0 .. S_{num_ecc-1} at roots alpha^0 .. alpha^{num_ecc-1}
    syndromes = []
    max_s = 0
    for i in range(num_ecc):
        root = qr_base.EXP_TABLE[i]
        val = 0
        for cw in codewords:
            val = _gf_mul(val, root) ^ cw
        syndromes.append(val)
        if val != 0:
            max_s = 1

    if max_s == 0:
        return codewords[:num_data]

    # 2. Berlekamp-Massey algorithm to find error locator polynomial sigma(x)
    C = [1] + [0] * num_ecc
    B = [1] + [0] * num_ecc
    L = 0
    m = 1
    b = 1
    for i in range(num_ecc):
        d = syndromes[i]
        for j in range(1, L + 1):
            d ^= _gf_mul(C[j], syndromes[i - j])
        if d == 0:
            m += 1
        elif 2 * L <= i:
            T = C[:]
            factor = _gf_div(d, b)
            for j in range(num_ecc + 1 - m):
                C[j + m] ^= _gf_mul(factor, B[j])
            L = i + 1 - L
            B = T
            b = d
            m = 1
        else:
            factor = _gf_div(d, b)
            for j in range(num_ecc + 1 - m):
                C[j + m] ^= _gf_mul(factor, B[j])
            m += 1

    if L > num_ecc // 2:
        return None

    sigma = C[: L + 1]

    # 3. Chien search to find error locations
    err_pos = []
    for i in range(n):
        # Power of alpha for index i (where codewords[0] is highest degree n-1)
        x_inv = qr_base.EXP_TABLE[(255 - (n - 1 - i)) % 255]
        if _gf_poly_eval(sigma, x_inv) == 0:
            err_pos.append(i)

    if len(err_pos) != L:
        return None

    # 4. Forney's algorithm to compute error magnitudes
    omega = _gf_poly_mul(syndromes, sigma)[:num_ecc]
    sigma_deriv = [sigma[j] if (j % 2 == 1) else 0 for j in range(1, len(sigma))]

    corrected = list(codewords)
    for pos in err_pos:
        k = n - 1 - pos
        x_inv = qr_base.EXP_TABLE[(255 - k) % 255]
        num = _gf_poly_eval(omega, x_inv)
        den = _gf_poly_eval(sigma_deriv, x_inv)
        if den == 0:
            return None
        # Since generator roots start at alpha^0, error magnitude is num / den
        err_mag = _gf_div(num, den)
        corrected[pos] ^= err_mag

    # Verify all syndromes are now 0
    for i in range(num_ecc):
        root = qr_base.EXP_TABLE[i]
        val = 0
        for cw in corrected:
            val = _gf_mul(val, root) ^ cw
        if val != 0:
            return None

    return corrected[:num_data]


def _decode_qr_bitstream(data_bytes: List[int], version: int) -> Optional[str]:
    """Decodes the QR data codeword stream (Byte / Alphanumeric / Numeric modes) into a string."""
    bits = []
    for b in data_bytes:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    idx = 0
    total_bits = len(bits)

    def _read_bits(count: int) -> int:
        nonlocal idx
        val = 0
        for _ in range(count):
            val = (val << 1) | (bits[idx] if idx < total_bits else 0)
            idx += 1
        return val

    out_chunks: List[bytes] = []
    while idx + 4 <= total_bits:
        mode = _read_bits(4)
        if mode == 0b0000:
            # Terminator
            break
        elif mode == 0b0100:
            # 8-bit Byte Mode
            count_bits = 8 if version < 10 else 16
            if idx + count_bits > total_bits:
                return None
            char_count = _read_bits(count_bits)
            if idx + char_count * 8 > total_bits:
                return None
            chunk = bytearray()
            for _ in range(char_count):
                chunk.append(_read_bits(8))
            out_chunks.append(bytes(chunk))
        elif mode == 0b0010:
            # Alphanumeric mode
            alphanum = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
            count_bits = 9 if version < 10 else (11 if version < 27 else 13)
            char_count = _read_bits(count_bits)
            chars = []
            while char_count >= 2:
                v = _read_bits(11)
                chars.append(alphanum[v // 45])
                chars.append(alphanum[v % 45])
                char_count -= 2
            if char_count == 1:
                chars.append(alphanum[_read_bits(6)])
            out_chunks.append("".join(chars).encode("utf-8"))
        elif mode == 0b0001:
            # Numeric mode
            count_bits = 10 if version < 10 else (12 if version < 27 else 14)
            char_count = _read_bits(count_bits)
            chars = []
            while char_count >= 3:
                chars.append(f"{_read_bits(10):03d}")
                char_count -= 3
            if char_count == 2:
                chars.append(f"{_read_bits(7):02d}")
            elif char_count == 1:
                chars.append(f"{_read_bits(4):01d}")
            out_chunks.append("".join(chars).encode("utf-8"))
        elif mode == 0b0111:
            # ECI mode (skip 8-bit assignment value)
            _read_bits(8)
        else:
            return None

    if not out_chunks:
        return None
    raw = b"".join(out_chunks)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("latin-1")
        except Exception:
            return None


def _build_function_mask_for_version(version: int) -> np.ndarray:
    """
    Uses qrcode.QRCode to build the exact boolean mask of function modules
    (Finder patterns, separators, timing patterns, alignment patterns, format info, version info).
    True = function module, False = data/ECC module.
    """
    qr = qrcode.QRCode(version=version, error_correction=qr_const.ERROR_CORRECT_M)
    n = version * 4 + 17
    qr.modules_count = n
    qr.modules = [[None] * n for _ in range(n)]
    qr.setup_position_probe_pattern(0, 0)
    qr.setup_position_probe_pattern(n - 7, 0)
    qr.setup_position_probe_pattern(0, n - 7)
    qr.setup_position_adjust_pattern()
    qr.setup_timing_pattern()
    qr.setup_type_info(True, 0)
    if version >= 7:
        qr.setup_type_number(True)

    func_mask = np.zeros((n, n), dtype=bool)
    for r in range(n):
        for c in range(n):
            if qr.modules[r][c] is not None:
                func_mask[r, c] = True
    return func_mask


def _decode_sampled_qr_matrix(matrix: np.ndarray, version: int) -> Optional[str]:
    """
    Given a boolean (N x N) sampled QR module matrix (True = dark) for `version`,
    decodes Format Info, unmasks, de-interleaves RS blocks, runs Reed-Solomon ECC,
    and parses the UTF-8 string.
    """
    n = version * 4 + 17
    if matrix.shape != (n, n):
        return None

    # 1. Read both 15-bit Format Information copies
    vert_bits = 0
    for i in range(15):
        if i < 6:
            mod = bool(matrix[i, 8])
        elif i < 8:
            mod = bool(matrix[i + 1, 8])
        else:
            mod = bool(matrix[n - 15 + i, 8])
        if mod:
            vert_bits |= 1 << i

    horiz_bits = 0
    for i in range(15):
        if i < 8:
            mod = bool(matrix[8, n - i - 1])
        elif i < 9:
            mod = bool(matrix[8, 15 - i])
        else:
            mod = bool(matrix[8, 15 - i - 1])
        if mod:
            horiz_bits |= 1 << i

    # Find best matching (ec_const, mask_pattern) by Hamming distance across both format copies
    candidates = []
    for ec_const, mask_pat, valid_bits in _VALID_FORMAT_WORDS:
        d_v = bin(vert_bits ^ valid_bits).count("1")
        d_h = bin(horiz_bits ^ valid_bits).count("1")
        candidates.append((min(d_v, d_h), d_v + d_h, ec_const, mask_pat))
    candidates.sort(key=lambda x: (x[0], x[1]))

    func_mask = _build_function_mask_for_version(version)

    # Try top format candidates (usually the very first one has distance 0)
    for min_d, _, ec_const, mask_pat in candidates[:4]:
        if min_d > 5:
            break

        mask_fn = qr_util.mask_func(mask_pat)
        codewords: List[int] = []
        inc = -1
        row = n - 1
        bit_idx = 7
        cur_byte = 0

        for col in range(n - 1, 0, -2):
            if col <= 6:
                col -= 1
            col_pair = (col, col - 1)
            while True:
                for c in col_pair:
                    if not func_mask[row, c]:
                        dark = bool(matrix[row, c])
                        if mask_fn(row, c):
                            dark = not dark
                        if dark:
                            cur_byte |= 1 << bit_idx
                        bit_idx -= 1
                        if bit_idx == -1:
                            codewords.append(cur_byte)
                            cur_byte = 0
                            bit_idx = 7
                row += inc
                if row < 0 or row >= n:
                    row -= inc
                    inc = -inc
                    break

        # De-interleave RS blocks according to version and ec_const
        rs_block_specs = qr_base.rs_blocks(version, ec_const)
        total_expected = sum(b.total_count for b in rs_block_specs)
        if len(codewords) < total_expected:
            continue

        num_blocks = len(rs_block_specs)
        data_blocks: List[List[int]] = [[] for _ in range(num_blocks)]
        ecc_blocks: List[List[int]] = [[] for _ in range(num_blocks)]

        max_data_len = max(b.data_count for b in rs_block_specs)
        max_ecc_len = max(b.total_count - b.data_count for b in rs_block_specs)

        cw_ptr = 0
        for i in range(max_data_len):
            for b_idx, b_spec in enumerate(rs_block_specs):
                if i < b_spec.data_count and cw_ptr < len(codewords):
                    data_blocks[b_idx].append(codewords[cw_ptr])
                    cw_ptr += 1

        for i in range(max_ecc_len):
            for b_idx, b_spec in enumerate(rs_block_specs):
                ecc_len = b_spec.total_count - b_spec.data_count
                if i < ecc_len and cw_ptr < len(codewords):
                    ecc_blocks[b_idx].append(codewords[cw_ptr])
                    cw_ptr += 1

        corrected_all_data: List[int] = []
        ok = True
        for b_idx, b_spec in enumerate(rs_block_specs):
            ecc_len = b_spec.total_count - b_spec.data_count
            full_block = data_blocks[b_idx] + ecc_blocks[b_idx]
            corr = _rs_correct_block(full_block, ecc_len)
            if corr is None:
                ok = False
                break
            corrected_all_data.extend(corr)

        if not ok:
            continue

        decoded_str = _decode_qr_bitstream(corrected_all_data, version)
        if decoded_str:
            return decoded_str

    return None


def _compute_homography(src_pts: np.ndarray, dst_pts: np.ndarray) -> np.ndarray:
    """Computes 3x3 perspective transform H mapping src_pts (4x2) -> dst_pts (4x2)."""
    A = np.zeros((8, 8), dtype=np.float64)
    b = np.zeros(8, dtype=np.float64)
    for i in range(4):
        x, y = src_pts[i]
        u, v = dst_pts[i]
        A[2 * i] = [x, y, 1, 0, 0, 0, -x * u, -y * u]
        b[2 * i] = u
        A[2 * i + 1] = [0, 0, 0, x, y, 1, -x * v, -y * v]
        b[2 * i + 1] = v
    h = np.linalg.solve(A, b)
    H = np.array([[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]], dtype=np.float64)
    return H


def _sample_qr_grid_homography(gray: np.ndarray, H: np.ndarray, n: int) -> np.ndarray:
    """
    Samples an (n x n) boolean module matrix from grayscale image `gray` using
    3x3 homography H (mapping module coordinates (col+0.5, row+0.5) -> pixel (x, y)).
    Samples a 3x3 sub-grid per module for high resistance against pixel aliasing.
    """
    h_img, w_img = gray.shape
    grid_vals = np.zeros((n, n), dtype=np.float32)

    cols = np.arange(n, dtype=np.float64)
    rows = np.arange(n, dtype=np.float64)
    cc, rr = np.meshgrid(cols, rows)

    for dy in (-0.18, 0.0, 0.18):
        for dx in (-0.18, 0.0, 0.18):
            mx = cc + 0.5 + dx
            my = rr + 0.5 + dy
            denom = H[2, 0] * mx + H[2, 1] * my + H[2, 2]
            px = (H[0, 0] * mx + H[0, 1] * my + H[0, 2]) / denom
            py = (H[1, 0] * mx + H[1, 1] * my + H[1, 2]) / denom
            ix = np.clip(np.round(px).astype(int), 0, w_img - 1)
            iy = np.clip(np.round(py).astype(int), 0, h_img - 1)
            grid_vals += gray[iy, ix]

    grid_vals /= 9.0
    # Local adaptive threshold across the sampled N x N module grid
    thresh = 0.5 * (float(np.percentile(grid_vals, 18)) + float(np.percentile(grid_vals, 82)))
    return grid_vals < thresh


def _find_finder_pattern_centers(dark: np.ndarray, gray: np.ndarray) -> List[Tuple[float, float, float]]:
    """
    Finds candidate QR Finder Pattern centers (cx, cy, est_module_size) in a binary dark mask.
    Looks for solid ~3x3-module dark squares that are surrounded by a white ring at radius ~2*m
    and a black frame at radius ~3*m in all 8 compass directions.
    """
    h, w = dark.shape
    visited = np.zeros_like(dark, dtype=bool)
    candidates: List[Tuple[float, float, float, float]] = []

    ys, xs = np.where(dark)
    for sy, sx in zip(ys, xs):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        min_x = max_x = sx
        min_y = max_y = sy
        sum_x = 0.0
        sum_y = 0.0
        count = 0

        while stack:
            cy, cx = stack.pop()
            count += 1
            sum_x += cx
            sum_y += cy
            if cx < min_x:
                min_x = cx
            if cx > max_x:
                max_x = cx
            if cy < min_y:
                min_y = cy
            if cy > max_y:
                max_y = cy

            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and dark[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))

        bw = max_x - min_x + 1
        bh = max_y - min_y + 1
        if bw < 5 or bh < 5 or bw > min(w, h) * 0.35 or bh > min(w, h) * 0.35:
            continue

        aspect = bw / float(bh)
        if not (0.65 <= aspect <= 1.52):
            continue

        fill_ratio = count / float(bw * bh)
        if fill_ratio < 0.52:
            continue

        cx_f = sum_x / float(count)
        cy_f = sum_y / float(count)
        m = 0.5 * (bw + bh) / 3.0  # 1 module size (inner square is 3x3 modules)
        if m < 1.6:
            continue

        # Check concentric square signature (tolerating rotation up to +/- 20 deg):
        # offset = 2.0 * m -> white 5x5 separator ring (dark == False)
        # offset = 3.0 * m -> black 7x7 outer frame (dark == True)
        # offset = 4.1 * m -> outer white quiet zone / separator (dark == False)
        base_dirs = [
            (1.0, 0.0),
            (-1.0, 0.0),
            (0.0, 1.0),
            (0.0, -1.0),
            (0.92, 0.92),
            (-0.92, 0.92),
            (0.92, -0.92),
            (-0.92, -0.92),
        ]
        matched_ring = False
        best_ring_score = 0.0
        for ang_deg in (0.0, -10.0, 10.0):
            rad = math.radians(ang_deg)
            ca, sa = math.cos(rad), math.sin(rad)
            white_ring_hits = 0
            black_ring_hits = 0
            outer_white_hits = 0
            valid_dirs = 0

            for bdx, bdy in base_dirs:
                dx = bdx * ca - bdy * sa
                dy = bdx * sa + bdy * ca
                x2, y2 = int(round(cx_f + dx * 2.0 * m)), int(round(cy_f + dy * 2.0 * m))
                x3, y3 = int(round(cx_f + dx * 3.0 * m)), int(round(cy_f + dy * 3.0 * m))
                x4, y4 = int(round(cx_f + dx * 4.15 * m)), int(round(cy_f + dy * 4.15 * m))
                if not (0 <= x3 < w and 0 <= y3 < h):
                    continue
                valid_dirs += 1
                if not dark[y2, x2]:
                    white_ring_hits += 1
                if dark[y3, x3]:
                    black_ring_hits += 1
                if 0 <= x4 < w and 0 <= y4 < h and not dark[y4, x4]:
                    outer_white_hits += 1

            if valid_dirs >= 6 and white_ring_hits >= 5 and black_ring_hits >= 5 and outer_white_hits >= 4:
                matched_ring = True
                best_ring_score = max(best_ring_score, white_ring_hits + black_ring_hits + 0.5 * outer_white_hits)
                break

        if matched_ring:
            # Refine sub-pixel center of the inner 3x3 dark square using inverse grayscale weights
            r_box = max(2, int(round(1.55 * m)))
            ix0, ix1 = max(0, int(round(cx_f)) - r_box), min(w, int(round(cx_f)) + r_box + 1)
            iy0, iy1 = max(0, int(round(cy_f)) - r_box), min(h, int(round(cy_f)) + r_box + 1)
            patch_d = dark[iy0:iy1, ix0:ix1]
            if patch_d.any():
                py_idx, px_idx = np.where(patch_d)
                weights = np.clip(255.0 - gray[iy0:iy1, ix0:ix1][py_idx, px_idx], 10.0, 255.0)
                cx_f = float(ix0 + (px_idx * weights).sum() / weights.sum())
                cy_f = float(iy0 + (py_idx * weights).sum() / weights.sum())

            score = best_ring_score + fill_ratio
            candidates.append((score, cx_f, cy_f, m))

    # Deduplicate nearby centers
    candidates.sort(key=lambda item: item[0], reverse=True)
    unique: List[Tuple[float, float, float]] = []
    for sc, cx, cy, m in candidates:
        if any(math.hypot(cx - ux, cy - uy) < max(m, um) * 2.5 for ux, uy, um in unique):
            continue
        unique.append((cx, cy, m))
    return unique


def _refine_alignment_pattern_center(
    dark: np.ndarray,
    gray: np.ndarray,
    pred_x: float,
    pred_y: float,
    m: float,
) -> Tuple[float, float]:
    """
    Locates the exact sub-pixel center of the bottom-right 5x5 Alignment Pattern
    near (pred_x, pred_y): 1x1 dark center, 3x3 white ring at radius ~1*m, 5x5 dark ring at radius ~2*m.
    """
    h, w = dark.shape
    search_r = max(4, int(round(3.5 * m)))
    x0 = max(2, int(round(pred_x)) - search_r)
    x1 = min(w - 2, int(round(pred_x)) + search_r + 1)
    y0 = max(2, int(round(pred_y)) - search_r)
    y1 = min(h - 2, int(round(pred_y)) + search_r + 1)

    best_score = -1e9
    best_x, best_y = pred_x, pred_y
    r1 = max(1, int(round(1.0 * m)))
    r2 = max(2, int(round(2.0 * m)))

    for py in range(y0, y1):
        for px in range(x0, x1):
            if not dark[py, px]:
                continue
            if px - r2 < 0 or px + r2 >= w or py - r2 < 0 or py + r2 >= h:
                continue
            # White ring at r1 in 4 cardinal directions
            w_hits = (
                (0 if dark[py, px - r1] else 1)
                + (0 if dark[py, px + r1] else 1)
                + (0 if dark[py - r1, px] else 1)
                + (0 if dark[py + r1, px] else 1)
            )
            # Black ring at r2 in 4 cardinal directions
            b_hits = (
                (1 if dark[py, px - r2] else 0)
                + (1 if dark[py, px + r2] else 0)
                + (1 if dark[py - r2, px] else 0)
                + (1 if dark[py + r2, px] else 0)
            )
            dist_pen = 0.08 * math.hypot(px - pred_x, py - pred_y) / max(1.0, m)
            sc = w_hits + b_hits - dist_pen - gray[py, px] / 255.0
            if sc > best_score:
                best_score = sc
                best_x, best_y = float(px), float(py)

    # Sub-pixel centroid of the 1x1 dark center blob around (best_x, best_y)
    rb = max(1, int(round(0.65 * m)))
    ix0, ix1 = max(0, int(round(best_x)) - rb), min(w, int(round(best_x)) + rb + 1)
    iy0, iy1 = max(0, int(round(best_y)) - rb), min(h, int(round(best_y)) + rb + 1)
    sub_d = dark[iy0:iy1, ix0:ix1]
    if sub_d.any():
        py_i, px_i = np.where(sub_d)
        weights = np.clip(255.0 - gray[iy0:iy1, ix0:ix1][py_i, px_i], 10.0, 255.0)
        best_x = float(ix0 + (px_i * weights).sum() / weights.sum())
        best_y = float(iy0 + (py_i * weights).sum() / weights.sum())

    return best_x, best_y



# -------------------------------------------------------------------------
# Robust Finder Pattern detection (scanline 1:1:3:1:1 ratio + cross-checks)
# -------------------------------------------------------------------------
def _runs_of(line: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run-length decomposition of a 1D boolean array -> (starts, lengths, values)."""
    n = line.shape[0]
    change = np.flatnonzero(line[1:] != line[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [n]))
    return starts, ends - starts, line[starts]


def _finder_ratio_ok(r: np.ndarray, tol: float = 0.62) -> bool:
    total = float(r.sum())
    if total < 7.0:
        return False
    m = total / 7.0
    mv = m * tol
    return (
        abs(r[0] - m) < mv
        and abs(r[1] - m) < mv
        and abs(r[2] - 3.0 * m) < 3.0 * mv
        and abs(r[3] - m) < mv
        and abs(r[4] - m) < mv
    )


def _cross_check_line(line: np.ndarray, pos: int) -> Optional[Tuple[float, float]]:
    """
    Checks whether the 1D line has a 1:1:3:1:1 (dark-light-DARK-light-dark) pattern centered
    on the dark run containing `pos`. Returns (refined_center, total_width) or None.
    """
    if pos < 0 or pos >= line.shape[0] or not line[pos]:
        return None
    starts, lengths, vals = _runs_of(line)
    k = int(np.searchsorted(starts, pos, side="right") - 1)
    if k < 2 or k + 2 >= len(starts):
        return None
    r = lengths[k - 2 : k + 3].astype(np.float64)
    if not (vals[k - 2] and not vals[k - 1] and vals[k] and not vals[k + 1] and vals[k + 2]):
        return None
    if not _finder_ratio_ok(r):
        return None
    return float(starts[k]) + lengths[k] / 2.0 - 0.5, float(r.sum())


def _find_finder_patterns_scanline(dark: np.ndarray) -> List[Tuple[float, float, float]]:
    """
    Classic (ZXing-style) finder pattern search: scans every row for 1:1:3:1:1 run ratios,
    cross-checks vertically and horizontally, then clusters hits.
    Robust against blur, uneven lighting and moderate rotation/perspective.
    Returns list of (cx, cy, module_size) sorted by confidence.
    """
    h, w = dark.shape
    raw_hits: List[Tuple[float, float, float]] = []
    col_cache: Dict[int, np.ndarray] = {}

    for y in range(0, h):
        row = dark[y]
        starts, lengths, vals = _runs_of(row)
        nr = len(starts)
        if nr < 5:
            continue
        L = lengths.astype(np.float64)
        V = vals
        a, b, c, d, e = L[:-4], L[1:-3], L[2:-2], L[3:-1], L[4:]
        tot = a + b + c + d + e
        m = tot / 7.0
        mv = m * 0.62
        ok = (
            V[:-4]
            & (tot >= 7.0)
            & (np.abs(a - m) < mv)
            & (np.abs(b - m) < mv)
            & (np.abs(c - 3.0 * m) < 3.0 * mv)
            & (np.abs(d - m) < mv)
            & (np.abs(e - m) < mv)
        )
        for i in np.flatnonzero(ok):
            cx = int(starts[i + 2] + lengths[i + 2] // 2)
            if cx not in col_cache:
                col_cache[cx] = dark[:, cx]
            vres = _cross_check_line(col_cache[cx], y)
            if vres is None:
                continue
            cy_f, v_total = vres
            h_total = float(tot[i])
            if not (0.55 <= v_total / h_total <= 1.8):
                continue
            cy_i = int(round(cy_f))
            hres = _cross_check_line(dark[cy_i], cx)
            if hres is None:
                continue
            cx_f, h_total2 = hres
            raw_hits.append((cx_f, cy_f, (h_total2 + v_total) / 14.0))

    # Cluster hits (a true finder pattern produces many hits across ~3 module rows)
    clusters: List[List[float]] = []  # [sum_x, sum_y, sum_m, count]
    for hx, hy, hm in raw_hits:
        merged = False
        for cl in clusters:
            cnt = cl[3]
            mx, my, mm = cl[0] / cnt, cl[1] / cnt, cl[2] / cnt
            if math.hypot(hx - mx, hy - my) < 2.2 * max(hm, mm) and 0.55 < hm / mm < 1.8:
                cl[0] += hx
                cl[1] += hy
                cl[2] += hm
                cl[3] += 1
                merged = True
                break
        if not merged:
            clusters.append([hx, hy, hm, 1])

    clusters.sort(key=lambda cl: cl[3], reverse=True)
    return [(cl[0] / cl[3], cl[1] / cl[3], cl[2] / cl[3]) for cl in clusters]


def _local_mean(gray: np.ndarray, win: int) -> np.ndarray:
    h, w = gray.shape
    win = max(3, win | 1)
    pad = win // 2
    padded = np.pad(gray, pad, mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant", constant_values=0).cumsum(axis=0).cumsum(axis=1)
    y0 = np.arange(h)
    x0 = np.arange(w)
    y1 = y0 + win
    x1 = x0 + win
    return (
        integral[y1[:, None], x1[None, :]]
        - integral[y0[:, None], x1[None, :]]
        - integral[y1[:, None], x0[None, :]]
        + integral[y0[:, None], x0[None, :]]
    ) / float(win * win)


def _otsu_threshold(gray: np.ndarray) -> float:
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 128.0
    omega = np.cumsum(hist) / total
    mu = np.cumsum(hist * np.arange(256)) / total
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    denom[denom == 0] = 1e-12
    sigma_b = (mu_t * omega - mu) ** 2 / denom
    return float(np.argmax(sigma_b))


def _sample_qr_grid_binary(dark: np.ndarray, H: np.ndarray, n: int) -> np.ndarray:
    """Samples module matrix from a binarized image via majority vote of a 3x3 sub-grid."""
    h_img, w_img = dark.shape
    cc, rr = np.meshgrid(np.arange(n, dtype=np.float64), np.arange(n, dtype=np.float64))
    votes = np.zeros((n, n), dtype=np.float32)
    for dy in (-0.2, 0.0, 0.2):
        for dx in (-0.2, 0.0, 0.2):
            mx = cc + 0.5 + dx
            my = rr + 0.5 + dy
            denom = H[2, 0] * mx + H[2, 1] * my + H[2, 2]
            px = (H[0, 0] * mx + H[0, 1] * my + H[0, 2]) / denom
            py = (H[1, 0] * mx + H[1, 1] * my + H[1, 2]) / denom
            ix = np.clip(np.round(px).astype(int), 0, w_img - 1)
            iy = np.clip(np.round(py).astype(int), 0, h_img - 1)
            votes += dark[iy, ix]
    return votes >= 4.5


def _decode_from_finder_centers(
    centers: List[Tuple[float, float, float]],
    gray: np.ndarray,
    dark: np.ndarray,
) -> Optional[str]:
    """Selects the best (TL, TR, BL) finder triple(s), builds a homography and decodes."""
    if len(centers) < 3:
        return None

    n_c = min(len(centers), 12)
    triples = []
    for i in range(n_c):
        for j in range(n_c):
            if j == i:
                continue
            for k in range(n_c):
                if k == i or k == j:
                    continue
                tl, tr, bl = centers[i], centers[j], centers[k]
                v1x, v1y = tr[0] - tl[0], tr[1] - tl[1]
                v2x, v2y = bl[0] - tl[0], bl[1] - tl[1]
                d1 = math.hypot(v1x, v1y)
                d2 = math.hypot(v2x, v2y)
                if d1 < 14 or d2 < 14:
                    continue
                if not (0.7 <= d1 / d2 <= 1.43):
                    continue
                # Cross product > 0: TR is clockwise from TL, BL below (image coordinates)
                if v1x * v2y - v1y * v2x <= 0:
                    continue
                cos_ang = abs(v1x * v2x + v1y * v2y) / (d1 * d2)
                if cos_ang > 0.3:
                    continue
                m_avg = (tl[2] + tr[2] + bl[2]) / 3.0
                m_diff = max(abs(tl[2] - m_avg), abs(tr[2] - m_avg), abs(bl[2] - m_avg)) / m_avg
                if m_diff > 0.5:
                    continue
                # Span in modules must be plausible for QR versions 1..20 (n_mod - 7 = 14..90)
                span_mod = 0.5 * (d1 + d2) / m_avg
                if not (11.0 <= span_mod <= 95.0):
                    continue
                triples.append((cos_ang + m_diff + 0.3 * abs(1.0 - d1 / d2), tl, tr, bl, d1, d2, m_avg))

    triples.sort(key=lambda x: x[0])
    for _, tl, tr, bl, d1, d2, m_avg in triples[:6]:
        span_px = 0.5 * (d1 + d2)
        est_n = span_px / m_avg + 7.0
        est_v = int(round((est_n - 17.0) / 4.0))
        est_v = max(1, min(20, est_v))

        cand_versions: List[int] = []
        for dv in (0, -1, 1, -2, 2, -3, 3):
            v = est_v + dv
            if 1 <= v <= 20 and v not in cand_versions:
                cand_versions.append(v)

        for v in cand_versions:
            n_mod = 17 + 4 * v
            m_v = span_px / float(n_mod - 7)
            dst_variants = []
            src_pts_aff = np.array(
                [[3.5, 3.5], [n_mod - 3.5, 3.5], [3.5, n_mod - 3.5], [n_mod - 3.5, n_mod - 3.5]],
                dtype=np.float64,
            )
            br_x = tr[0] + bl[0] - tl[0]
            br_y = tr[1] + bl[1] - tl[1]
            dst_aff = np.array([[tl[0], tl[1]], [tr[0], tr[1]], [bl[0], bl[1]], [br_x, br_y]], dtype=np.float64)

            if v >= 2:
                frac = float(n_mod - 10) / float(n_mod - 7)
                pred_ax = tl[0] + frac * ((tr[0] - tl[0]) + (bl[0] - tl[0]))
                pred_ay = tl[1] + frac * ((tr[1] - tl[1]) + (bl[1] - tl[1]))
                try:
                    ax, ay = _refine_alignment_pattern_center(dark, gray, pred_ax, pred_ay, m_v)
                    src_pts = np.array(
                        [[3.5, 3.5], [n_mod - 3.5, 3.5], [3.5, n_mod - 3.5], [n_mod - 6.5, n_mod - 6.5]],
                        dtype=np.float64,
                    )
                    dst_pts = np.array([[tl[0], tl[1]], [tr[0], tr[1]], [bl[0], bl[1]], [ax, ay]], dtype=np.float64)
                    dst_variants.append((src_pts, dst_pts))
                except Exception:
                    pass
            dst_variants.append((src_pts_aff, dst_aff))

            for src_pts, dst_pts in dst_variants:
                try:
                    H = _compute_homography(src_pts, dst_pts)
                except Exception:
                    continue
                for matrix in (_sample_qr_grid_homography(gray, H, n_mod), _sample_qr_grid_binary(dark, H, n_mod)):
                    decoded = _decode_sampled_qr_matrix(matrix, v)
                    if decoded:
                        return decoded
    return None


def _try_decode_from_crop(gray: np.ndarray) -> Optional[str]:
    """
    Attempts to locate and decode a QR code inside a grayscale image array.
    Tries several binarizations (adaptive local mean + Otsu) and two finder detectors
    (scanline ratio detector and blob/ring detector).
    """
    h, w = gray.shape
    if h < 35 or w < 35:
        return None

    win = max(31, (min(w, h) // 5) | 1)
    lm = _local_mean(gray, win)
    otsu = _otsu_threshold(gray)

    binarizations = [
        gray < (lm - 6.0),
        gray < (lm - 2.0),
        gray < otsu,
    ]

    tried_keys = set()
    for dark in binarizations:
        key = int(dark.sum())
        if key in tried_keys:
            continue
        tried_keys.add(key)

        centers = _find_finder_patterns_scanline(dark)
        decoded = _decode_from_finder_centers(centers, gray, dark)
        if decoded:
            return decoded

    # Fallback: legacy blob/ring finder detector
    for offset in (10.0, 4.0):
        dark = gray < (lm - offset)
        centers = _find_finder_pattern_centers(dark, gray)
        decoded = _decode_from_finder_centers(centers, gray, dark)
        if decoded:
            return decoded

    return None


def _resize_gray(gray: np.ndarray, factor: float) -> np.ndarray:
    h, w = gray.shape
    nw, nh = max(1, int(round(w * factor))), max(1, int(round(h * factor)))
    im = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8))
    resample = Image.Resampling.BICUBIC if factor > 1.0 else Image.Resampling.LANCZOS
    return np.array(im.resize((nw, nh), resample), dtype=np.float32)


def _try_decode_multiscale(gray: np.ndarray) -> Optional[str]:
    """Runs the crop decoder at several scales (upscaling small crops, downscaling huge ones)."""
    h, w = gray.shape
    max_dim = max(h, w)
    scales: List[float] = []
    if max_dim > 1700:
        scales.append(1400.0 / max_dim)
    scales.append(1.0)
    if max_dim <= 1100:
        scales.append(2.0)
    elif max_dim <= 1700:
        scales.append(1.5)

    for s in scales:
        g = gray if abs(s - 1.0) < 1e-6 else _resize_gray(gray, s)
        raw = _try_decode_from_crop(g)
        if raw:
            return raw
    return None


def decode_scorecard_qr_from_pil(
    img: Image.Image,
    passe_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    scale_from_eval_img: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """
    Detects and decodes a bgScore QR code from a photographed scorecard image (`img`).
    1. If `passe_boxes` were detected, uses their geometry to crop the top-right header
       region (generous margin) at full resolution where the QR code is located.
    2. Also checks the top-right region of the image, the whole upper half, and the full image.
    Returns the parsed dictionary (from `parse_scorecard_qr_payload`) or None.
    """
    rgb = np.array(img.convert("RGB"))
    h, w, _ = rgb.shape
    gray = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.float32)

    crops_to_test: List[np.ndarray] = []

    # 1. Geometry-guided crop from detected Passe 1 (top-left) and Passe 2 (top-right) boxes
    if passe_boxes and len(passe_boxes) >= 2:
        p1 = passe_boxes[0]
        p2 = passe_boxes[1]
        gx0 = p1[0] * scale_from_eval_img
        gx1 = p2[2] * scale_from_eval_img
        gy0 = min(p1[1], p2[1]) * scale_from_eval_img
        gw = max(50.0, gx1 - gx0)
        # Official layout: QR code at x = 165..193.5 mm, y = 15.2..43.7 mm; Passe grid starts
        # at about y = 63.5 mm with width ~175 mm -> generous crop around that region.
        cx0 = max(0, int(round(gx0 + 0.66 * gw)))
        cx1 = min(w, int(round(gx0 + 1.12 * gw)))
        cy0 = max(0, int(round(gy0 - 0.40 * gw)))
        cy1 = min(h, int(round(gy0 + 0.02 * gw)))
        if cx1 - cx0 >= 40 and cy1 - cy0 >= 40:
            crops_to_test.append(gray[cy0:cy1, cx0:cx1])

    # 2. Top-right region of the photo
    crops_to_test.append(gray[0 : int(h * 0.40), int(w * 0.50) : w])

    # 3. Whole upper half (sheet shifted / rotated)
    crops_to_test.append(gray[0 : int(h * 0.55), :])

    # 4. Full image fallback (e.g. if user zoomed directly onto the QR code)
    crops_to_test.append(gray)

    for crop in crops_to_test:
        if crop.shape[0] < 35 or crop.shape[1] < 35:
            continue
        raw_str = _try_decode_multiscale(crop)
        if raw_str:
            parsed = parse_scorecard_qr_payload(raw_str)
            if parsed:
                return parsed

    return None
