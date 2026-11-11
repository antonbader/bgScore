import base64
import csv
import io
import json
import os
import re
import socket
import sqlite3
import sys
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Fallback für Streams bei PyInstaller --noconsole / Windowed Modus
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

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
import scorecard_pdf

# Ermittlung der Verzeichnisse:
# - BUNDLE_DIR: Liefert im PyInstaller-Bundle (_MEIPASS) die statischen Assets (Templates, Static, Vorlagen).
# - APP_DIR / BASE_DIR: Verzeichnis der ausführbaren .exe (oder Skriptpfad), in dem bgscore.db und eigene Zertifikate dauerhaft liegen.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    APP_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

BASE_DIR = APP_DIR
DB_PATH = os.path.join(APP_DIR, "bgscore.db")

app = Flask(
    __name__,
    static_folder=os.path.join(BUNDLE_DIR, "static"),
    template_folder=os.path.join(BUNDLE_DIR, "templates"),
)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit for high-res phone photos
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def get_ssl_cert_paths() -> Tuple[Optional[str], Optional[str]]:
    """Prüft, ob SSL-Zertifikate vorhanden sind (im Anwendungsverzeichnis oder im Bundle)."""
    c_app = os.path.join(APP_DIR, "cert.pem")
    k_app = os.path.join(APP_DIR, "key.pem")
    if os.path.exists(c_app) and os.path.exists(k_app):
        return c_app, k_app
    c_bundle = os.path.join(BUNDLE_DIR, "cert.pem")
    k_bundle = os.path.join(BUNDLE_DIR, "key.pem")
    if os.path.exists(c_bundle) and os.path.exists(k_bundle):
        return c_bundle, k_bundle
    return None, None


@app.context_processor
def inject_asset_version():
    """Cache-busting version for static JS/CSS (changes whenever the files are modified)."""
    try:
        mtimes = [
            os.path.getmtime(os.path.join(BUNDLE_DIR, "static", "js", "app.js")),
            os.path.getmtime(os.path.join(BUNDLE_DIR, "static", "css", "style.css")),
        ]
        version = str(int(max(mtimes)))
    except OSError:
        version = "1"
    return {"asset_version": version}


@app.after_request
def add_cache_control_headers(response):
    """Ensure API endpoints and HTML/static assets are not cached stale by browsers."""
    if request.path == "/" or request.path.startswith("/api/") or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
            group_name TEXT DEFAULT '',
            stand_number TEXT DEFAULT '',
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

    # Migrate existing shooters table if group_name or stand_number columns do not exist yet
    cur.execute("PRAGMA table_info(shooters)")
    existing_cols = {row["name"] for row in cur.fetchall()}
    if "group_name" not in existing_cols:
        cur.execute("ALTER TABLE shooters ADD COLUMN group_name TEXT DEFAULT ''")
    if "stand_number" not in existing_cols:
        cur.execute("ALTER TABLE shooters ADD COLUMN stand_number TEXT DEFAULT ''")

    # Seed initial competition with default classes if database is empty
    cur.execute("SELECT COUNT(*) AS cnt FROM competitions")
    if cur.fetchone()["cnt"] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today_str = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "INSERT INTO competitions (name, date, location, distance, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Blasrohr-Vereinsmeisterschaft", today_str, "Schützenhaus", "7 m", now),
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


# Ensure DB schema and migrations are applied on startup/import
init_db()


def _natural_sort_key(val: str) -> Tuple:
    """Helper for natural sorting of stand numbers / group names (e.g. '2' before '10')."""
    s = (val or "").strip()
    if not s:
        return (1, 999999, "")
    parts = re.split(r"(\d+)", s.lower())
    converted = [int(p) if p.isdigit() else p for p in parts]
    first_num = next((int(p) for p in parts if p.isdigit()), 999999)
    return (0, first_num, converted)


def _build_shooter_results(competition_id: int, class_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Loads all shooters for a competition (optionally filtered by class_id) together with
    their Passe 1..10 scores, Half 1 sum, Half 2 sum, Total score, and 10s/9s count.
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT distance FROM competitions WHERE id = ?", (competition_id,))
    comp_row = cur.fetchone()
    comp_distance = (comp_row["distance"] if comp_row else "") or ""

    query = """
        SELECT s.id, s.competition_id, s.class_id, s.last_name, s.first_name, s.club,
               s.group_name, s.stand_number, s.created_at,
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

        class_name_str = r["class_name"] or ""
        results.append(
            {
                "id": sid,
                "competition_id": r["competition_id"],
                "class_id": r["class_id"],
                "class_name": class_name_str,
                "class_sort": r["class_sort"] or 0,
                "last_name": r["last_name"],
                "first_name": r["first_name"],
                "club": r["club"] or "",
                "group_name": r["group_name"] or "",
                "stand_number": r["stand_number"] or "",
                "distance": scorecard_pdf.resolve_distance(class_name_str, comp_distance),
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
    cert_f, key_f = get_ssl_cert_paths()
    has_ssl = cert_f is not None and key_f is not None
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


def _normalize_date_str(date_val: Any) -> str:
    s = str(date_val or "").strip()
    if not s:
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


@app.route("/api/competitions/import", methods=["POST"])
def import_competition():
    """
    Imports a competition from JSON containing:
      - turniername: str
      - datum: str (DD.MM.YYYY or YYYY-MM-DD)
      - klassen: list of class names (str)
      - schuetzen: list of shooters with name, vorname, klasse, verein, gruppe, scheibe
    Accepts application/json or multipart/form-data (file upload).
    """
    raw_data = None
    if request.files:
        file_obj = next(iter(request.files.values()), None)
        if file_obj:
            content = file_obj.read()
            if content.startswith(b"\xef\xbb\xbf"):
                content = content[3:]
            try:
                raw_data = json.loads(content.decode("utf-8"))
            except Exception as e:
                return jsonify({"error": f"Ungültiges JSON in Datei: {str(e)}"}), 400
    elif request.is_json:
        raw_data = request.get_json(silent=True)
    elif request.data:
        try:
            content = request.data
            if content.startswith(b"\xef\xbb\xbf"):
                content = content[3:]
            raw_data = json.loads(content.decode("utf-8"))
        except Exception as e:
            return jsonify({"error": f"Ungültiges JSON: {str(e)}"}), 400

    if not isinstance(raw_data, dict):
        return jsonify({"error": "Ungültiges Datenformat: JSON-Objekt erwartet."}), 400

    turniername = (
        raw_data.get("turniername")
        or raw_data.get("name")
        or raw_data.get("wettkampf")
        or raw_data.get("competition")
        or ""
    )
    turniername = str(turniername).strip()
    if not turniername:
        return jsonify({"error": "Kein 'turniername' in der JSON-Datei angegeben."}), 400

    datum_raw = raw_data.get("datum") if "datum" in raw_data else raw_data.get("date")
    date_normalized = _normalize_date_str(datum_raw)

    klassen_raw = raw_data.get("klassen") if "klassen" in raw_data else raw_data.get("classes", [])
    if not isinstance(klassen_raw, list):
        klassen_raw = []

    klassen_list: List[str] = []
    seen_classes = set()
    for k in klassen_raw:
        cname = (k.get("name") if isinstance(k, dict) else str(k)).strip()
        if cname and cname.lower() not in seen_classes:
            klassen_list.append(cname)
            seen_classes.add(cname.lower())

    schuetzen_raw = raw_data.get("schuetzen") if "schuetzen" in raw_data else raw_data.get("shooters", [])
    if not isinstance(schuetzen_raw, list):
        return jsonify({"error": "'schuetzen' muss eine Liste von Schützen-Einträgen sein."}), 400

    # Auto-add classes from shooters if not already present in 'klassen'
    for s in schuetzen_raw:
        if isinstance(s, dict):
            cname = str(s.get("klasse") or s.get("class") or s.get("class_name") or "").strip()
            if cname and cname.lower() not in seen_classes:
                klassen_list.append(cname)
                seen_classes.add(cname.lower())

    if not klassen_list:
        klassen_list = ["Allgemeine Klasse"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO competitions (name, date, location, distance, created_at) VALUES (?, ?, ?, ?, ?)",
            (turniername, date_normalized, "", "7 m", now),
        )
        comp_id = cur.lastrowid

        class_map: Dict[str, int] = {}
        for idx, cname in enumerate(klassen_list):
            cur.execute(
                "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, ?)",
                (comp_id, cname, idx + 1),
            )
            class_id = cur.lastrowid
            class_map[cname.lower()] = class_id

        created_shooters = 0
        for s in schuetzen_raw:
            if not isinstance(s, dict):
                continue

            last_name = str(s.get("name") or s.get("last_name") or s.get("nachname") or "").strip()
            first_name = str(s.get("vorname") or s.get("first_name") or "").strip()

            if not last_name and not first_name:
                continue

            cname = str(s.get("klasse") or s.get("class") or s.get("class_name") or "").strip()
            class_id = class_map.get(cname.lower())
            if not class_id:
                class_id = list(class_map.values())[0]

            club = str(s.get("verein") or s.get("club") or "").strip()

            grp_val = s.get("gruppe") if "gruppe" in s else s.get("group", s.get("group_name"))
            group_name = str(grp_val).strip() if grp_val is not None else ""

            sch_val = s.get("scheibe") if "scheibe" in s else s.get("stand", s.get("stand_number"))
            stand_number = str(sch_val).strip() if sch_val is not None else ""

            cur.execute(
                """
                INSERT INTO shooters (competition_id, class_id, last_name, first_name, club, group_name, stand_number, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (comp_id, class_id, last_name, first_name, club, group_name, stand_number, now),
            )
            created_shooters += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Fehler beim Speichern in der Datenbank: {str(e)}"}), 500

    conn.close()

    return jsonify({
        "id": comp_id,
        "name": turniername,
        "date": date_normalized,
        "classes_count": len(class_map),
        "shooters_count": created_shooters,
        "message": f"Turnier '{turniername}' erfolgreich importiert ({len(class_map)} Klassen, {created_shooters} Schützen)."
    }), 201


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
    group_name = (data.get("group_name") if "group_name" in data else data.get("group") or "").strip()
    stand_number = str(data.get("stand_number") if "stand_number" in data else data.get("stand") or "").strip()
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
        INSERT INTO shooters (competition_id, class_id, last_name, first_name, club, group_name, stand_number, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (comp_id, int(class_id), last_name, first_name, club, group_name, stand_number, now),
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
    group_name = (data.get("group_name") if "group_name" in data else data.get("group") or "").strip()
    stand_number = str(data.get("stand_number") if "stand_number" in data else data.get("stand") or "").strip()
    class_id = data.get("class_id")

    if not last_name or not first_name or not class_id:
        return jsonify({"error": "Name, Vorname und Klasse sind Pflichtfelder."}), 400

    conn = get_db()
    conn.execute(
        """
        UPDATE shooters
        SET last_name = ?, first_name = ?, club = ?, group_name = ?, stand_number = ?, class_id = ?
        WHERE id = ?
        """,
        (last_name, first_name, club, group_name, stand_number, int(class_id), shooter_id),
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
def _resolve_shooter_from_qr(
    qr_data: Dict[str, Any],
    competition_id: Optional[int] = None,
    auto_create: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Matches a decoded QR code payload against shooters in the active competition.
    1. Matches by shooter_id (if it belongs to the target competition).
    2. Matches by (last_name, first_name) case-insensitively in the target competition.
    3. If not found and auto_create=True, automatically creates the shooter (and class if needed)
       from the QR header fields so any printed scorecard with QR code can be evaluated immediately.
    """
    if not qr_data or not isinstance(qr_data, dict):
        return None

    conn = get_db()
    cur = conn.cursor()

    try:
        comp_id = competition_id
        if not comp_id and qr_data.get("competition_id"):
            try:
                cand_cid = int(qr_data["competition_id"])
                cur.execute("SELECT id FROM competitions WHERE id = ?", (cand_cid,))
                if cur.fetchone():
                    comp_id = cand_cid
            except Exception:
                comp_id = None

        if not comp_id:
            cur.execute("SELECT id FROM competitions ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                comp_id = int(row["id"])

        if not comp_id:
            return None

        shooter_row = None
        auto_created = False

        # 1. Try exact shooter_id match in this competition
        qr_sid = qr_data.get("shooter_id")
        if qr_sid is not None:
            try:
                cur.execute(
                    """
                    SELECT s.*, c.name AS class_name
                    FROM shooters s
                    LEFT JOIN classes c ON c.id = s.class_id
                    WHERE s.id = ? AND s.competition_id = ?
                    """,
                    (int(qr_sid), comp_id),
                )
                shooter_row = cur.fetchone()
            except Exception:
                shooter_row = None

        # Extract name parts from QR data
        last_name = str(qr_data.get("last_name") or "").strip()
        first_name = str(qr_data.get("first_name") or "").strip()
        full_name = str(qr_data.get("name") or "").strip()
        if (not last_name and not first_name) and full_name:
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

        # 2. Match by last_name + first_name (case-insensitive) within competition
        if shooter_row is None and (last_name or first_name):
            cur.execute(
                """
                SELECT s.*, c.name AS class_name
                FROM shooters s
                LEFT JOIN classes c ON c.id = s.class_id
                WHERE s.competition_id = ?
                  AND LOWER(TRIM(s.last_name)) = LOWER(?)
                  AND LOWER(TRIM(s.first_name)) = LOWER(?)
                LIMIT 1
                """,
                (comp_id, last_name, first_name),
            )
            shooter_row = cur.fetchone()

        # 3. Auto-create shooter in active competition if not yet registered
        if shooter_row is None and auto_create and (last_name or first_name):
            qr_class_name = str(qr_data.get("klasse") or "").strip()
            class_id = None
            resolved_class_name = qr_class_name

            if qr_class_name:
                cur.execute(
                    """
                    SELECT id, name FROM classes
                    WHERE competition_id = ? AND LOWER(TRIM(name)) = LOWER(?)
                    LIMIT 1
                    """,
                    (comp_id, qr_class_name),
                )
                c_row = cur.fetchone()
                if c_row:
                    class_id = int(c_row["id"])
                    resolved_class_name = c_row["name"]
                else:
                    cur.execute(
                        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_sort FROM classes WHERE competition_id = ?",
                        (comp_id,),
                    )
                    next_sort = int(cur.fetchone()["next_sort"])
                    cur.execute(
                        "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, ?)",
                        (comp_id, qr_class_name, next_sort),
                    )
                    class_id = cur.lastrowid

            if class_id is None:
                cur.execute(
                    "SELECT id, name FROM classes WHERE competition_id = ? ORDER BY sort_order ASC, id ASC LIMIT 1",
                    (comp_id,),
                )
                c_row = cur.fetchone()
                if c_row:
                    class_id = int(c_row["id"])
                    resolved_class_name = c_row["name"]
                else:
                    cur.execute(
                        "INSERT INTO classes (competition_id, name, sort_order) VALUES (?, ?, 1)",
                        (comp_id, "Offene Klasse"),
                    )
                    class_id = cur.lastrowid
                    resolved_class_name = "Offene Klasse"

            club = str(qr_data.get("verein") or "").strip()
            group_name = str(qr_data.get("gruppe") or "").strip()
            stand_number = str(qr_data.get("standnummer") or "").strip()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            cur.execute(
                """
                INSERT INTO shooters (
                    competition_id, class_id, last_name, first_name,
                    club, group_name, stand_number, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comp_id,
                    class_id,
                    last_name or "Unbekannt",
                    first_name or "-",
                    club,
                    group_name,
                    stand_number,
                    now,
                ),
            )
            new_sid = cur.lastrowid
            conn.commit()
            auto_created = True

            cur.execute(
                """
                SELECT s.*, c.name AS class_name
                FROM shooters s
                LEFT JOIN classes c ON c.id = s.class_id
                WHERE s.id = ?
                """,
                (new_sid,),
            )
            shooter_row = cur.fetchone()

        if shooter_row is None:
            return None

        # Backfill full values into qr_data for rich frontend banner display
        try:
            if not qr_data.get("name") or qr_data.get("name") == "-":
                ln = shooter_row["last_name"] or ""
                fn = shooter_row["first_name"] or ""
                qr_data["name"] = f"{ln}, {fn}" if (ln and fn) else (ln or fn)
            if not qr_data.get("verein") and shooter_row.get("club"):
                qr_data["verein"] = shooter_row["club"]
            if not qr_data.get("klasse") and shooter_row.get("class_name"):
                qr_data["klasse"] = shooter_row["class_name"]
            if not qr_data.get("gruppe") and shooter_row.get("group_name"):
                qr_data["gruppe"] = shooter_row["group_name"]
            if not qr_data.get("standnummer") and shooter_row.get("stand_number"):
                qr_data["standnummer"] = shooter_row["stand_number"]
            if not qr_data.get("wettbewerb") and comp_id:
                cur.execute("SELECT name FROM competitions WHERE id = ?", (comp_id,))
                c_row = cur.fetchone()
                if c_row and c_row["name"]:
                    qr_data["wettbewerb"] = c_row["name"]
        except Exception:
            pass

        return {
            "id": int(shooter_row["id"]),
            "competition_id": int(shooter_row["competition_id"]),
            "class_id": int(shooter_row["class_id"]),
            "class_name": shooter_row["class_name"] or "",
            "last_name": shooter_row["last_name"],
            "first_name": shooter_row["first_name"],
            "club": shooter_row["club"] or "",
            "group_name": shooter_row["group_name"] or "",
            "stand_number": shooter_row["stand_number"] or "",
            "auto_created": auto_created,
        }
    finally:
        conn.close()


@app.route("/api/evaluate-photo", methods=["POST"])
def api_evaluate_photo():
    """
    Receives a photo of a Blasrohr-Ergebniszettel (either multipart/form-data file upload
    or JSON base64 image) and the selected half (1 or 2), evaluates all 5 Passen (6 arrows each)
    plus optional QR code header metadata, and returns the detected ring values, sums,
    confidence scores, QR metadata, matched shooter, and annotated preview images.
    """
    half = 1
    crop_region = None
    image_bytes = None
    competition_id = None
    auto_create_shooter = True

    if request.files and "photo" in request.files:
        file_obj = request.files["photo"]
        image_bytes = file_obj.read()
        half = int(request.form.get("half", 1))
        if request.form.get("competition_id") and str(request.form.get("competition_id")).isdigit():
            competition_id = int(request.form["competition_id"])
        if request.form.get("auto_create_shooter") is not None:
            auto_create_shooter = str(request.form.get("auto_create_shooter")).lower() not in ("0", "false", "no")
        if request.form.get("crop_region"):
            try:
                crop_region = json.loads(request.form["crop_region"])
            except Exception:
                crop_region = None
    else:
        data = request.get_json(force=True, silent=True) or {}
        half = int(data.get("half", 1))
        if data.get("competition_id") is not None:
            try:
                competition_id = int(data["competition_id"])
            except Exception:
                competition_id = None
        if "auto_create_shooter" in data:
            auto_create_shooter = bool(data.get("auto_create_shooter"))
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
        qr_data = result.get("qr_data")
        if qr_data:
            matched_shooter = _resolve_shooter_from_qr(
                qr_data=qr_data,
                competition_id=competition_id,
                auto_create=auto_create_shooter,
            )
            result["matched_shooter"] = matched_shooter
        else:
            result["matched_shooter"] = None
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
        "Gruppe",
        "Standnummer",
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
            r["group_name"],
            r["stand_number"],
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
    ws.merge_cells("A1:W1")
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
        "Gruppe",
        "Stand",
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
        cell.alignment = Alignment(horizontal="center" if col_idx not in (3, 4, 5, 6, 7) else "left")

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
            r["group_name"],
            r["stand_number"],
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
            if c_idx in (14, 20):
                c.fill = sum_fill
                c.font = bold_font
            elif c_idx == 21:
                c.fill = total_fill
                c.font = bold_font

    col_widths = [11, 11, 18, 16, 20, 16, 13, 9, 6, 6, 6, 6, 6, 11, 6, 6, 6, 6, 6, 11, 12, 7, 7]
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


@app.route("/api/competitions/<int:comp_id>/export.json", methods=["GET"])
def export_json(comp_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, date FROM competitions WHERE id = ?", (comp_id,))
    comp_row = cur.fetchone()
    if not comp_row:
        conn.close()
        return jsonify({"error": "Wettkampf nicht gefunden."}), 404

    comp_name = comp_row["name"]
    comp_date = scorecard_pdf.format_date_de(comp_row["date"])

    cur.execute(
        "SELECT id, name FROM classes WHERE competition_id = ? ORDER BY sort_order ASC, name ASC",
        (comp_id,),
    )
    classes_rows = cur.fetchall()
    classes_list = [r["name"] for r in classes_rows]
    class_id_to_name = {r["id"]: r["name"] for r in classes_rows}

    cur.execute(
        "SELECT id, class_id, last_name, first_name, club, group_name, stand_number FROM shooters WHERE competition_id = ? ORDER BY id ASC",
        (comp_id,),
    )
    shooters_rows = cur.fetchall()
    shooters_list = []
    for s in shooters_rows:
        grp = s["group_name"] or ""
        try:
            grp_val = int(grp) if grp != "" and grp.isdigit() else grp
        except Exception:
            grp_val = grp

        st = s["stand_number"] or ""
        try:
            st_val = int(st) if st != "" and st.isdigit() else st
        except Exception:
            st_val = st

        shooters_list.append({
            "name": s["last_name"],
            "vorname": s["first_name"],
            "klasse": class_id_to_name.get(s["class_id"], ""),
            "verein": s["club"] or "",
            "gruppe": grp_val,
            "scheibe": st_val,
        })

    conn.close()

    export_data = {
        "turniername": comp_name,
        "datum": comp_date,
        "klassen": classes_list,
        "schuetzen": shooters_list,
    }

    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in comp_name)
    filename = f"bgScore_{safe_name}.json"

    return Response(
        json.dumps(export_data, indent=2, ensure_ascii=False).encode("utf-8"),
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------------------
# Blank Template, Pre-filled Scorecard PDFs & Synthetic Demo Generator
# -------------------------------------------------------------------------
def _safe_filename_part(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", "ä", "ö", "ü", "Ä", "Ö", "Ü", "ß") else "_" for ch in (text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "Unbekannt"


def _parse_halves_param(half_param: str) -> List[int]:
    hp = (half_param or "both").strip().lower()
    if hp in ("1", "a"):
        return [1]
    if hp in ("2", "b"):
        return [2]
    return [1, 2]


def _group_shooters_for_pdf(shooters: List[Dict[str, Any]], mode: str) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """
    Groups shooters according to mode:
      - 'all' / 'gesamt': 1 group containing all shooters
      - 'class' / 'klasse': 1 group per Klasse
      - 'club' / 'verein': 1 group per Verein
      - 'group' / 'gruppe': 1 group per Gruppe
    Returns list of tuples: (key, display_label, sorted_shooters_list).
    """
    m = (mode or "all").strip().lower()

    if m in ("class", "klasse"):
        buckets: Dict[str, Tuple[int, str, List[Dict[str, Any]]]] = {}
        for s in shooters:
            key = str(s["class_id"])
            label = s["class_name"] or "Klasse"
            c_sort = s.get("class_sort", 0)
            if key not in buckets:
                buckets[key] = (c_sort, label, [])
            buckets[key][2].append(s)

        out = []
        for key, (_, label, items) in sorted(buckets.items(), key=lambda kv: (kv[1][0], kv[1][1].lower())):
            items_sorted = sorted(
                items,
                key=lambda x: (
                    _natural_sort_key(x.get("group_name", "")),
                    _natural_sort_key(x.get("stand_number", "")),
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                ),
            )
            out.append((key, label, items_sorted))
        return out

    if m in ("club", "verein"):
        club_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for s in shooters:
            c_val = (s.get("club") or "").strip()
            key = c_val if c_val else "__empty__"
            club_buckets.setdefault(key, []).append(s)

        out = []
        sorted_keys = sorted(
            club_buckets.keys(),
            key=lambda k: (1 if k == "__empty__" else 0, k.lower()),
        )
        for key in sorted_keys:
            label = "Ohne Verein" if key == "__empty__" else key
            items_sorted = sorted(
                club_buckets[key],
                key=lambda x: (
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                ),
            )
            out.append((key, label, items_sorted))
        return out

    if m in ("group", "gruppe"):
        grp_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for s in shooters:
            g_val = (s.get("group_name") or "").strip()
            key = g_val if g_val else "__empty__"
            grp_buckets.setdefault(key, []).append(s)

        out = []
        sorted_keys = sorted(
            grp_buckets.keys(),
            key=lambda k: (1, 999999, "") if k == "__empty__" else _natural_sort_key(k),
        )
        for key in sorted_keys:
            label = "Ohne Gruppe" if key == "__empty__" else key
            items_sorted = sorted(
                grp_buckets[key],
                key=lambda x: (
                    _natural_sort_key(x.get("stand_number", "")),
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                ),
            )
            out.append((key, label, items_sorted))
        return out

    # Default: 'all' / 'gesamt'
    all_sorted = sorted(
        shooters,
        key=lambda x: (
            _natural_sort_key(x.get("group_name", "")),
            _natural_sort_key(x.get("stand_number", "")),
            x["last_name"].lower(),
            x["first_name"].lower(),
        ),
    )
    return [("all", "Gesamt", all_sorted)]


def _parse_manual_pairs_param(
    pairs_raw: Any,
    shooters: List[Dict[str, Any]],
) -> List[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """
    Parses manual pairs from either a query string ('12:15,18:0,0:21')
    or a JSON array of [left_id, right_id] pairs.
    """
    shooter_map = {str(s["id"]): s for s in shooters}
    out: List[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []
    if not pairs_raw:
        return out

    def _resolve_id(val: Any) -> Optional[Dict[str, Any]]:
        if val is None:
            return None
        k = str(val).strip()
        if not k or k.lower() in ("0", "blank", "none", "null", "empty", "-"):
            return None
        return shooter_map.get(k)

    if isinstance(pairs_raw, list):
        for item in pairs_raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append((_resolve_id(item[0]), _resolve_id(item[1])))
            elif isinstance(item, dict):
                out.append((_resolve_id(item.get("left")), _resolve_id(item.get("right"))))
        return out

    s_raw = str(pairs_raw).strip()
    for part in s_raw.split(","):
        part = part.strip()
        if not part:
            continue
        sides = part.split(":", 1)
        left_k = sides[0].strip()
        right_k = sides[1].strip() if len(sides) > 1 else ""
        out.append((_resolve_id(left_k), _resolve_id(right_k)))
    return out


def _serialize_shooter_for_pair(s: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    last_n = (s.get("last_name") or "").strip()
    first_n = (s.get("first_name") or "").strip()
    full_n = f"{last_n}, {first_n}" if (last_n and first_n) else (last_n or first_n)
    return {
        "id": s["id"],
        "name": full_n,
        "last_name": last_n,
        "first_name": first_n,
        "class_name": s.get("class_name") or "",
        "club": s.get("club") or "",
        "group_name": s.get("group_name") or "",
        "stand_number": str(s.get("stand_number") or ""),
    }


@app.route("/api/template-pdf", methods=["GET"])
def download_template_pdf():
    per_page_arg = (request.args.get("per_page") or "1").strip()
    per_page = 2 if per_page_arg == "2" else 1
    pdf_bytes = scorecard_pdf.generate_blank_template_pdf_bytes(per_page=per_page)
    fname = (
        "Blasrohr_Ergebniszettel_Leer_A_und_B_2auf1.pdf"
        if per_page == 2
        else "Blasrohr_Ergebniszettel_Leer_A_und_B.pdf"
    )
    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=fname,
        mimetype="application/pdf",
    )


@app.route("/api/competitions/<int:comp_id>/scorecards-groups", methods=["GET"])
def get_scorecard_groups_summary(comp_id: int):
    """Returns shooter counts grouped by Klasse, Verein, and Gruppe plus auto-pairs for the PDF generator modal."""
    shooters = _build_shooter_results(comp_id)
    by_class = [{"key": k, "label": lbl, "count": len(items)} for k, lbl, items in _group_shooters_for_pdf(shooters, "class")]
    by_club = [{"key": k, "label": lbl, "count": len(items)} for k, lbl, items in _group_shooters_for_pdf(shooters, "club")]
    by_group = [{"key": k, "label": lbl, "count": len(items)} for k, lbl, items in _group_shooters_for_pdf(shooters, "group")]

    sorted_all = _group_shooters_for_pdf(shooters, "all")[0][2] if shooters else []
    raw_auto_pairs = scorecard_pdf.build_adjacent_shooter_pairs(sorted_all)
    auto_pairs = [
        {
            "group_name": p["group_name"] or "Ohne Gruppe",
            "left_stand": p["left_stand"],
            "right_stand": p["right_stand"],
            "left_shooter": _serialize_shooter_for_pair(p["left_shooter"]),
            "right_shooter": _serialize_shooter_for_pair(p["right_shooter"]),
        }
        for p in raw_auto_pairs
    ]

    return jsonify(
        {
            "total_shooters": len(shooters),
            "by_class": by_class,
            "by_club": by_club,
            "by_group": by_group,
            "auto_pairs": auto_pairs,
            "shooters": [_serialize_shooter_for_pair(s) for s in sorted_all],
        }
    )


@app.route("/api/competitions/<int:comp_id>/scorecards.pdf", methods=["GET", "POST"])
def download_prefilled_scorecards_pdf(comp_id: int):
    """
    Generates a pre-filled PDF with Schießzettel (Wettbewerb, Name, Vorname, Verein,
    Datum, Klasse, Entfernung, Gruppe, Standnummer).
    Supports:
      - mode=all (Gesamt-PDF für alle Schützen)
      - mode=class & value=<class_id> (PDF für eine Klasse)
      - mode=club & value=<club_name or __empty__> (PDF für einen Verein)
      - mode=group & value=<group_name or __empty__> (PDF für eine Gruppe)
      - mode=shooter & value=<shooter_id> (PDF für einen einzelnen Schützen)
      - per_page=1 or 2
      - pair_mode=same_shooter | adjacent | manual
      - pairs=<left_id>:<right_id>,... (when pair_mode=manual)
    """
    body_data = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(body_data, dict):
        body_data = {}

    mode = (request.args.get("mode") or body_data.get("mode") or "all").strip().lower()
    value = request.args.get("value") if "value" in request.args else body_data.get("value")
    half_raw = request.args.get("half") or body_data.get("half") or "both"
    halves = _parse_halves_param(str(half_raw))
    override_distance = (request.args.get("distance") or body_data.get("distance") or "").strip() or None

    per_page_raw = str(request.args.get("per_page") or body_data.get("per_page") or "1").strip()
    per_page = 2 if per_page_raw == "2" else 1
    pair_mode = str(request.args.get("pair_mode") or body_data.get("pair_mode") or "same_shooter").strip().lower()
    pairs_param = request.args.get("pairs") if "pairs" in request.args else body_data.get("pairs")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,))
    comp_row = cur.fetchone()
    conn.close()

    if not comp_row:
        return jsonify({"error": "Wettkampf nicht gefunden."}), 404

    comp_dict = dict(comp_row)
    comp_safe = _safe_filename_part(comp_dict["name"])
    shooters = _build_shooter_results(comp_id)

    selected_shooters: List[Dict[str, Any]] = []
    suffix = "Gesamt"

    if pair_mode in ("manual", "2_shooters_manual"):
        per_page = 2
        manual_pairs = _parse_manual_pairs_param(pairs_param, shooters)
        suffix = "Manuelle_Paare"
    else:
        manual_pairs = None
        if mode == "shooter" and value:
            selected_shooters = [s for s in shooters if str(s["id"]) == str(value)]
            if selected_shooters:
                s0 = selected_shooters[0]
                suffix = _safe_filename_part(f"{s0['last_name']}_{s0['first_name']}")
            else:
                suffix = "Schuetze"
        elif mode in ("class", "klasse", "club", "verein", "group", "gruppe"):
            grouped = _group_shooters_for_pdf(shooters, mode)
            if value is not None and value != "":
                for k, lbl, items in grouped:
                    if k == value or lbl == value:
                        selected_shooters = items
                        prefix_map = {"class": "Klasse", "klasse": "Klasse", "club": "Verein", "verein": "Verein", "group": "Gruppe", "gruppe": "Gruppe"}
                        suffix = f"{prefix_map.get(mode, 'Auswahl')}_{_safe_filename_part(lbl)}"
                        break
            else:
                # If no specific value filter passed, concatenate all groups in sorted order
                for _, _, items in grouped:
                    selected_shooters.extend(items)
                suffix = f"Alle_nach_{_safe_filename_part(mode)}"
        else:
            grouped = _group_shooters_for_pdf(shooters, "all")
            selected_shooters = grouped[0][2] if grouped else []
            suffix = "Gesamt"

    pdf_bytes = scorecard_pdf.generate_prefilled_scorecards_pdf_bytes(
        shooters=selected_shooters,
        competition=comp_dict,
        halves=halves,
        override_distance=override_distance,
        per_page=per_page,
        pair_mode=pair_mode,
        manual_pairs=manual_pairs,
    )

    layout_tag = ""
    if pair_mode in ("adjacent", "2_shooters_auto"):
        layout_tag = "_2Schuetzen_Scheiben"
    elif pair_mode in ("manual", "2_shooters_manual"):
        layout_tag = "_2Schuetzen"
    elif per_page == 2:
        layout_tag = "_2auf1"

    filename = f"Schiesszettel_{comp_safe}_{suffix}{layout_tag}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


@app.route("/api/competitions/<int:comp_id>/scorecards.zip", methods=["GET"])
def download_prefilled_scorecards_zip(comp_id: int):
    """
    Generates one separate PDF per Klasse, per Verein, or per Gruppe and bundles them
    into a downloadable ZIP archive.
    """
    mode = (request.args.get("mode") or "class").strip().lower()
    halves = _parse_halves_param(request.args.get("half") or "both")
    override_distance = (request.args.get("distance") or "").strip() or None
    per_page_raw = (request.args.get("per_page") or "1").strip()
    per_page = 2 if per_page_raw == "2" else 1
    pair_mode = (request.args.get("pair_mode") or "same_shooter").strip().lower()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,))
    comp_row = cur.fetchone()
    conn.close()

    if not comp_row:
        return jsonify({"error": "Wettkampf nicht gefunden."}), 404

    comp_dict = dict(comp_row)
    comp_safe = _safe_filename_part(comp_dict["name"])
    shooters = _build_shooter_results(comp_id)
    grouped = _group_shooters_for_pdf(shooters, mode)

    prefix_map = {
        "class": "Klasse",
        "klasse": "Klasse",
        "club": "Verein",
        "verein": "Verein",
        "group": "Gruppe",
        "gruppe": "Gruppe",
    }
    prefix = prefix_map.get(mode, "Gesamt")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not grouped or not shooters:
            pdf_bytes = scorecard_pdf.generate_prefilled_scorecards_pdf_bytes(
                shooters=[],
                competition=comp_dict,
                halves=halves,
                override_distance=override_distance,
                per_page=per_page,
                pair_mode=pair_mode,
            )
            zf.writestr(f"Schiesszettel_{comp_safe}_Leer.pdf", pdf_bytes)
        else:
            used_names = set()
            for _, label, items in grouped:
                safe_lbl = _safe_filename_part(label)
                base_fname = f"Schiesszettel_{comp_safe}_{prefix}_{safe_lbl}"
                fname = f"{base_fname}.pdf"
                counter = 2
                while fname in used_names:
                    fname = f"{base_fname}_{counter}.pdf"
                    counter += 1
                used_names.add(fname)

                pdf_bytes = scorecard_pdf.generate_prefilled_scorecards_pdf_bytes(
                    shooters=items,
                    competition=comp_dict,
                    halves=halves,
                    override_distance=override_distance,
                    per_page=per_page,
                    pair_mode=pair_mode,
                )
                zf.writestr(fname, pdf_bytes)

    zip_buf.seek(0)
    zip_filename = f"Schiesszettel_{comp_safe}_pro_{prefix}.zip"
    return send_file(
        zip_buf,
        as_attachment=True,
        download_name=zip_filename,
        mimetype="application/zip",
    )


@app.route("/api/demo-scorecard", methods=["GET"])
def api_demo_scorecard():
    """Generates a realistic filled-out Ergebniszettel (A or B) with QR-Code for testing."""
    half = int(request.args.get("half", 1))
    shooter_name = request.args.get("name", "Mustermann, Max")
    club = request.args.get("club", "SV Pfeilflug")
    class_name = request.args.get("class_name", "Herren")
    comp_name = request.args.get("competition", "Vereinsmeisterschaft")
    group_name = request.args.get("group_name", "Gruppe 1")
    stand_number = request.args.get("stand_number", "4")
    date_str = scorecard_pdf.format_date_de(request.args.get("date", "31.10.2026"))
    distance = scorecard_pdf.resolve_distance(class_name, "", request.args.get("distance", ""))
    sid_arg = request.args.get("shooter_id")
    cid_arg = request.args.get("competition_id")
    shooter_id = int(sid_arg) if sid_arg and str(sid_arg).isdigit() else None
    competition_id = int(cid_arg) if cid_arg and str(cid_arg).isdigit() else None

    img_bytes, passes = evaluator.generate_test_scorecard_image(
        half=half,
        shooter_name=shooter_name,
        club=club,
        class_name=class_name,
        competition_name=comp_name,
        date_str=date_str,
        group_name=group_name,
        stand_number=stand_number,
        distance=distance,
        shooter_id=shooter_id,
        competition_id=competition_id,
    )

    if request.args.get("format") == "json":
        b64 = "data:image/jpeg;base64," + base64.b64encode(img_bytes).decode("ascii")
        return jsonify({"image_base64": b64, "half": half, "expected_passes": passes})

    return Response(img_bytes, mimetype="image/jpeg")


if __name__ == "__main__":
    init_db()
    cert_file, key_file = get_ssl_cert_paths()
    if cert_file and key_file:
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

    # Browser nach kurzer Verzögerung automatisch im Standard-Webbrowser öffnen
    import threading
    import time
    import webbrowser

    def _open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open("http://localhost:5000")
        except Exception as e:
            print("Browser-Start Hinweis:", e)

    threading.Thread(target=_open_browser, daemon=True).start()

    print("========================================================")
    print("  bgScore - Automatische Blasrohr-Schiesszettel Webapp")
    print("========================================================")
    print(" * Lokaler Server laeuft auf: http://localhost:5000")
    print(" * Standard-Webbrowser wird geoeffnet...")
    print(" * Schliessen Sie dieses Fenster zum Beenden der Software.")
    print("========================================================")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
