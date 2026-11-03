# bgScore – Automatische Blasrohr-Schießzettel-Auswertung

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-green.svg)](https://palletsprojects.com/p/flask/)
[![Lizenz: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**bgScore** ist eine leistungsfähige, moderne Webanwendung zur **vollautomatischen optischen Erfassung, Auswertung und Verwaltung von Blasrohr-Schießzetteln (Wertungsbögen)**.

Kein lästiges manuelles Zusammenrechnen von Wertungszetteln mehr: Einfach den Schießzettel mit dem Smartphone oder einer Webcam fotografieren – bgScore erkennt den Schützen, die Hälfte (A oder B) des Durchgangs sowie alle angekreuzten Treffer in Sekunden und aktualisiert sofort die Live-Ergebnisliste.

---

## 📖 Ausführliche Dokumentation

Für eine detaillierte Schritt-für-Schritt-Anleitung mit vielen Praxistipps für Wettkampfleiter und Auswerter lesen Sie bitte das:
👉 **[Ausführliche Benutzerhandbuch (BENUTZERHANDBUCH.md)](BENUTZERHANDBUCH.md)**

---

## ✨ Kernfunktionen im Überblick

- 🎯 **Optische Zettelauswertung (Computer Vision):**
  - Erkennt automatisch die 5 Passen-Tabellen auf dem Wertungsbogen.
  - Automatische Begradigung (Auto-Deskew bis ±14°) bei schräg fotografierten Zetteln.
  - Präzise Trefferauswertung für Ringe 10, 9, 8, 7, 6 und 0 (Miss/Fehlschuss).
  - Funktioniert zu 100 % lokal ohne Cloud-Zwang, ohne API-Kosten und ohne KI-Abo!
- 🏁 **Vollautomatische QR-Code-Erkennung:**
  - Schießzettel enthalten einen kompakten QR-Code im Kopfbereich.
  - Beim Fotografieren werden Schütze, Klasse, Verein, Standnummer und Zettelhälfte (Zettel A: Passe 1–5 / Zettel B: Passe 6–10) automatisch erkannt und zugeordnet.
- 📱 **Multi-Device & Smartphone-Support:**
  - Der Vereins-Laptop dient als Server. Helfer verbinden ihr Smartphone einfach per QR-Code ("Handy-Link") im lokalen WLAN.
  - Integrierter **Zielrahmen** im Kamera-Sucher für optimales Ausrichten.
  - Unterstützt sowohl die direkte Live-Kamera (HTTPS) als auch die native Smartphone-Kamera (HTTP).
- 📄 **Druckfertiger Schießzettel-PDF-Generator:**
  - Erstellt offizielle, normgerechte Wertungsbögen im Original-Layout.
  - Wahlweise als Gesamt-PDF, aufgeteilt nach Klassen, Vereinen oder Gruppen/Ständen (auch als gebündeltes ZIP-Archiv).
  - Inklusive Leervorlagen-Druck für spontane Nachmelder.
- 🏆 **Echtzeit-Ergebnisliste:**
  - Live-Aktualisierung alle 3 Sekunden – ideal für die Anzeige auf einem Großbildschirm oder Beamer im Schützenhaus.
  - Rangberechnung: Höchste Ringzahl, bei Gleichstand Auswertung der 10er und 9er.
  - Medaillen-Kennzeichnung (🥇 Gold, 🥈 Silber, 🥉 Bronze) und Schnellfilter nach Klassen.
- 📊 **Excel- & CSV-Export:**
  - **.xlsx:** Professionell formatierte Arbeitsmappe mit Wettkampf-Deckblatt, farbigen Tabellen und allen 10 Passen.
  - **.csv:** Standardisiert mit Semikolon-Trennung und UTF-8-BOM für den direkten Excel-Doppelklick.

---

## 🔄 Ablauf eines Wettkampfs

```mermaid
flowchart LR
    A["1. Schützen anlegen\n(oder importieren)"] --> B["2. PDFs mit QR-Code\nausdrucken"]
    B --> C["3. Schießen & Treffer\nankreuzen"]
    C --> D["4. Zettel mit Handy\nabfotografieren"]
    D --> E["5. Automatische\nErkennung & Prüfung"]
    E --> F["6. Live-Rangliste\n& Excel-Export"]
```

---

## 🚀 Schnellstart (Quick Start)

### Voraussetzungen
- **Python 3.10 oder neuer** (empfohlen: Python 3.11 / 3.12)
- Ein aktueller Webbrowser (Chrome, Edge, Firefox oder Safari)

### 1. Installation
Klonen Sie das Repository oder laden Sie den Quellcode herunter:
```bash
git clone https://github.com/IhrUsername/bgScore.git
cd bgScore
```

Installieren Sie die benötigten Python-Pakete:
```bash
pip install -r requirements.txt
```

### 2. Starten

#### Unter Windows (am einfachsten):
Klicken Sie doppelt auf die Datei **`start_bgscore.bat`**.  
Der lokale Server startet und öffnet die Anwendung automatisch im Browser unter:
👉 `http://localhost:5000`

#### Manuell über die Konsole (Windows / Linux / macOS):
```bash
python app.py
```

### 3. Smartphone im WLAN verbinden
1. Klicken Sie in der Kopfleiste auf **📱 Handy-Link**.
2. Scannen Sie den angezeigten QR-Code mit der Smartphone-Kamera.
3. Schon können Sie Schießzettel direkt mit dem Handy am Schießstand fotografieren!

---

## 📂 Projektstruktur

```
bgScore/
├── app.py                  # Flask Webserver & REST-API-Endpunkte
├── evaluator.py            # Bildanalyse, Deskew, Boxensuche & Kreuz-Erkennung
├── qr_codec.py             # QR-Code Generator & Decoder (Pure Python + Reed-Solomon)
├── scorecard_pdf.py        # PDF-Generator für Schießzettel (ReportLab)
├── start_bgscore.bat       # Windows 1-Klick-Starter
├── requirements.txt        # Liste der Python-Abhängigkeiten
├── bgscore.db              # SQLite-Datenbank (wird beim 1. Start automatisch erstellt)
├── BENUTZERHANDBUCH.md     # Ausführliches Benutzerhandbuch
├── README.md               # Diese Schnellübersicht
├── cert.pem / key.pem      # SSL-Zertifikate für HTTPS-Live-Kamera
├── static/
│   ├── css/style.css       # Responsives UI-Styling
│   └── js/app.js           # Client-Logik, Kamera-Steuerung, Live-Aktualisierung
├── templates/
│   └── index.html          # Single-Page-Application Web-Interface
└── Vorlage/                # Offizielle DSB-Vorlagenmuster (.docx / .pdf)
```

---

## 🛠️ Verwendete Technologien & Bibliotheken

- **Backend:** [Flask](https://flask.palletsprojects.com/), Python SQLite3
- **Bildverarbeitung:** [Pillow (PIL)](https://python-pillow.org/), [NumPy](https://numpy.org/)
- **PDF-Generierung:** [ReportLab](https://www.reportlab.com/)
- **Tabellen-Export:** [openpyxl](https://openpyxl.readthedocs.io/)
- **Frontend:** Vanilla JavaScript (ES6+), Modernes CSS mit Flexbox/Grid, responsives Mobil-Layout

---

## 📄 Lizenz

Dieses Projekt steht unter der **GNU General Public License v3.0 (GPL-3.0)**. Nähere Informationen finden Sie in der Datei [LICENSE](LICENSE).

---

*Viel Erfolg und Gut Schuss mit bgScore!* 🎯
