import base64
import csv
import io
import json
import os
import socket
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

import evaluator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bgscore.db")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit for high-res phone photos


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS competitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            location TEXT DEFAULT '',
            distance TEXT DEFAULT '7 m',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id INTEGER NOT NULL,
            class_id INTEGER NOT NULL,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            club TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS passe_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shooter_id INTEGER NOT NULL,
            half INTEGER NOT NULL,
            passe_number INTEGER NOT NULL,
            arrow_1 INTEGER NOT NULL DEFAULT 0,
            arrow_2 INTEGER NOT NULL DEFAULT 0,
            arrow_3 INTEGER NOT NULL DEFAULT 0,
            arrow_4 INTEGER NOT NULL DEFAULT 0,
            arrow_5 INTEGER NOT NULL DEFAULT 0,
            arrow_6 INTEGER NOT NULL DEFAULT 0,
            passe_sum INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(shooter_id, passe_number),
            FOREIGN KEY (shooter_id) REFERENCES shooters(id) ON DELETE CASCADE
        );
        """
    )

    # Seed initial competition with default classes if database is empty
    cur.execute("SELECT COUNT(*) AS cnt FROM competitions")
    if cur.fetchone()["cnt"] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today_str = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "INSERT INTO competitions (name, date, location, distance, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Blasrohr-Vereinsmeisterschaft", today_str, "Schützenhaus", "", now),
        )
        comp_id = cur.lastrowid
        default_classes = [
            "Schüler III (5 m)",
            "Schüler II",
            "Schüler I",
            "Jugend",
            "Damen",
            "Herren",
            "Senioren",
        ]
        for idx, cname in enumerate(default_classes):
            cur.execute(
                "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, ?)",
                (comp_id, cname, idx + 1),
            )

    conn.commit()
    conn.close()


def _build_shooter_results(competition_id: int, class_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Loads all shooters for a competition (optionally filtered by class_id) together with
    their Passe 1..10 scores, Half 1 sum, Half 2 sum, Total score, and 10s/9s count.
    """
    conn = get_db()
    cur = conn.cursor()

    query = """
        SELECT s.id, s.competition_id, s.class_id, s.last_name, s.first_name, s.club, s.created_at,
               c.name AS class_name, c.sort_order AS class_sort
        FROM shooters s
        JOIN classes c ON s.class_id = c.id
        WHERE s.competition_id = ?
    """
    params: List[Any] = [competition_id]
    if class_id is not None:
        query += " AND s.class_id = ?"
        params.append(class_id)

    query += " ORDER BY s.last_name COLLATE NOCASE ASC, s.first_name COLLATE NOCASE ASC"
    cur.execute(query, params)
    shooter_rows = cur.fetchall()

    if not shooter_rows:
        conn.close()
        return []

    shooter_ids = [r["id"] for r in shooter_rows]
    placeholders = ",".join("?" for _ in shooter_ids)
    cur.execute(
        f"""
        SELECT shooter_id, half, passe_number,
               arrow_1, arrow_2, arrow_3, arrow_4, arrow_5, arrow_6, passe_sum, updated_at
        FROM passe_scores
        WHERE shooter_id IN ({placeholders})
        ORDER BY passe_number ASC
        """,
        shooter_ids,
    )
    score_rows = cur.fetchall()
    conn.close()

    scores_by_shooter: Dict[int, Dict[int, Dict[str, Any]]] = {sid: {} for sid in shooter_ids}
    for sr in score_rows:
        arrows = [sr["arrow_1"], sr["arrow_2"], sr["arrow_3"], sr["arrow_4"], sr["arrow_5"], sr["arrow_6"]]
        scores_by_shooter[sr["shooter_id"]][sr["passe_number"]] = {
            "passe_number": sr["passe_number"],
            "half": sr["half"],
            "arrows": arrows,
            "sum": sr["passe_sum"],
            "updated_at": sr["updated_at"],
        }

    results = []
    for r in shooter_rows:
        sid = r["id"]
        p_map = scores_by_shooter.get(sid, {})

        passes_summary: Dict[int, Optional[int]] = {}
        passes_detail: Dict[int, Any] = {}
        tens_count = 0
        nines_count = 0

        for p_num in range(1, 11):
            if p_num in p_map:
                passes_summary[p_num] = p_map[p_num]["sum"]
                passes_detail[p_num] = p_map[p_num]
                for a in p_map[p_num]["arrows"]:
                    if a == 10:
                        tens_count += 1
                    elif a == 9:
                        nines_count += 1
            else:
                passes_summary[p_num] = None
                passes_detail[p_num] = None

        half1_recorded = any(passes_summary[p] is not None for p in range(1, 6))
        half2_recorded = any(passes_summary[p] is not None for p in range(6, 11))

        half1_sum = sum(passes_summary[p] or 0 for p in range(1, 6)) if half1_recorded else None
        half2_sum = sum(passes_summary[p] or 0 for p in range(6, 11)) if half2_recorded else None

        if half1_recorded or half2_recorded:
            total_sum = (half1_sum or 0) + (half2_sum or 0)
        else:
            total_sum = None

        results.append(
            {
                "id": sid,
                "competition_id": r["competition_id"],
                "class_id": r["class_id"],
                "class_name": r["class_name"],
                "last_name": r["last_name"],
                "first_name": r["first_name"],
                "club": r["club"] or "",
                "passes": passes_summary,
                "passes_detail": passes_detail,
                "half1_recorded": half1_recorded,
                "half2_recorded": half2_recorded,
                "half1_sum": half1_sum,
                "half2_sum": half2_sum,
                "total_sum": total_sum,
                "tens_count": tens_count,
                "nines_count": nines_count,
            }
        )

    # Sort by total_sum descending (shooters with scores first), then 10s, then 9s, then name
    results.sort(
        key=lambda item: (
            0 if item["total_sum"] is not None else 1,
            -(item["total_sum"] or 0),
            -item["tens_count"],
            -item["nines_count"],
            item["last_name"].lower(),
            item["first_name"].lower(),
        )
    )

    # Compute ranks per class and overall rank
    class_ranks: Dict[int, int] = {}
    for idx, item in enumerate(results):
        cid = item["class_id"]
        class_ranks[cid] = class_ranks.get(cid, 0) + 1
        item["rank_overall"] = idx + 1 if item["total_sum"] is not None else None
        item["rank_in_class"] = class_ranks[cid] if item["total_sum"] is not None else None

    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/network-info", methods=["GET"])
def api_network_info():
    """Returns local LAN IP and a QR code data URL so smartphones can easily connect."""
    lan_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        try:
            hostname = socket.gethostname()
            lan_ip = socket.gethostbyname(hostname)
        except Exception:
            lan_ip = "127.0.0.1"

    port = request.host.split(":")[-1] if ":" in request.host else "5000"
    url = f"http://{lan_ip}:5000"
    https_url = f"https://{lan_ip}:5443"
    has_ssl = os.path.exists(os.path.join(BASE_DIR, "cert.pem")) and os.path.exists(os.path.join(BASE_DIR, "key.pem"))
    preferred_mobile_url = https_url if has_ssl else url

    qr_b64 = None
    try:
        import qrcode

        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(preferred_mobile_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        qr_b64 = None

    return jsonify(
        {
            "lan_ip": lan_ip,
            "port": port,
            "url": url,
            "https_url": https_url if has_ssl else None,
            "preferred_url": preferred_mobile_url,
            "qr_code": qr_b64,
        }
    )


# -------------------------------------------------------------------------
# Competitions & Classes API
# -------------------------------------------------------------------------
@app.route("/api/competitions", methods=["GET"])
def list_competitions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM competitions ORDER BY date DESC, id DESC")
    comps = [dict(r) for r in cur.fetchall()]

    for comp in comps:
        cur.execute(
            "SELECT * FROM classes WHERE competition_id = ? ORDER BY sort_order ASC, name ASC",
            (comp["id"],),
        )
        comp["classes"] = [dict(c) for c in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS cnt FROM shooters WHERE competition_id = ?", (comp["id"],))
        comp["shooter_count"] = cur.fetchone()["cnt"]

    conn.close()
    return jsonify(comps)


@app.route("/api/competitions", methods=["POST"])
def create_competition():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Bitte einen Wettkampf-Namen eingeben."}), 400

    date_str = (data.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()
    location = (data.get("location") or "").strip()
    create_default_classes = bool(data.get("create_default_classes", True))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO competitions (name, date, location, distance, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, date_str, location, "", now),
    )
    comp_id = cur.lastrowid

    if create_default_classes:
        default_classes = data.get("classes") or [
            "Schüler III (5 m)",
            "Schüler II",
            "Schüler I",
            "Jugend",
            "Damen",
            "Herren",
            "Senioren",
        ]
        for idx, cname in enumerate(default_classes):
            if cname.strip():
                cur.execute(
                    "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, ?)",
                    (comp_id, cname.strip(), idx + 1),
                )

    conn.commit()
    conn.close()
    return jsonify({"id": comp_id, "message": "Wettkampf erfolgreich angelegt."}), 201


@app.route("/api/competitions/<int:comp_id>", methods=["PUT"])
def update_competition(comp_id: int):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Der Name darf nicht leer sein."}), 400

    date_str = (data.get("date") or "").strip()
    location = (data.get("location") or "").strip()

    conn = get_db()
    conn.execute(
        "UPDATE competitions SET name = ?, date = ?, location = ? WHERE id = ?",
        (name, date_str, location, comp_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "Wettkampf aktualisiert."})


@app.route("/api/competitions/<int:comp_id>", methods=["DELETE"])
def delete_competition(comp_id: int):
    conn = get_db()
    conn.execute("DELETE FROM competitions WHERE id = ?", (comp_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Wettkampf gelöscht."})


@app.route("/api/competitions/<int:comp_id>/classes", methods=["POST"])
def create_class(comp_id: int):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Bitte einen Klassennamen angeben."}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_ord FROM classes WHERE competition_id = ?", (comp_id,))
    next_ord = cur.fetchone()["next_ord"]
    cur.execute(
        "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, ?)",
        (comp_id, name, next_ord),
    )
    class_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": class_id, "name": name, "competition_id": comp_id}), 201


@app.route("/api/classes/<int:class_id>", methods=["PUT"])
def update_class(class_id: int):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Klassenname darf nicht leer sein."}), 400
    conn = get_db()
    conn.execute("UPDATE classes SET name = ? WHERE id = ?", (name, class_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "Klasse aktualisiert."})


@app.route("/api/classes/<int:class_id>", methods=["DELETE"])
def delete_class(class_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM shooters WHERE class_id = ?", (class_id,))
    if cur.fetchone()["cnt"] > 0:
        conn.close()
        return jsonify({"error": "Dieser Klasse sind noch Schützen zugeordnet. Bitte zuerst die Schützen umbuchen oder löschen."}), 400
    cur.execute("DELETE FROM classes WHERE id = ?", (class_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Klasse gelöscht."})


# -------------------------------------------------------------------------
# Shooters API
# -------------------------------------------------------------------------
@app.route("/api/competitions/<int:comp_id>/shooters", methods=["GET"])
def list_shooters(comp_id: int):
    class_id_param = request.args.get("class_id")
    class_id = int(class_id_param) if class_id_param and class_id_param.isdigit() else None
    results = _build_shooter_results(comp_id, class_id=class_id)
    return jsonify(results)


@app.route("/api/competitions/<int:comp_id>/shooters", methods=["POST"])
def create_shooter(comp_id: int):
    data = request.get_json(force=True)
    last_name = (data.get("last_name") or "").strip()
    first_name = (data.get("first_name") or "").strip()
    club = (data.get("club") or "").strip()
    class_id = data.get("class_id")

    if not last_name or not first_name:
        return jsonify({"error": "Bitte Name und Vorname des Schützen angeben."}), 400
    if not class_id:
        return jsonify({"error": "Bitte eine Klasse auswählen."}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO shooters (competition_id, class_id, last_name, first_name, club, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (comp_id, int(class_id), last_name, first_name, club, now),
    )
    shooter_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": shooter_id, "message": "Schütze erfolgreich angelegt."}), 201


@app.route("/api/shooters/<int:shooter_id>", methods=["PUT"])
def update_shooter(shooter_id: int):
    data = request.get_json(force=True)
    last_name = (data.get("last_name") or "").strip()
    first_name = (data.get("first_name") or "").strip()
    club = (data.get("club") or "").strip()
    class_id = data.get("class_id")

    if not last_name or not first_name or not class_id:
        return jsonify({"error": "Name, Vorname und Klasse sind Pflichtfelder."}), 400

    conn = get_db()
    conn.execute(
        "UPDATE shooters SET last_name = ?, first_name = ?, club = ?, class_id = ? WHERE id = ?",
        (last_name, first_name, club, int(class_id), shooter_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "Schütze aktualisiert."})


@app.route("/api/shooters/<int:shooter_id>", methods=["DELETE"])
def delete_shooter(shooter_id: int):
    conn = get_db()
    conn.execute("DELETE FROM shooters WHERE id = ?", (shooter_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Schütze gelöscht."})


# -------------------------------------------------------------------------
# Scorecard Camera Evaluation & Score Saving API
# -------------------------------------------------------------------------
@app.route("/api/evaluate-photo", methods=["POST"])
def api_evaluate_photo():
    """
    Receives a photo of a Blasrohr-Ergebniszettel (either multipart/form-data file upload
    or JSON base64 image) and the selected half (1 or 2), evaluates all 5 Passen (6 arrows each),
    and returns the detected ring values, sums, confidence scores, and annotated preview images.
    """
    half = 1
    crop_region = None
    image_bytes = None

    if request.files and "photo" in request.files:
        file_obj = request.files["photo"]
        image_bytes = file_obj.read()
        half = int(request.form.get("half", 1))
        if request.form.get("crop_region"):
            try:
                crop_region = json.loads(request.form["crop_region"])
            except Exception:
                crop_region = None
    else:
        data = request.get_json(force=True, silent=True) or {}
        half = int(data.get("half", 1))
        crop_region = data.get("crop_region")
        b64_str = data.get("image_base64") or ""
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        if b64_str:
            image_bytes = base64.b64decode(b64_str)

    if not image_bytes:
        return jsonify({"error": "Kein Bild empfangen."}), 400

    try:
        result = evaluator.evaluate_scorecard_image(image_bytes, half=half, crop_region=crop_region)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": f"Fehler bei der Bildauswertung: {str(exc)}"}), 500


@app.route("/api/shooters/<int:shooter_id>/scores", methods=["POST"])
def save_shooter_half_scores(shooter_id: int):
    """
    Saves the evaluated (and optionally user-verified) scores for a shooter's half (1 or 2).
    Expects JSON:
      {
        "half": 1 or 2,
        "passes": [
          {"passe_number": 1, "arrows": [10, 9, 10, 8, 9, 10]},
          ...
        ]
      }
    """
    data = request.get_json(force=True)
    half = int(data.get("half", 1))
    if half not in (1, 2):
        return jsonify({"error": "Hälfte muss 1 oder 2 sein."}), 400

    passes = data.get("passes") or []
    if len(passes) != 5:
        return jsonify({"error": "Es müssen genau 5 Passen übergeben werden."}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()

    start_passe = 1 if half == 1 else 6
    half_total = 0

    for idx, p_item in enumerate(passes):
        p_num = int(p_item.get("passe_number", start_passe + idx))
        arrows = p_item.get("arrows") or [0, 0, 0, 0, 0, 0]
        if len(arrows) != 6:
            conn.close()
            return jsonify({"error": f"Passe {p_num} muss genau 6 Pfeilwerte enthalten."}), 400

        clean_arrows = [int(a) if int(a) in (10, 9, 8, 7, 6, 0) else 0 for a in arrows]
        p_sum = sum(clean_arrows)
        half_total += p_sum

        cur.execute(
            """
            INSERT INTO passe_scores (
                shooter_id, half, passe_number,
                arrow_1, arrow_2, arrow_3, arrow_4, arrow_5, arrow_6,
                passe_sum, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shooter_id, passe_number) DO UPDATE SET
                half = excluded.half,
                arrow_1 = excluded.arrow_1,
                arrow_2 = excluded.arrow_2,
                arrow_3 = excluded.arrow_3,
                arrow_4 = excluded.arrow_4,
                arrow_5 = excluded.arrow_5,
                arrow_6 = excluded.arrow_6,
                passe_sum = excluded.passe_sum,
                updated_at = excluded.updated_at
            """,
            (
                shooter_id,
                half,
                p_num,
                clean_arrows[0],
                clean_arrows[1],
                clean_arrows[2],
                clean_arrows[3],
                clean_arrows[4],
                clean_arrows[5],
                p_sum,
                now,
            ),
        )

    conn.commit()
    conn.close()
    return jsonify(
        {
            "message": f"{half}. Hälfte erfolgreich gespeichert ({half_total} Ringe).",
            "shooter_id": shooter_id,
            "half": half,
            "half_total": half_total,
        }
    )


@app.route("/api/shooters/<int:shooter_id>/scores/<int:half>", methods=["DELETE"])
def delete_shooter_half_scores(shooter_id: int, half: int):
    conn = get_db()
    conn.execute("DELETE FROM passe_scores WHERE shooter_id = ? AND half = ?", (shooter_id, half))
    conn.commit()
    conn.close()
    return jsonify({"message": f"Ergebnisse der {half}. Hälfte wurden zurückgesetzt."})


# -------------------------------------------------------------------------
# Export API (CSV for Excel & native XLSX)
# -------------------------------------------------------------------------
@app.route("/api/competitions/<int:comp_id>/export.csv", methods=["GET"])
def export_csv(comp_id: int):
    class_id_param = request.args.get("class_id")
    class_id = int(class_id_param) if class_id_param and class_id_param.isdigit() else None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, date FROM competitions WHERE id = ?", (comp_id,))
    comp_row = cur.fetchone()
    conn.close()

    comp_name = comp_row["name"] if comp_row else "Wettkampf"
    results = _build_shooter_results(comp_id, class_id=class_id)

    output = io.StringIO()
    # Write UTF-8 BOM so Microsoft Excel on Windows recognizes UTF-8 encoding automatically
    output.write("\ufeff")
    # Semicolon separator is standard for German Excel CSV files
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    headers = [
        "Platz (Klasse)",
        "Platz (Gesamt)",
        "Name",
        "Vorname",
        "Verein",
        "Klasse",
        "Passe 1",
        "Passe 2",
        "Passe 3",
        "Passe 4",
        "Passe 5",
        "1. Hälfte",
        "Passe 6",
        "Passe 7",
        "Passe 8",
        "Passe 9",
        "Passe 10",
        "2. Hälfte",
        "Gesamtergebnis",
        "10er",
        "9er",
    ]
    writer.writerow(headers)

    for r in results:
        p = r["passes"]
        row = [
            r["rank_in_class"] if r["rank_in_class"] is not None else "-",
            r["rank_overall"] if r["rank_overall"] is not None else "-",
            r["last_name"],
            r["first_name"],
            r["club"],
            r["class_name"],
            p[1] if p[1] is not None else "",
            p[2] if p[2] is not None else "",
            p[3] if p[3] is not None else "",
            p[4] if p[4] is not None else "",
            p[5] if p[5] is not None else "",
            r["half1_sum"] if r["half1_sum"] is not None else "",
            p[6] if p[6] is not None else "",
            p[7] if p[7] is not None else "",
            p[8] if p[8] is not None else "",
            p[9] if p[9] is not None else "",
            p[10] if p[10] is not None else "",
            r["half2_sum"] if r["half2_sum"] is not None else "",
            r["total_sum"] if r["total_sum"] is not None else "",
            r["tens_count"] if r["total_sum"] is not None else "",
            r["nines_count"] if r["total_sum"] is not None else "",
        ]
        writer.writerow(row)

    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in comp_name)
    filename = f"bgScore_{safe_name}_Ergebnisse.csv"

    return Response(
        output.getvalue().encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/competitions/<int:comp_id>/export.xlsx", methods=["GET"])
def export_xlsx(comp_id: int):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    class_id_param = request.args.get("class_id")
    class_id = int(class_id_param) if class_id_param and class_id_param.isdigit() else None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, date, location, distance FROM competitions WHERE id = ?", (comp_id,))
    comp_row = cur.fetchone()
    conn.close()

    comp_name = comp_row["name"] if comp_row else "Wettkampf"
    comp_date = comp_row["date"] if comp_row else ""
    results = _build_shooter_results(comp_id, class_id=class_id)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ergebnisliste"

    # Title row
    ws.merge_cells("A1:U1")
    ws["A1"] = f"bgScore Ergebnisliste – {comp_name} ({comp_date})"
    ws["A1"].font = Font(size=14, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1E3A8A")
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    headers = [
        "Platz (Kl.)",
        "Platz (Ges.)",
        "Name",
        "Vorname",
        "Verein",
        "Klasse",
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "1. Hälfte",
        "P6",
        "P7",
        "P8",
        "P9",
        "P10",
        "2. Hälfte",
        "Gesamt",
        "10er",
        "9er",
    ]
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="E2E8F0")
    bold_font = Font(bold=True)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = bold_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center" if col_idx not in (3, 4, 5, 6) else "left")

    sum_fill = PatternFill("solid", fgColor="FEF3C7")
    total_fill = PatternFill("solid", fgColor="DCFCE7")

    for r_idx, r in enumerate(results, start=3):
        p = r["passes"]
        row_vals = [
            r["rank_in_class"] if r["rank_in_class"] is not None else "-",
            r["rank_overall"] if r["rank_overall"] is not None else "-",
            r["last_name"],
            r["first_name"],
            r["club"],
            r["class_name"],
            p[1],
            p[2],
            p[3],
            p[4],
            p[5],
            r["half1_sum"],
            p[6],
            p[7],
            p[8],
            p[9],
            p[10],
            r["half2_sum"],
            r["total_sum"],
            r["tens_count"] if r["total_sum"] is not None else None,
            r["nines_count"] if r["total_sum"] is not None else None,
        ]
        ws.append(row_vals)
        for c_idx in range(1, len(headers) + 1):
            c = ws.cell(row=r_idx, column=c_idx)
            c.border = thin_border
            if c_idx in (12, 18):
                c.fill = sum_fill
                c.font = bold_font
            elif c_idx == 19:
                c.fill = total_fill
                c.font = bold_font

    col_widths = [11, 11, 18, 16, 20, 16, 6, 6, 6, 6, 6, 11, 6, 6, 6, 6, 6, 11, 12, 7, 7]
    for i, w in enumerate(col_widths, start=1):
        col_letter = openpyxl.utils.get_column_letter(i)
        ws.column_dimensions[col_letter].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in comp_name)
    filename = f"bgScore_{safe_name}_Ergebnisse.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# -------------------------------------------------------------------------
# Blank Template Download & Synthetic Test Scorecard Generator
# -------------------------------------------------------------------------
@app.route("/api/template-pdf", methods=["GET"])
def download_template_pdf():
    vorlage_dir = os.path.join(BASE_DIR, "Vorlage")
    return send_from_directory(
        vorlage_dir,
        "Wertungszettel_Blasrohrschiessen_1_Haelfte.pdf",
        as_attachment=True,
        download_name="Blasrohr_Ergebniszettel_A_und_B.pdf",
    )


@app.route("/api/demo-scorecard", methods=["GET"])
def api_demo_scorecard():
    """Generates a realistic filled-out Ergebniszettel (A or B) for testing."""
    half = int(request.args.get("half", 1))
    shooter_name = request.args.get("name", "Mustermann, Max")
    club = request.args.get("club", "SV Pfeilflug")
    class_name = request.args.get("class_name", "Herren")
    comp_name = request.args.get("competition", "Vereinsmeisterschaft")

    img_bytes, passes = evaluator.generate_test_scorecard_image(
        half=half,
        shooter_name=shooter_name,
        club=club,
        class_name=class_name,
        competition_name=comp_name,
    )

    if request.args.get("format") == "json":
        b64 = "data:image/jpeg;base64," + base64.b64encode(img_bytes).decode("ascii")
        return jsonify({"image_base64": b64, "half": half, "expected_passes": passes})

    return Response(img_bytes, mimetype="image/jpeg")


if __name__ == "__main__":
    init_db()
    cert_file = os.path.join(BASE_DIR, "cert.pem")
    key_file = os.path.join(BASE_DIR, "key.pem")
    if os.path.exists(cert_file) and os.path.exists(key_file):
        import threading
        from werkzeug.serving import make_server

        def _run_https():
            try:
                srv = make_server("0.0.0.0", 5443, app, ssl_context=(cert_file, key_file), threaded=True)
                print(" * HTTPS Live-Kamera Server aktiv auf https://0.0.0.0:5443")
                srv.serve_forever()
            except Exception as e:
                print("HTTPS Server Hinweis:", e)

        t = threading.Thread(target=_run_https, daemon=True)
        t.start()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
