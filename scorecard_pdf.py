import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import qr_codec

_FONTS_REGISTERED = False
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"
FONT_BOLD_ITALIC = "Helvetica-BoldOblique"


def _ensure_fonts() -> None:
    global _FONTS_REGISTERED, FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_BOLD_ITALIC
    if _FONTS_REGISTERED:
        return
    _FONTS_REGISTERED = True

    win_fonts = r"C:\Windows\Fonts"
    calibri_map = {
        "Calibri": os.path.join(win_fonts, "calibri.ttf"),
        "Calibri-Bold": os.path.join(win_fonts, "calibrib.ttf"),
        "Calibri-Italic": os.path.join(win_fonts, "calibrii.ttf"),
        "Calibri-BoldItalic": os.path.join(win_fonts, "calibriz.ttf"),
    }
    if all(os.path.exists(p) for p in calibri_map.values()):
        try:
            for name, path in calibri_map.items():
                pdfmetrics.registerFont(TTFont(name, path))
            FONT_REGULAR = "Calibri"
            FONT_BOLD = "Calibri-Bold"
            FONT_ITALIC = "Calibri-Italic"
            FONT_BOLD_ITALIC = "Calibri-BoldItalic"
            return
        except Exception:
            pass

    arial_map = {
        "Arial": os.path.join(win_fonts, "arial.ttf"),
        "Arial-Bold": os.path.join(win_fonts, "arialbd.ttf"),
        "Arial-Italic": os.path.join(win_fonts, "ariali.ttf"),
        "Arial-BoldItalic": os.path.join(win_fonts, "arialbi.ttf"),
    }
    if os.path.exists(arial_map["Arial"]) and os.path.exists(arial_map["Arial-Bold"]):
        try:
            pdfmetrics.registerFont(TTFont("Arial", arial_map["Arial"]))
            pdfmetrics.registerFont(TTFont("Arial-Bold", arial_map["Arial-Bold"]))
            FONT_REGULAR = "Arial"
            FONT_BOLD = "Arial-Bold"
            if os.path.exists(arial_map["Arial-Italic"]):
                pdfmetrics.registerFont(TTFont("Arial-Italic", arial_map["Arial-Italic"]))
                FONT_ITALIC = "Arial-Italic"
            if os.path.exists(arial_map["Arial-BoldItalic"]):
                pdfmetrics.registerFont(TTFont("Arial-BoldItalic", arial_map["Arial-BoldItalic"]))
                FONT_BOLD_ITALIC = "Arial-BoldItalic"
        except Exception:
            pass


def format_date_de(date_str: str) -> str:
    """Formats ISO YYYY-MM-DD dates as DD.MM.YYYY for German scorecards."""
    s = (date_str or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return s


def resolve_distance(class_name: str = "", comp_distance: str = "", explicit_distance: str = "") -> str:
    """
    Determines the shooting distance ('7 m' or '5 m') for a shooter.
    In Blasrohr competitions, classes containing '5 m' or '5m' (e.g. 'Schüler III (5 m)')
    shoot at 5 m; all other classes default to 7 m unless overridden.
    """
    if explicit_distance and explicit_distance.strip():
        return explicit_distance.strip()
    cn = (class_name or "").lower()
    if "5 m" in cn or "5m" in cn:
        return "5 m"
    if "7 m" in cn or "7m" in cn:
        return "7 m"
    cd = (comp_distance or "").lower()
    if "5 m" in cd or "5m" in cd:
        return "5 m"
    return "7 m"


def _fit_text(c: canvas.Canvas, text: str, font_name: str, max_pt: float, min_pt: float, max_width_pt: float) -> float:
    """Finds a font size <= max_pt so text fits within max_width_pt."""
    sz = max_pt
    while sz > min_pt:
        if c.stringWidth(text, font_name, sz) <= max_width_pt:
            return sz
        sz -= 0.5
    return min_pt


def _draw_double_box(c: canvas.Canvas, x_mm: float, y_top_mm: float, w_mm: float, h_mm: float) -> None:
    """Draws an official double-bordered total box (top-down mm coordinates)."""
    PAGE_H = 297.0
    x = x_mm * mm
    y = (PAGE_H - y_top_mm - h_mm) * mm
    w = w_mm * mm
    h = h_mm * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(1.0)
    c.rect(x, y, w, h, stroke=1, fill=0)
    inset = 1.6
    c.setLineWidth(0.5)
    c.rect(x + inset, y + inset, w - 2 * inset, h - 2 * inset, stroke=1, fill=0)


def _draw_passe_box(
    c: canvas.Canvas,
    x_mm: float,
    y_top_mm: float,
    passe_number: int,
) -> None:
    """
    Draws one single Passe table (82.532 mm wide x 48.404 mm high) with its
    'Passe X' header above it, matching the exact millimeter proportions of
    evaluator.PASSE_COL_X_FRACS and evaluator.PASSE_ROW_Y_FRACS.
    """
    PAGE_H = 297.0
    box_w_mm = 82.532
    box_h_mm = 48.404

    col_widths_mm = [16.492, 11.0067, 11.0067, 11.0067, 11.0067, 11.0067, 11.0067]
    row_heights_mm = [7.197, 5.786, 5.786, 5.786, 5.786, 5.786, 5.786, 6.491]

    col_xs_mm = [x_mm]
    for cw in col_widths_mm:
        col_xs_mm.append(col_xs_mm[-1] + cw)

    row_ys_mm = [y_top_mm]
    for rh in row_heights_mm:
        row_ys_mm.append(row_ys_mm[-1] + rh)

    # 1. 'Passe X' label above box
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 10.5)
    c.drawString((x_mm + 1.2) * mm, (PAGE_H - (y_top_mm - 1.5)) * mm, f"Passe {passe_number}")

    # 2. Cell background fills
    # Header row cols 1..6: #D9D9D9
    c.setFillColor(colors.HexColor("#D9D9D9"))
    c.rect(
        col_xs_mm[1] * mm,
        (PAGE_H - row_ys_mm[1]) * mm,
        (col_xs_mm[7] - col_xs_mm[1]) * mm,
        row_heights_mm[0] * mm,
        stroke=0,
        fill=1,
    )

    ring_specs = [
        ("10", "#FFF2CC"),
        ("9", "#FFF2CC"),
        ("8", "#F8CECC"),
        ("7", "#F8CECC"),
        ("6", "#DAE8FC"),
        ("0 (M)", "#FFFFFF"),
    ]
    for idx, (label, hex_col) in enumerate(ring_specs):
        ry_top = row_ys_mm[idx + 1]
        ry_bot = row_ys_mm[idx + 2]
        c.setFillColor(colors.HexColor(hex_col))
        c.rect(
            col_xs_mm[0] * mm,
            (PAGE_H - ry_bot) * mm,
            col_widths_mm[0] * mm,
            (ry_bot - ry_top) * mm,
            stroke=0,
            fill=1,
        )

    # Summe row col 0: #F2F2F2
    c.setFillColor(colors.HexColor("#F2F2F2"))
    c.rect(
        col_xs_mm[0] * mm,
        (PAGE_H - row_ys_mm[8]) * mm,
        col_widths_mm[0] * mm,
        row_heights_mm[7] * mm,
        stroke=0,
        fill=1,
    )

    # 3. Text labels inside cells
    c.setFillColor(colors.black)
    # Diagonal cell "Ringe \ Pfeil"
    c.setFont(FONT_BOLD, 7.5)
    c.drawRightString((col_xs_mm[1] - 1.0) * mm, (PAGE_H - (row_ys_mm[0] + 2.8)) * mm, "Pfeil")
    c.drawString((col_xs_mm[0] + 1.0) * mm, (PAGE_H - (row_ys_mm[1] - 1.2)) * mm, "Ringe")

    # Diagonal line in top-left cell
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.line(
        col_xs_mm[0] * mm,
        (PAGE_H - row_ys_mm[0]) * mm,
        col_xs_mm[1] * mm,
        (PAGE_H - row_ys_mm[1]) * mm,
    )

    # Arrow numbers 1..6
    c.setFont(FONT_BOLD, 10.0)
    for arrow_idx in range(6):
        cx_mid = 0.5 * (col_xs_mm[arrow_idx + 1] + col_xs_mm[arrow_idx + 2])
        cy_base = row_ys_mm[0] + 4.8
        c.drawCentredString(cx_mid * mm, (PAGE_H - cy_base) * mm, str(arrow_idx + 1))

    # Ring labels 10, 9, 8, 7, 6, 0 (M)
    for idx, (label, _) in enumerate(ring_specs):
        cx_mid = 0.5 * (col_xs_mm[0] + col_xs_mm[1])
        cy_base = row_ys_mm[idx + 1] + 4.0
        c.drawCentredString(cx_mid * mm, (PAGE_H - cy_base) * mm, label)

    # Summe label
    c.setFont(FONT_BOLD, 9.5)
    cx_mid = 0.5 * (col_xs_mm[0] + col_xs_mm[1])
    cy_base = row_ys_mm[7] + 4.4
    c.drawCentredString(cx_mid * mm, (PAGE_H - cy_base) * mm, "Summe \u2211")

    # 4. Internal grid lines
    c.setStrokeColor(colors.black)
    for r_i in range(1, 8):
        ry = (PAGE_H - row_ys_mm[r_i]) * mm
        if r_i in (1, 7):
            # Double / emphasized separator line under header and above Summe
            c.setLineWidth(0.6)
            c.line(col_xs_mm[0] * mm, ry + 0.6, col_xs_mm[7] * mm, ry + 0.6)
            c.line(col_xs_mm[0] * mm, ry - 0.6, col_xs_mm[7] * mm, ry - 0.6)
        else:
            c.setLineWidth(0.5)
            c.line(col_xs_mm[0] * mm, ry, col_xs_mm[7] * mm, ry)

    # Vertical line after Col 0 (goes all the way to bottom of Summe row)
    c.setLineWidth(0.6)
    c.line(
        col_xs_mm[1] * mm,
        (PAGE_H - row_ys_mm[0]) * mm,
        col_xs_mm[1] * mm,
        (PAGE_H - row_ys_mm[8]) * mm,
    )

    # Vertical lines between arrow columns 1..6 (stop at top of Summe row)
    c.setLineWidth(0.5)
    for c_i in range(2, 7):
        c.line(
            col_xs_mm[c_i] * mm,
            (PAGE_H - row_ys_mm[0]) * mm,
            col_xs_mm[c_i] * mm,
            (PAGE_H - row_ys_mm[7]) * mm,
        )

    # 5. Outer thick border of the Passe box
    c.setLineWidth(1.5)
    c.rect(
        x_mm * mm,
        (PAGE_H - (y_top_mm + box_h_mm)) * mm,
        box_w_mm * mm,
        box_h_mm * mm,
        stroke=1,
        fill=0,
    )


def _draw_qr_code_vector(
    c: canvas.Canvas,
    payload_str: str,
    x_mm: float,
    y_top_mm: float,
    size_mm: float,
) -> None:
    """
    Draws a razor-sharp vector QR code onto the ReportLab canvas inside
    the square (x_mm, y_top_mm, size_mm, size_mm) including a quiet zone.
    """
    PAGE_H = 297.0
    modules = qr_codec.make_scorecard_qr_matrix(payload_str)
    n_mod = len(modules)
    quiet = 2
    total_units = n_mod + 2 * quiet
    unit_mm = size_mm / float(total_units)

    # White background box for quiet zone
    c.setFillColor(colors.white)
    c.rect(x_mm * mm, (PAGE_H - y_top_mm - size_mm) * mm, size_mm * mm, size_mm * mm, stroke=0, fill=1)

    c.setFillColor(colors.black)
    # Slight overlap (0.02 mm) prevents hairline gaps in PDF viewers when printing
    eps = 0.02 * mm
    u_pt = unit_mm * mm
    for r in range(n_mod):
        row_mods = modules[r]
        my_top = y_top_mm + (quiet + r) * unit_mm
        y_pt = (PAGE_H - my_top - unit_mm) * mm
        for col in range(n_mod):
            if row_mods[col]:
                mx_left = x_mm + (quiet + col) * unit_mm
                x_pt = mx_left * mm
                c.rect(x_pt, y_pt, u_pt + eps, u_pt + eps, stroke=0, fill=1)


def draw_scorecard_page(
    c: canvas.Canvas,
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
    is_blank_template: bool = False,
) -> None:
    """
    Draws one full A4 scorecard page (half=1 -> Zettel A, half=2 -> Zettel B) onto the ReportLab canvas.
    Header fields:
      Left column:  Wettbewerb, Name, Vorname, Verein, Gruppe
      Right column: Datum, Klasse, Entfernung (7 m / 5 m), Standnummer
      Top-Right (when not blank): QR-Code encoding all header fields + half
    The 5 Passe boxes are positioned at the exact FULL_PAGE_GRID_AREA coordinates
    so camera/photo evaluation works with 100% accuracy.
    """
    _ensure_fonts()
    PAGE_W = 210.0
    PAGE_H = 297.0

    margin_l = 17.5
    margin_r = PAGE_W - 17.5  # 192.5 mm

    formatted_date = format_date_de(date_str)

    # -------------------------------------------------------------------------
    # 1. Title, A/B Box & QR Code (on pre-filled scorecards)
    # -------------------------------------------------------------------------
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 16.0)
    c.drawString(margin_l * mm, (PAGE_H - 16.2) * mm, "Blasrohr - Ergebniszettel")

    ab_letter = "A" if int(half) == 1 else "B"
    ab_w = 11.0
    ab_h = 8.8
    ab_y_top = 10.0

    if is_blank_template:
        ab_x = margin_r - ab_w
        left_line_x0 = 45.5
        left_line_x1 = 99.5
        right_label_x = 105.5
        right_line_x0 = 130.5
        right_line_x1 = margin_r
    else:
        # Leave a dedicated 28 x 28 mm zone on the right of the header for the QR code
        qr_size_mm = 28.5
        qr_x_mm = margin_r - qr_size_mm + 1.0  # 165.0 mm .. 193.5 mm
        qr_y_top_mm = 15.2

        ab_x = 151.0  # Aligned with the right end of the header lines (162.0 mm)
        left_line_x0 = 45.0
        left_line_x1 = 96.5
        right_label_x = 100.5
        right_line_x0 = 124.5
        right_line_x1 = 162.0

        qr_payload = qr_codec.encode_scorecard_qr_payload(
            half=half,
            competition_name=competition_name,
            shooter_name=shooter_name,
            club=club,
            date_str=formatted_date,
            class_name=class_name,
            distance=distance,
            group_name=group_name,
            stand_number=stand_number,
            shooter_id=shooter_id,
            competition_id=competition_id,
        )
        _draw_qr_code_vector(c, qr_payload, qr_x_mm, qr_y_top_mm, qr_size_mm)

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.rect(ab_x * mm, (PAGE_H - ab_y_top - ab_h) * mm, ab_w * mm, ab_h * mm, stroke=1, fill=0)
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 15.0)
    c.drawCentredString((ab_x + ab_w * 0.5) * mm, (PAGE_H - ab_y_top - 6.3) * mm, ab_letter)

    # -------------------------------------------------------------------------
    # 2. Header Fields (4 rows x 2 columns)
    #    Row 1: Wettbewerb       | Datum
    #    Row 2: Name, Vorname    | Klasse
    #    Row 3: Verein           | Entfernung  7 m []   5 m []
    #    Row 4: Gruppe           | Standnummer
    # -------------------------------------------------------------------------
    row_ys = [24.0, 29.8, 35.6, 41.4]  # Baseline Y in mm from top
    left_label_x = margin_l           # 17.5 mm

    left_fields = [
        ("Wettbewerb", competition_name),
        ("Name, Vorname", shooter_name),
        ("Verein", club),
        ("Gruppe", group_name),
    ]

    for idx, (label, val) in enumerate(left_fields):
        y_base = row_ys[idx]
        y_line = y_base + 1.1
        c.setFillColor(colors.black)
        c.setFont(FONT_BOLD, 10.0)
        c.drawString(left_label_x * mm, (PAGE_H - y_base) * mm, label)

        c.setStrokeColor(colors.black)
        c.setLineWidth(0.6)
        c.line(left_line_x0 * mm, (PAGE_H - y_line) * mm, left_line_x1 * mm, (PAGE_H - y_line) * mm)

        if val and not is_blank_template:
            val_str = str(val).strip()
            max_w_pt = (left_line_x1 - left_line_x0 - 2.0) * mm
            fsz = _fit_text(c, val_str, FONT_REGULAR, 10.5, 7.5, max_w_pt)
            c.setFont(FONT_REGULAR, fsz)
            c.drawString((left_line_x0 + 1.0) * mm, (PAGE_H - (y_base - 0.1)) * mm, val_str)

    # Right Column Row 1 (Datum), Row 2 (Klasse), Row 4 (Standnummer)
    right_text_fields = [
        (0, "Datum", formatted_date),
        (1, "Klasse", class_name),
        (3, "Standnummer", stand_number),
    ]
    for r_idx, label, val in right_text_fields:
        y_base = row_ys[r_idx]
        y_line = y_base + 1.1
        c.setFillColor(colors.black)
        c.setFont(FONT_BOLD, 10.0)
        c.drawString(right_label_x * mm, (PAGE_H - y_base) * mm, label)

        c.setStrokeColor(colors.black)
        c.setLineWidth(0.6)
        c.line(right_line_x0 * mm, (PAGE_H - y_line) * mm, right_line_x1 * mm, (PAGE_H - y_line) * mm)

        if val and not is_blank_template:
            val_str = str(val).strip()
            max_w_pt = (right_line_x1 - right_line_x0 - 2.0) * mm
            fsz = _fit_text(c, val_str, FONT_REGULAR, 10.5, 7.5, max_w_pt)
            c.setFont(FONT_REGULAR, fsz)
            c.drawString((right_line_x0 + 1.0) * mm, (PAGE_H - (y_base - 0.1)) * mm, val_str)

    # Right Column Row 3: Entfernung   7 m [ ]     5 m [ ]
    y_entf = row_ys[2]
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 10.0)
    c.drawString(right_label_x * mm, (PAGE_H - y_entf) * mm, "Entfernung")

    c.setFont(FONT_BOLD, 10.5)
    c.drawString((right_line_x0 + 0.5) * mm, (PAGE_H - y_entf) * mm, "7 m")
    box7_x = right_line_x0 + 9.0
    box_y_bottom = PAGE_H - (y_entf + 0.3)
    box_sz = 3.6
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.rect(box7_x * mm, box_y_bottom * mm, box_sz * mm, box_sz * mm, stroke=1, fill=0)

    c.drawString((right_line_x0 + 20.5) * mm, (PAGE_H - y_entf) * mm, "5 m")
    box5_x = right_line_x0 + 29.0
    c.rect(box5_x * mm, box_y_bottom * mm, box_sz * mm, box_sz * mm, stroke=1, fill=0)

    if not is_blank_template and distance:
        dist_norm = distance.strip().lower()
        checked_box_x = None
        if "5" in dist_norm:
            checked_box_x = box5_x
        elif "7" in dist_norm:
            checked_box_x = box7_x
        if checked_box_x is not None:
            c.setLineWidth(1.2)
            pad = 0.55
            c.line(
                (checked_box_x + pad) * mm,
                (box_y_bottom + pad) * mm,
                (checked_box_x + box_sz - pad) * mm,
                (box_y_bottom + box_sz - pad) * mm,
            )
            c.line(
                (checked_box_x + box_sz - pad) * mm,
                (box_y_bottom + pad) * mm,
                (checked_box_x + pad) * mm,
                (box_y_bottom + box_sz - pad) * mm,
            )

    # -------------------------------------------------------------------------
    # 3. Instruction Lines
    # -------------------------------------------------------------------------
    c.setFillColor(colors.black)
    c.setFont(FONT_ITALIC, 9.5)
    c.drawCentredString((PAGE_W * 0.5) * mm, (PAGE_H - 48.2) * mm, "Bitte den Ergebniszettel leserlich ausfüllen.")
    c.setFont(FONT_BOLD, 9.5)
    c.drawCentredString(
        (PAGE_W * 0.5) * mm,
        (PAGE_H - 52.8) * mm,
        "Bei jedem Schuss einer Passe pro Spalte bitte genau ein Kreuz.",
    )

    # -------------------------------------------------------------------------
    # 4. Five Passe Boxes (exact coordinates matching FULL_PAGE_GRID_AREA)
    # -------------------------------------------------------------------------
    # FULL_PAGE_GRID_AREA = (0.0833, 0.2138, 0.9167, 0.7633)
    # y0 = 0.2138 * 297 = 63.50 mm
    grid_x_left = 17.50
    grid_x_right = 110.032
    row1_y_top = 63.50
    row2_y_top = 120.90  # 63.50 + 48.404 + 8.996
    row3_y_top = 178.30  # 120.90 + 48.404 + 8.996

    start_passe = 1 if int(half) == 1 else 6
    _draw_passe_box(c, grid_x_left, row1_y_top, start_passe + 0)
    _draw_passe_box(c, grid_x_right, row1_y_top, start_passe + 1)
    _draw_passe_box(c, grid_x_left, row2_y_top, start_passe + 2)
    _draw_passe_box(c, grid_x_right, row2_y_top, start_passe + 3)
    _draw_passe_box(c, grid_x_left, row3_y_top, start_passe + 4)

    # -------------------------------------------------------------------------
    # 5. Signature Block (bottom right next to Passe 5 / Passe 10)
    # -------------------------------------------------------------------------
    sig_x0 = grid_x_right
    sig_x1 = margin_r
    c.setFillColor(colors.black)
    c.setFont(FONT_REGULAR, 9.0)
    c.drawString(sig_x0 * mm, (PAGE_H - (row3_y_top + 3.8)) * mm, "Mit unserer Unterschrift bestätigen wir die")
    c.drawString(sig_x0 * mm, (PAGE_H - (row3_y_top + 8.2)) * mm, "Korrektheit der Angaben:")

    sig1_line_y = row3_y_top + 25.5
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.6)
    c.line(sig_x0 * mm, (PAGE_H - sig1_line_y) * mm, sig_x1 * mm, (PAGE_H - sig1_line_y) * mm)
    c.setFillColor(colors.HexColor("#404040"))
    c.setFont(FONT_REGULAR, 8.0)
    c.drawString(sig_x0 * mm, (PAGE_H - (sig1_line_y + 3.5)) * mm, "Unterschrift Schütze/Schützin")

    sig2_line_y = row3_y_top + 43.0
    c.line(sig_x0 * mm, (PAGE_H - sig2_line_y) * mm, sig_x1 * mm, (PAGE_H - sig2_line_y) * mm)
    c.drawString(sig_x0 * mm, (PAGE_H - (sig2_line_y + 3.5)) * mm, "Unterschrift Auswerter/in")

    # -------------------------------------------------------------------------
    # 6. Bottom Total Boxes
    # -------------------------------------------------------------------------
    tot_label_y = 238.5
    tot_box_y_top = 240.0
    tot_box_h = 7.0

    c.setFillColor(colors.black)
    if int(half) == 1:
        c.setFont(FONT_BOLD, 9.5)
        c.drawString((margin_l + 0.5) * mm, (PAGE_H - tot_label_y) * mm, "Gesamtergebnis Passe 1 - 5")
        _draw_double_box(c, margin_l, tot_box_y_top, 45.0, tot_box_h)
    else:
        c.setFont(FONT_BOLD, 9.0)
        b_w = 42.0
        gap_w = 8.0
        x1 = margin_l
        x2 = x1 + b_w + gap_w
        x3 = x2 + b_w + gap_w

        c.drawString((x1 + 1.0) * mm, (PAGE_H - tot_label_y) * mm, "Gesamtergebnis Passe 6 - 10")
        _draw_double_box(c, x1, tot_box_y_top, b_w, tot_box_h)

        c.setFont(FONT_BOLD, 13.0)
        c.drawCentredString((x1 + b_w + gap_w * 0.5) * mm, (PAGE_H - (tot_box_y_top + 5.0)) * mm, "+")

        c.setFont(FONT_BOLD, 9.0)
        c.drawString((x2 + 1.0) * mm, (PAGE_H - tot_label_y) * mm, "Gesamtergebnis Passe 1 - 5")
        _draw_double_box(c, x2, tot_box_y_top, b_w, tot_box_h)

        c.setFont(FONT_BOLD, 13.0)
        c.drawCentredString((x2 + b_w + gap_w * 0.5) * mm, (PAGE_H - (tot_box_y_top + 5.0)) * mm, "=")

        c.setFont(FONT_BOLD, 9.0)
        c.drawString((x3 + 1.0) * mm, (PAGE_H - tot_label_y) * mm, "Endergebnis")
        _draw_double_box(c, x3, tot_box_y_top, b_w, tot_box_h)

    c.showPage()


def generate_blank_template_pdf_bytes() -> bytes:
    """Generates the blank 2-page PDF template (Sheet A and Sheet B) with Gruppe and Standnummer."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Blasrohr - Ergebniszettel (A & B)")
    draw_scorecard_page(c, half=1, is_blank_template=True)
    draw_scorecard_page(c, half=2, is_blank_template=True)
    c.save()
    return buf.getvalue()


def generate_prefilled_scorecards_pdf_bytes(
    shooters: Sequence[Dict[str, Any]],
    competition: Dict[str, Any],
    halves: Sequence[int] = (1, 2),
    override_distance: Optional[str] = None,
) -> bytes:
    """
    Generates a multi-page PDF containing pre-filled scorecards for the given shooters.
    For each shooter, renders the requested halves (default: Page 1 = Zettel A, Page 2 = Zettel B).
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    comp_name = (competition.get("name") or "").strip()
    comp_date = (competition.get("date") or "").strip()
    comp_dist = (competition.get("distance") or "").strip()
    c.setTitle(f"Blasrohr - Ergebniszettel - {comp_name}")

    comp_id = competition.get("id")
    if not shooters:
        for h in halves:
            draw_scorecard_page(
                c,
                half=h,
                competition_name=comp_name,
                date_str=comp_date,
                distance=resolve_distance("", comp_dist, override_distance or ""),
                competition_id=comp_id,
                is_blank_template=False,
            )
        c.save()
        return buf.getvalue()

    for s in shooters:
        last_name = (s.get("last_name") or "").strip()
        first_name = (s.get("first_name") or "").strip()
        full_name = f"{last_name}, {first_name}" if (last_name and first_name) else (last_name or first_name)
        club = (s.get("club") or "").strip()
        class_name = (s.get("class_name") or "").strip()
        group_name = (s.get("group_name") or "").strip()
        stand_number = str(s.get("stand_number") or "").strip()
        dist = resolve_distance(class_name, comp_dist, override_distance or "")
        sid = s.get("id")

        for h in halves:
            draw_scorecard_page(
                c,
                half=h,
                competition_name=comp_name,
                shooter_name=full_name,
                club=club,
                date_str=comp_date,
                class_name=class_name,
                distance=dist,
                group_name=group_name,
                stand_number=stand_number,
                shooter_id=sid,
                competition_id=comp_id,
                is_blank_template=False,
            )

    c.save()
    return buf.getvalue()
