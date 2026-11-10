// =========================================================================
// bgScore Frontend Application Logic
// =========================================================================

const RING_VALUES = [10, 9, 8, 7, 6, 0];
const RING_LABELS = ["10", "9", "8", "7", "6", "0 (M)"];
const RING_ROW_CLASSES = ["row-10", "row-9", "row-8", "row-7", "row-6", "row-0"];

const state = {
    competitions: [],
    activeCompetitionId: null,
    shooters: [],
    selectedShooterId: null,
    selectedHalf: 1, // 1 = Zettel A (Passe 1-5), 2 = Zettel B (Passe 6-10)
    scanMode: "qr", // 'qr' = Automatisch über QR-Code erkennen, 'manual' = Manuell auswählen
    lastQrData: null,
    evaluatedPasses: [], // Array of 5 objects: { passe_number, arrows: [10,9..], confidences: [1..], sum }
    passeCrops: [],
    liveStream: null,
};

let resultsAutoRefreshTimer = null;
let isAutoRefreshPaused = false;
let lastResultsSignature = "";
let lastRefreshTime = null;
const recentlyUpdatedShooterIds = new Set();
let pollCounter = 0;

document.addEventListener("DOMContentLoaded", async () => {
    // Set default date in competition form to today
    const compDateInput = document.getElementById("compDate");
    if (compDateInput) {
        compDateInput.value = new Date().toISOString().slice(0, 10);
    }

    // Bind events
    document.getElementById("activeCompetitionSelect").addEventListener("change", async (e) => {
        state.activeCompetitionId = parseInt(e.target.value, 10);
        localStorage.setItem("bgscore_active_comp", String(state.activeCompetitionId));
        lastResultsSignature = "";
        await refreshCompetitionData();
    });

    document.getElementById("scanClassFilter").addEventListener("change", () => {
        populateScanShooterDropdown();
    });

    document.getElementById("scanShooterSelect").addEventListener("change", (e) => {
        const val = e.target.value ? parseInt(e.target.value, 10) : null;
        selectShooterForScan(val);
    });

    document.getElementById("nativeCameraInput").addEventListener("change", handleFileSelected);
    document.getElementById("galleryFileInput").addEventListener("change", handleFileSelected);
    document.getElementById("btnShowQrModal").addEventListener("click", openQrModal);

    // Initialize Tournament JSON Import Dropzone
    initImportDropZone();

    // Restore auto-save preference
    const autoSaveCheckbox = document.getElementById("chkAutoSaveOnScan");
    if (autoSaveCheckbox) {
        autoSaveCheckbox.checked = localStorage.getItem("bgscore_auto_save_scan") === "1";
    }

    // Visibility change handler (immediate refresh when user switches back to browser tab)
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && state.activeTab === "results") {
            pollResultsUpdate(false);
        }
    });

    await loadCompetitions();

    // Start auto-refresh polling loop (every 3 seconds on results tab)
    startAutoRefreshLoop();
});

// =========================================================================
// Navigation & Toast Helpers
// =========================================================================
async function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll(".tab-panel").forEach((el) => el.classList.remove("active"));
    document.querySelectorAll(".nav-tab, .mob-nav-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tabName);
    });

    const targetPanel = document.getElementById(`tab-${tabName}`);
    if (targetPanel) {
        targetPanel.classList.add("active");
    }

    if (tabName !== "scan") {
        stopLiveCamera();
    }
    if (tabName === "results") {
        await pollResultsUpdate(false);
        renderResultsTable();
    }
    if (tabName === "shooters") {
        renderManageShootersTable();
    }
    if (tabName === "competition") {
        renderCompetitionsAndClassesTab();
    }
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function showToast(message, type = "info") {
    const container = document.getElementById("toastContainer");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.remove();
    }, 3400);
}

function getActiveCompetition() {
    return state.competitions.find((c) => c.id === state.activeCompetitionId) || null;
}

// =========================================================================
// Load & Render Competitions, Classes, Shooters
// =========================================================================
async function loadCompetitions() {
    try {
        const res = await fetch("/api/competitions");
        state.competitions = await res.json();

        const savedId = parseInt(localStorage.getItem("bgscore_active_comp") || "", 10);
        if (state.competitions.some((c) => c.id === savedId)) {
            state.activeCompetitionId = savedId;
        } else if (state.competitions.length > 0) {
            state.activeCompetitionId = state.competitions[0].id;
        } else {
            state.activeCompetitionId = null;
        }

        renderCompetitionSelector();
        await refreshCompetitionData();
    } catch (err) {
        showToast("Fehler beim Laden der Wettkämpfe.", "error");
    }
}

function renderCompetitionSelector() {
    const select = document.getElementById("activeCompetitionSelect");
    select.innerHTML = "";
    state.competitions.forEach((comp) => {
        const opt = document.createElement("option");
        opt.value = comp.id;
        opt.textContent = `${comp.name} (${comp.date})`;
        if (comp.id === state.activeCompetitionId) opt.selected = true;
        select.appendChild(opt);
    });
}

async function refreshCompetitionData() {
    const comp = getActiveCompetition();
    if (!comp) return;

    // Populate class dropdowns across all tabs
    populateClassDropdowns(comp.classes || []);

    // Load shooters & scores for active competition
    try {
        const res = await fetch(`/api/competitions/${comp.id}/shooters`, {
            cache: "no-store",
            headers: { "Cache-Control": "no-cache" }
        });
        const freshShooters = await res.json();
        if (state.shooters && state.shooters.length > 0) {
            const oldMap = new Map(state.shooters.map((s) => [s.id, s]));
            freshShooters.forEach((s) => {
                const old = oldMap.get(s.id);
                if (
                    !old ||
                    old.total_sum !== s.total_sum ||
                    old.half1_sum !== s.half1_sum ||
                    old.half2_sum !== s.half2_sum
                ) {
                    recentlyUpdatedShooterIds.add(s.id);
                    setTimeout(() => {
                        recentlyUpdatedShooterIds.delete(s.id);
                    }, 4000);
                }
            });
        }
        state.shooters = freshShooters;
        lastResultsSignature = computeShootersSignature(state.shooters);
        lastRefreshTime = new Date();
    } catch (err) {
        state.shooters = [];
    }

    document.getElementById("navShooterCount").textContent = state.shooters.length;

    populateScanShooterDropdown();
    renderResultsTable();
    renderManageShootersTable();
    renderCompetitionsAndClassesTab();
    updateLiveIndicatorUi();
}

function populateClassDropdowns(classes) {
    // 1. Scan Class Filter
    const scanClassFilter = document.getElementById("scanClassFilter");
    const prevScanClass = scanClassFilter.value;
    scanClassFilter.innerHTML = '<option value="">Alle Klassen anzeigen</option>';
    classes.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.name;
        scanClassFilter.appendChild(opt);
    });
    if ([...scanClassFilter.options].some((o) => o.value === prevScanClass)) {
        scanClassFilter.value = prevScanClass;
    }

    // 2. Results Class Filter & Quick Pills
    const resClassFilter = document.getElementById("resultsClassFilter");
    const prevResClass = resClassFilter.value;
    resClassFilter.innerHTML = '<option value="">Alle Klassen</option>';
    classes.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.name;
        resClassFilter.appendChild(opt);
    });
    if ([...resClassFilter.options].some((o) => o.value === prevResClass)) {
        resClassFilter.value = prevResClass;
    }

    // 3. Manage Shooters Class Filter
    const manageFilter = document.getElementById("manageShootersClassFilter");
    const prevManage = manageFilter.value;
    manageFilter.innerHTML = '<option value="">Alle Klassen</option>';
    classes.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.name;
        manageFilter.appendChild(opt);
    });
    if ([...manageFilter.options].some((o) => o.value === prevManage)) {
        manageFilter.value = prevManage;
    }

    // 4. Shooter Creation Class Selects
    ["shooterClassSelect", "quickClassSelect"].forEach((selId) => {
        const sel = document.getElementById(selId);
        if (!sel) return;
        sel.innerHTML = "";
        classes.forEach((c) => {
            const opt = document.createElement("option");
            opt.value = c.id;
            opt.textContent = c.name;
            sel.appendChild(opt);
        });
    });
}

// =========================================================================
// TAB 1: SCAN / EVALUATION WORKFLOW
// =========================================================================
function populateScanShooterDropdown() {
    const classFilterVal = document.getElementById("scanClassFilter").value;
    const select = document.getElementById("scanShooterSelect");

    const filtered = state.shooters.filter((s) => {
        if (classFilterVal && String(s.class_id) !== String(classFilterVal)) return false;
        return true;
    });

    // Sort alphabetically by last_name, first_name for easy lookup on mobile
    filtered.sort((a, b) =>
        `${a.last_name} ${a.first_name}`.localeCompare(`${b.last_name} ${b.first_name}`, "de")
    );

    select.innerHTML = "";
    if (filtered.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "– Bitte zuerst einen Schützen anlegen –";
        select.appendChild(opt);
        selectShooterForScan(null);
        return;
    }

    filtered.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.id;
        const h1Status = s.half1_recorded ? `A:${s.half1_sum}` : "A:offen";
        const h2Status = s.half2_recorded ? `B:${s.half2_sum}` : "B:offen";
        const clubStr = s.club ? ` · ${s.club}` : "";
        const grpParts = [];
        if (s.group_name) grpParts.push(`Gr. ${s.group_name}`);
        if (s.stand_number) grpParts.push(`St. ${s.stand_number}`);
        const grpStr = grpParts.length ? ` · ${grpParts.join("/")}` : "";
        opt.textContent = `${s.last_name}, ${s.first_name} (${s.class_name}${clubStr}${grpStr}) [${h1Status} | ${h2Status}]`;
        select.appendChild(opt);
    });

    if (state.selectedShooterId && filtered.some((s) => s.id === state.selectedShooterId)) {
        select.value = state.selectedShooterId;
    } else {
        state.selectedShooterId = filtered[0].id;
        select.value = state.selectedShooterId;
    }

    selectShooterForScan(state.selectedShooterId);
}

function selectScanMode(mode) {
    state.scanMode = mode === "manual" ? "manual" : "qr";
    const btnQr = document.getElementById("btnScanModeQr");
    const btnManual = document.getElementById("btnScanModeManual");
    const hintBox = document.getElementById("qrModeInfoHint");
    const selectLabel = document.getElementById("scanShooterSelectLabel");

    if (btnQr) btnQr.classList.toggle("active", state.scanMode === "qr");
    if (btnManual) btnManual.classList.toggle("active", state.scanMode === "manual");
    if (hintBox) hintBox.classList.toggle("hidden", state.scanMode !== "qr");
    if (selectLabel) {
        selectLabel.textContent =
            state.scanMode === "qr"
                ? "Schütze (wird bei QR-Scan automatisch gesetzt)"
                : "Schütze auswählen *";
    }
}

function renderQrDetectedBanner(qrData, matchedShooter) {
    const banner = document.getElementById("qrDetectedBanner");
    const grid = document.getElementById("qrDetectedFieldsGrid");
    const halfBadge = document.getElementById("qrDetectedHalfBadge");
    if (!banner || !grid) return;

    if (!qrData) {
        banner.classList.add("hidden");
        grid.innerHTML = "";
        return;
    }

    const halfNum = qrData.half === 2 ? 2 : 1;
    if (halfBadge) {
        halfBadge.textContent =
            halfNum === 1 ? "1. Hälfte · Zettel A (Passe 1–5)" : "2. Hälfte · Zettel B (Passe 6–10)";
    }

    const fields = [
        { label: "Wettbewerb", value: qrData.wettbewerb || "–" },
        { label: "Name, Vorname", value: qrData.name || "–" },
        { label: "Verein", value: qrData.verein || "–" },
        { label: "Datum", value: qrData.datum || "–" },
        { label: "Klasse", value: qrData.klasse || "–" },
        { label: "Entfernung", value: qrData.entfernung || "–" },
        { label: "Gruppe", value: qrData.gruppe || "–" },
        { label: "Standnummer", value: qrData.standnummer || "–" },
    ];

    let itemsHtml = fields
        .map(
            (f) => `
            <div class="qr-field-item">
                <span class="qr-field-label">${escapeHtml(f.label)}:</span>
                <strong class="qr-field-val">${escapeHtml(String(f.value))}</strong>
            </div>
        `
        )
        .join("");

    if (matchedShooter) {
        const statusNote = matchedShooter.auto_created
            ? "Neu aus QR-Code im Wettkampf angelegt ✓"
            : "Schütze im Wettkampf zugeordnet ✓";
        itemsHtml += `
            <div class="qr-field-item qr-field-match">
                <span class="qr-field-label">Zuordnung:</span>
                <strong class="qr-field-val">${escapeHtml(statusNote)}</strong>
            </div>
        `;
    }

    grid.innerHTML = itemsHtml;
    banner.classList.remove("hidden");
}

function selectShooterForScan(shooterId, preserveEvaluatedGrid = false) {
    state.selectedShooterId = shooterId;
    const banner = document.getElementById("selectedShooterBanner");
    const shooter = state.shooters.find((s) => s.id === shooterId);

    if (!shooter) {
        banner.classList.add("hidden");
        return;
    }

    banner.classList.remove("hidden");
    document.getElementById("bannerShooterName").textContent = `${shooter.last_name}, ${shooter.first_name}`;
    const metaParts = [shooter.class_name];
    if (shooter.club) metaParts.push(shooter.club);
    if (shooter.group_name) metaParts.push(`Gruppe: ${shooter.group_name}`);
    if (shooter.stand_number) metaParts.push(`Stand: ${shooter.stand_number}`);
    document.getElementById("bannerShooterMeta").textContent = metaParts.join(" · ");

    const b1 = document.getElementById("badgeHalf1");
    const b2 = document.getElementById("badgeHalf2");

    if (shooter.half1_recorded) {
        b1.textContent = `1. Hälfte (A): ${shooter.half1_sum} Ringe ✓`;
        b1.className = "status-pill done";
    } else {
        b1.textContent = "1. Hälfte (A): offen";
        b1.className = "status-pill";
    }

    if (shooter.half2_recorded) {
        b2.textContent = `2. Hälfte (B): ${shooter.half2_sum} Ringe ✓`;
        b2.className = "status-pill done";
    } else {
        b2.textContent = "2. Hälfte (B): offen";
        b2.className = "status-pill";
    }

    if (!preserveEvaluatedGrid) {
        // Load existing scores for this shooter & half if already recorded
        loadExistingScoresIfPresent(shooter, state.selectedHalf);
    }
}

function setHalfUiButtonsAndOverlay(half) {
    state.selectedHalf = half;
    document.getElementById("btnSelectHalf1").classList.toggle("active", half === 1);
    document.getElementById("btnSelectHalf2").classList.toggle("active", half === 2);

    // Update Live Camera overlay labels (Passe 1-5 vs Passe 6-10)
    const startP = half === 1 ? 1 : 6;
    for (let i = 1; i <= 5; i++) {
        const lbl = document.getElementById(`ovLabelP${i}`);
        if (lbl) lbl.textContent = `Passe ${startP + i - 1}`;
    }
}

function selectScanHalf(half) {
    setHalfUiButtonsAndOverlay(half);

    const shooter = state.shooters.find((s) => s.id === state.selectedShooterId);
    if (shooter) {
        loadExistingScoresIfPresent(shooter, half);
    }
}

function loadExistingScoresIfPresent(shooter, half) {
    const startP = half === 1 ? 1 : 6;
    const isRecorded = half === 1 ? shooter.half1_recorded : shooter.half2_recorded;

    if (isRecorded && shooter.passes_detail) {
        state.evaluatedPasses = [];
        state.passeCrops = [];
        for (let i = 0; i < 5; i++) {
            const pNum = startP + i;
            const detail = shooter.passes_detail[pNum];
            const arrows = detail ? [...detail.arrows] : [0, 0, 0, 0, 0, 0];
            state.evaluatedPasses.push({
                passe_number: pNum,
                arrows: arrows,
                confidences: [1, 1, 1, 1, 1, 1],
                sum: arrows.reduce((a, b) => a + b, 0),
            });
        }
        document.getElementById("photoPreviewDetails").classList.add("hidden");
        document.getElementById("evalConfidenceBadge").classList.add("hidden");
        renderInteractivePassesEditor();
    } else {
        document.getElementById("evalEmptyState").classList.remove("hidden");
        document.getElementById("evalActiveContent").classList.add("hidden");
        document.getElementById("evalConfidenceBadge").classList.add("hidden");
    }
}

function initEmptyManualGrid() {
    if (!state.selectedShooterId) {
        showToast("Bitte zuerst einen Schützen auswählen oder anlegen.", "error");
        return;
    }
    const startP = state.selectedHalf === 1 ? 1 : 6;
    state.evaluatedPasses = [];
    state.passeCrops = [];
    for (let i = 0; i < 5; i++) {
        state.evaluatedPasses.push({
            passe_number: startP + i,
            arrows: [10, 10, 10, 10, 10, 10],
            confidences: [1, 1, 1, 1, 1, 1],
            sum: 60,
        });
    }
    document.getElementById("photoPreviewDetails").classList.add("hidden");
    document.getElementById("evalConfidenceBadge").classList.add("hidden");
    renderInteractivePassesEditor();
}

// =========================================================================
// Camera Capture & Photo Upload (Always-Visible 5-Passen Zielrahmen)
// =========================================================================
let currentFacingMode = "environment";

function showPhotoInsideViewfinder(dataUrl) {
    const photoEl = document.getElementById("viewfinderPhotoPreview");
    const videoEl = document.getElementById("cameraVideo");
    const idleEl = document.getElementById("viewfinderIdleBg");
    if (photoEl && dataUrl) {
        photoEl.src = dataUrl;
        photoEl.classList.remove("hidden");
    }
    if (videoEl) videoEl.classList.add("hidden");
    if (idleEl) idleEl.classList.add("hidden");
}

async function handleFileSelected(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;

    if (state.scanMode === "manual" && !state.selectedShooterId) {
        showToast("Bitte zuerst einen Schützen auswählen (oder 'Automatisch über QR-Code erkennen' nutzen).", "error");
        event.target.value = "";
        return;
    }

    // Show captured/selected photo right inside the Zielrahmen viewfinder
    const reader = new FileReader();
    reader.onload = (e) => {
        if (e.target && e.target.result) {
            showPhotoInsideViewfinder(e.target.result);
        }
    };
    reader.readAsDataURL(file);

    const formData = new FormData();
    formData.append("photo", file);
    formData.append("half", String(state.selectedHalf));
    if (state.activeCompetitionId) {
        formData.append("competition_id", String(state.activeCompetitionId));
    }
    formData.append("auto_create_shooter", state.scanMode === "qr" ? "1" : "0");

    await sendPhotoForEvaluation(formData);
    event.target.value = "";
}

async function startLiveCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        // Browser blocked getUserMedia (usually because of HTTP instead of HTTPS on mobile LAN)
        const httpsPortUrl = `https://${window.location.hostname}:5443`;
        const idleBox = document.getElementById("viewfinderIdleBg");
        if (idleBox) {
            idleBox.classList.remove("hidden");
            idleBox.innerHTML = `
                <div class="idle-hint-box" style="max-width: 320px;">
                    <span style="color: #facc15;">🔒 Live-Stream erfordert HTTPS am Handy</span>
                    <small style="display:block; margin: 6px 0;">
                        Für das Live-Kamerabild direkt im Zielrahmen wechsle bitte zur verschlüsselten Verbindung oder nutze "📷 System-Kamera / Foto":
                    </small>
                    <a href="${httpsPortUrl}" class="btn btn-primary btn-sm" style="margin-top:4px;">
                        🔒 Jetzt zu HTTPS (${httpsPortUrl}) wechseln
                    </a>
                </div>
            `;
        }
        showToast("Für Live-Kamera am Handy bitte den HTTPS-Link (:5443) öffnen oder 'System-Kamera' wählen.", "info");
        return;
    }

    try {
        stopLiveCamera();
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: { ideal: currentFacingMode },
                    width: { ideal: 1920, min: 1280 },
                    height: { ideal: 1440, min: 720 },
                },
                audio: false,
            });
        } catch (innerErr) {
            // Fallback to any available camera (e.g. PC webcam)
            stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        }

        state.liveStream = stream;
        const video = document.getElementById("cameraVideo");
        const photoEl = document.getElementById("viewfinderPhotoPreview");
        const idleEl = document.getElementById("viewfinderIdleBg");

        video.srcObject = stream;
        video.classList.remove("hidden");
        if (photoEl) photoEl.classList.add("hidden");
        if (idleEl) idleEl.classList.add("hidden");

        document.getElementById("liveStreamActiveBar").classList.remove("hidden");
        document.getElementById("overlayTopBanner").textContent =
            "🎯 Live-Kamera aktiv: 5 Passen im Zielrahmen ausrichten";
    } catch (err) {
        showToast(
            "Kein direkter Kamerazugriff möglich. Nutze '📷 System-Kamera / Foto', um das Bild im Zielrahmen auszuwerten.",
            "error"
        );
    }
}

async function switchCameraFacing() {
    currentFacingMode = currentFacingMode === "environment" ? "user" : "environment";
    await startLiveCamera();
}

function stopLiveCamera() {
    if (state.liveStream) {
        state.liveStream.getTracks().forEach((t) => t.stop());
        state.liveStream = null;
    }
    const video = document.getElementById("cameraVideo");
    if (video) video.classList.add("hidden");
    const activeBar = document.getElementById("liveStreamActiveBar");
    if (activeBar) activeBar.classList.add("hidden");

    const photoEl = document.getElementById("viewfinderPhotoPreview");
    const idleEl = document.getElementById("viewfinderIdleBg");
    if (photoEl && photoEl.classList.contains("hidden") && idleEl) {
        idleEl.classList.remove("hidden");
    }
}

async function captureLiveFrame() {
    const video = document.getElementById("cameraVideo");
    if (!video || !video.videoWidth) return;

    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    stopLiveCamera();

    const dataUrl = canvas.toDataURL("image/jpeg", 0.92);
    showPhotoInsideViewfinder(dataUrl);
    await sendBase64ForEvaluation(dataUrl);
}

async function runDemoScorecardScan() {
    if (state.scanMode === "manual" && !state.selectedShooterId) {
        showToast("Bitte zuerst einen Schützen auswählen oder anlegen.", "error");
        return;
    }
    const shooter = state.shooters.find((s) => s.id === state.selectedShooterId);
    const comp = getActiveCompetition();

    const params = new URLSearchParams({
        half: String(state.selectedHalf),
        name: shooter ? `${shooter.last_name}, ${shooter.first_name}` : "Mustermann, Max",
        club: shooter ? shooter.club : "SV Pfeilflug",
        class_name: shooter ? shooter.class_name : "Herren",
        group_name: shooter && shooter.group_name ? shooter.group_name : "Gruppe 1",
        stand_number: shooter && shooter.stand_number ? shooter.stand_number : "4",
        date: comp ? comp.date : "31.10.2026",
        competition: comp ? comp.name : "Vereinsmeisterschaft",
        format: "json",
    });
    if (shooter) {
        params.set("shooter_id", String(shooter.id));
    }
    if (comp) {
        params.set("competition_id", String(comp.id));
    }

    const loadingBox = document.getElementById("scanLoadingState");
    loadingBox.classList.remove("hidden");

    try {
        const demoRes = await fetch(`/api/demo-scorecard?${params.toString()}`);
        const demoData = await demoRes.json();
        showPhotoInsideViewfinder(demoData.image_base64);
        await sendBase64ForEvaluation(demoData.image_base64);
        showToast("Test-Ergebniszettel (inkl. QR-Code) im Zielrahmen geladen und ausgewertet!", "success");
    } catch (err) {
        loadingBox.classList.add("hidden");
        showToast("Fehler beim Erzeugen des Test-Zettels.", "error");
    }
}

async function sendPhotoForEvaluation(formData) {
    const loadingBox = document.getElementById("scanLoadingState");
    loadingBox.classList.remove("hidden");

    try {
        const res = await fetch("/api/evaluate-photo", {
            method: "POST",
            body: formData,
        });
        const data = await res.json();
        loadingBox.classList.add("hidden");

        if (!res.ok || data.error) {
            showToast(data.error || "Fehler bei der Auswertung.", "error");
            return;
        }
        await applyEvaluationResult(data);
    } catch (err) {
        loadingBox.classList.add("hidden");
        showToast("Netzwerkfehler bei der Bildauswertung.", "error");
    }
}

async function sendBase64ForEvaluation(imageBase64) {
    const loadingBox = document.getElementById("scanLoadingState");
    loadingBox.classList.remove("hidden");

    try {
        const res = await fetch("/api/evaluate-photo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                half: state.selectedHalf,
                competition_id: state.activeCompetitionId,
                auto_create_shooter: state.scanMode === "qr",
                image_base64: imageBase64,
            }),
        });
        const data = await res.json();
        loadingBox.classList.add("hidden");

        if (!res.ok || data.error) {
            showToast(data.error || "Fehler bei der Auswertung.", "error");
            return;
        }
        await applyEvaluationResult(data);
    } catch (err) {
        loadingBox.classList.add("hidden");
        showToast("Fehler bei der Bildauswertung.", "error");
    }
}

async function applyQrRecognition(data) {
    const qr = data.qr_data;
    if (!qr) {
        renderQrDetectedBanner(null, null);
        if (state.scanMode === "qr") {
            showToast(
                "Kein QR-Code erkannt – bitte Zettel vollständig (inkl. QR-Code oben rechts) fotografieren oder Schütze/Hälfte manuell prüfen.",
                "error"
            );
        }
        return;
    }

    // Half: the backend already evaluated the Passen with the half from the QR code
    const qrHalf = Number(data.half || qr.half) === 2 ? 2 : 1;
    setHalfUiButtonsAndOverlay(qrHalf);
    const halfText = qrHalf === 1 ? "1. Hälfte (Zettel A)" : "2. Hälfte (Zettel B)";

    const ms = data.matched_shooter;
    if (ms && state.scanMode === "qr") {
        // If the shooter was newly created or isn't known locally yet, reload competition data
        const existsInState = state.shooters.some((s) => s.id === ms.id);
        if (ms.auto_created || !existsInState) {
            if (ms.competition_id && ms.competition_id !== state.activeCompetitionId) {
                state.activeCompetitionId = ms.competition_id;
                localStorage.setItem("bgscore_active_comp", String(ms.competition_id));
            }
            await loadCompetitions();
        }

        // Make sure the class filter doesn't hide the matched shooter
        const classFilterEl = document.getElementById("scanClassFilter");
        if (classFilterEl && classFilterEl.value && String(classFilterEl.value) !== String(ms.class_id)) {
            classFilterEl.value = "";
        }

        state.selectedShooterId = ms.id;
        populateScanShooterDropdown();
        const selectEl = document.getElementById("scanShooterSelect");
        if (selectEl) selectEl.value = String(ms.id);
        selectShooterForScan(ms.id, true);

        showToast(`QR-Code erkannt: ${ms.last_name}, ${ms.first_name} · ${halfText}`, "success");
    } else if (ms && state.scanMode === "manual") {
        if (state.selectedShooterId !== ms.id) {
            showToast(
                `Hinweis: QR-Code gehört zu ${ms.last_name}, ${ms.first_name} – manuell gewählter Schütze wurde beibehalten.`,
                "info"
            );
        }
    } else {
        showToast(`QR-Code erkannt (${qr.name || "Zettel"} · ${halfText}), aber kein Schütze zugeordnet.`, "info");
    }

    renderQrDetectedBanner(qr, ms || null);
}

async function applyEvaluationResult(data) {
    state.lastQrData = data.qr_data || null;

    // 1. Handle QR-Code Automatic Recognition (Header fields, Half A/B, Shooter Matching)
    try {
        await applyQrRecognition(data);
    } catch (err) {
        console.error("QR-Zuordnung fehlgeschlagen:", err);
    }

    // 2. Populate evaluated 5 Passen
    state.evaluatedPasses = data.passes.map((p) => ({
        passe_number: p.passe_number,
        arrows: [...p.arrows],
        confidences: [...(p.confidences || [1, 1, 1, 1, 1, 1])],
        sum: p.sum,
    }));
    state.passeCrops = data.passe_crops || [];

    // Update Annotated Preview Image
    const previewDetails = document.getElementById("photoPreviewDetails");
    const previewImg = document.getElementById("annotatedPreviewImg");
    if (data.annotated_image) {
        previewImg.src = data.annotated_image;
        previewDetails.classList.remove("hidden");
    }

    // Confidence Badge
    const confBadge = document.getElementById("evalConfidenceBadge");
    const pct = Math.round((data.average_confidence || 0.9) * 100);
    const qrBadgeSuffix = data.qr_data ? " · QR ✓" : "";
    confBadge.textContent = `Erkennung: ${pct}% Sicherheit${qrBadgeSuffix}`;
    confBadge.classList.remove("hidden");

    const modeMap = {
        auto_5_boxes: "5 Passen-Kästen selbstständig erkannt ✓",
        auto_seed_constellation: "Passen-Kästen über Raster-Anker erkannt ✓",
        auto_global_search: "Passen-Kästen auf Gesamtzettel lokalisiert ✓",
        auto_inferred_from_partial: "Passen-Raster automatisch berechnet ✓",
        manual_frame: "Manueller Rahmen ausgewertet",
        template_fallback: "Standard-Zettel-Layout angewendet",
    };
    const angleNote =
        data.deskew_angle && Math.abs(data.deskew_angle) >= 0.4
            ? ` (Schräglage ${data.deskew_angle}° automatisch korrigiert)`
            : "";
    const qrModeNote = data.qr_data ? " · QR-Code im Kopfbereich gelesen ✓" : "";
    document.getElementById("detectionModeLabel").textContent =
        (modeMap[data.detection_mode] || data.detection_mode) + angleNote + qrModeNote;

    renderInteractivePassesEditor();

    // If auto-save on scan is enabled and a shooter is selected with 5 passes:
    const isAutoSave = localStorage.getItem("bgscore_auto_save_scan") === "1";
    if (isAutoSave && state.selectedShooterId && state.evaluatedPasses && state.evaluatedPasses.length === 5) {
        await saveEvaluatedScores(true);
    }

    // Smoothly scroll to results on mobile
    if (window.innerWidth < 900) {
        document.getElementById("evaluationResultCard").scrollIntoView({ behavior: "smooth" });
    }
}

// =========================================================================
// Render Interactive 5-Passen Grid Editor (1-Tap Cross Correction)
// =========================================================================
function renderInteractivePassesEditor() {
    document.getElementById("evalEmptyState").classList.add("hidden");
    document.getElementById("evalActiveContent").classList.remove("hidden");

    const halfLabel =
        state.selectedHalf === 1
            ? "Ergebnis 1. Hälfte – Zettel A (Passe 1 – 5)"
            : "Ergebnis 2. Hälfte – Zettel B (Passe 6 – 10)";
    document.getElementById("evalSummaryTitle").textContent = halfLabel;

    const container = document.getElementById("interactivePassesContainer");
    container.innerHTML = "";

    let halfTotal = 0;

    state.evaluatedPasses.forEach((passeObj, pIdx) => {
        const pSum = passeObj.arrows.reduce((acc, v) => acc + v, 0);
        passeObj.sum = pSum;
        halfTotal += pSum;

        const card = document.createElement("div");
        card.className = "passe-card";

        const cropHtml = state.passeCrops[pIdx]
            ? `<img src="${state.passeCrops[pIdx]}" alt="Passe ${passeObj.passe_number}" class="passe-crop-preview">`
            : "";

        let rowsHtml = "";
        RING_VALUES.forEach((ringVal, rIdx) => {
            let cellsHtml = "";
            for (let arrowIdx = 0; arrowIdx < 6; arrowIdx++) {
                const isSelected = passeObj.arrows[arrowIdx] === ringVal;
                const conf = passeObj.confidences ? passeObj.confidences[arrowIdx] : 1.0;
                const lowConfClass = isSelected && conf < 0.7 ? "low-conf" : "";
                cellsHtml += `
                    <td class="cross-cell ${isSelected ? "selected" : ""} ${lowConfClass}"
                        onclick="togglePasseCell(${pIdx}, ${arrowIdx}, ${ringVal})"
                        title="Passe ${passeObj.passe_number}, Pfeil ${arrowIdx + 1}: ${RING_LABELS[rIdx]} Ringe">
                        ${isSelected ? "✕" : ""}
                    </td>
                `;
            }

            rowsHtml += `
                <tr class="${RING_ROW_CLASSES[rIdx]}">
                    <td class="ring-label">${RING_LABELS[rIdx]}</td>
                    ${cellsHtml}
                </tr>
            `;
        });

        card.innerHTML = `
            <div class="passe-card-header">
                <span>Passe ${passeObj.passe_number}</span>
                <span id="passeHeaderSum-${pIdx}">${pSum} Ringe</span>
            </div>
            ${cropHtml}
            <table class="passe-grid-table">
                <thead>
                    <tr>
                        <th class="col-ringe">Ringe \\ Pfeil</th>
                        <th class="col-arrow">1</th>
                        <th class="col-arrow">2</th>
                        <th class="col-arrow">3</th>
                        <th class="col-arrow">4</th>
                        <th class="col-arrow">5</th>
                        <th class="col-arrow">6</th>
                    </tr>
                </thead>
                <tbody>
                    ${rowsHtml}
                    <tr class="sum-row">
                        <td>Summe &Sigma;</td>
                        <td colspan="6">
                            <span id="passeSumVal-${pIdx}">${pSum}</span>
                            <span class="passe-arrows-summary" id="passeArrowsText-${pIdx}">
                                (${passeObj.arrows.join(" + ")})
                            </span>
                        </td>
                    </tr>
                </tbody>
            </table>
        `;

        container.appendChild(card);
    });

    document.getElementById("evalHalfTotalValue").textContent = halfTotal;

    const shooter = state.shooters.find((s) => s.id === state.selectedShooterId);
    const saveBtn = document.getElementById("btnSaveScores");
    if (shooter) {
        saveBtn.textContent = `💾 ${state.selectedHalf}. Hälfte (${halfTotal} Ringe) für ${shooter.first_name} ${shooter.last_name} speichern`;
    }
}

function togglePasseCell(passeIdx, arrowIdx, ringVal) {
    const passeObj = state.evaluatedPasses[passeIdx];
    if (!passeObj) return;

    passeObj.arrows[arrowIdx] = ringVal;
    if (passeObj.confidences) {
        passeObj.confidences[arrowIdx] = 1.0; // Manually verified
    }
    renderInteractivePassesEditor();
}

async function saveEvaluatedScores(isAuto = false) {
    if (!state.selectedShooterId) {
        if (!isAuto) showToast("Bitte zuerst einen Schützen auswählen!", "error");
        return;
    }

    try {
        const res = await fetch(`/api/shooters/${state.selectedShooterId}/scores`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                half: state.selectedHalf,
                passes: state.evaluatedPasses,
            }),
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(data.error || "Fehler beim Speichern.", "error");
            return;
        }

        const shooter = state.shooters.find((s) => s.id === state.selectedShooterId);
        const halfTotal = state.evaluatedPasses.reduce((acc, p) => acc + (p.sum || 0), 0);
        const nameStr = shooter ? `${shooter.first_name} ${shooter.last_name}` : "Schütze";

        recentlyUpdatedShooterIds.add(state.selectedShooterId);
        setTimeout(() => {
            recentlyUpdatedShooterIds.delete(state.selectedShooterId);
        }, 4000);

        const successMsg = isAuto
            ? `⚡ Auto-Save: ${nameStr} (${halfTotal} Ringe, ${state.selectedHalf}. Hälfte) gespeichert & Ergebnisliste aktualisiert!`
            : (data.message || `Ergebnis für ${nameStr} (${halfTotal} Ringe) erfolgreich gespeichert!`);

        showToast(successMsg, "success");

        // Show banner in scan card
        const banner = document.getElementById("saveSuccessBanner");
        const msgEl = document.getElementById("saveSuccessMsg");
        if (banner && msgEl) {
            msgEl.textContent = `${nameStr}: ${state.selectedHalf}. Hälfte (${halfTotal} Ringe) gespeichert · Ergebnisliste aktualisiert ✓`;
            banner.classList.remove("hidden");
        }

        await refreshCompetitionData();
    } catch (err) {
        showToast("Netzwerkfehler beim Speichern.", "error");
    }
}

// =========================================================================
// TAB 2: RESULTS LIST, FILTERING & CSV/EXCEL EXPORT
// =========================================================================
function renderResultsTable() {
    const comp = getActiveCompetition();
    if (!comp) return;

    const classFilterVal = document.getElementById("resultsClassFilter").value;
    const searchQuery = (document.getElementById("resultsSearchInput").value || "").trim().toLowerCase();

    // Render Class Filter Pills
    const pillsBar = document.getElementById("classFilterPills");
    pillsBar.innerHTML = "";
    const allPill = document.createElement("button");
    allPill.type = "button";
    allPill.className = `class-pill ${classFilterVal === "" ? "active" : ""}`;
    allPill.textContent = `Alle Klassen (${state.shooters.length})`;
    allPill.onclick = () => {
        document.getElementById("resultsClassFilter").value = "";
        renderResultsTable();
    };
    pillsBar.appendChild(allPill);

    (comp.classes || []).forEach((cls) => {
        const count = state.shooters.filter((s) => s.class_id === cls.id).length;
        const pill = document.createElement("button");
        pill.type = "button";
        pill.className = `class-pill ${String(classFilterVal) === String(cls.id) ? "active" : ""}`;
        pill.textContent = `${cls.name} (${count})`;
        pill.onclick = () => {
            document.getElementById("resultsClassFilter").value = String(cls.id);
            renderResultsTable();
        };
        pillsBar.appendChild(pill);
    });

    // Filter shooters
    const filtered = state.shooters.filter((s) => {
        if (classFilterVal && String(s.class_id) !== String(classFilterVal)) return false;
        if (searchQuery) {
            const hay = `${s.last_name} ${s.first_name} ${s.club} ${s.class_name} ${s.group_name || ""} ${s.stand_number || ""}`.toLowerCase();
            if (!hay.includes(searchQuery)) return false;
        }
        return true;
    });

    // Update KPI Cards
    document.getElementById("kpiTotalShooters").textContent = filtered.length;
    document.getElementById("kpiHalf1Done").textContent = filtered.filter((s) => s.half1_recorded).length;
    document.getElementById("kpiHalf2Done").textContent = filtered.filter((s) => s.half2_recorded).length;

    const withTotal = filtered.filter((s) => s.total_sum !== null);
    if (withTotal.length > 0) {
        const maxScore = Math.max(...withTotal.map((s) => s.total_sum));
        document.getElementById("kpiBestScore").textContent = `${maxScore} Ringe`;
    } else {
        document.getElementById("kpiBestScore").textContent = "–";
    }

    const tbody = document.getElementById("resultsTableBody");
    tbody.innerHTML = "";

    if (filtered.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="20" class="text-center text-muted" style="padding: 28px;">
                    Keine Schützen für diesen Filter gefunden.
                </td>
            </tr>
        `;
        return;
    }

    filtered.forEach((s, idx) => {
        const rankNum = classFilterVal ? s.rank_in_class : s.rank_overall;
        const rankClass = rankNum && rankNum <= 3 ? `rank-${rankNum}` : "";
        const rankHtml = rankNum ? `<span class="rank-badge ${rankClass}">${rankNum}</span>` : "–";

        const p = s.passes || {};
        const fmtP = (n) => (p[n] !== null && p[n] !== undefined ? p[n] : "–");
        const grpStandText = formatGroupStandText(s.group_name, s.stand_number);

        const tr = document.createElement("tr");
        if (recentlyUpdatedShooterIds.has(s.id)) {
            tr.className = "tr-row-updated";
        }
        tr.innerHTML = `
            <td>${rankHtml}</td>
            <td><strong>${escapeHtml(s.last_name)}, ${escapeHtml(s.first_name)}</strong></td>
            <td>${escapeHtml(s.club || "–")}</td>
            <td><span class="status-pill">${escapeHtml(s.class_name)}</span></td>
            <td>${escapeHtml(grpStandText)}</td>
            <td class="num-col passe-col">${fmtP(1)}</td>
            <td class="num-col passe-col">${fmtP(2)}</td>
            <td class="num-col passe-col">${fmtP(3)}</td>
            <td class="num-col passe-col">${fmtP(4)}</td>
            <td class="num-col passe-col">${fmtP(5)}</td>
            <td class="num-col half-col">${s.half1_sum !== null ? s.half1_sum : "–"}</td>
            <td class="num-col passe-col">${fmtP(6)}</td>
            <td class="num-col passe-col">${fmtP(7)}</td>
            <td class="num-col passe-col">${fmtP(8)}</td>
            <td class="num-col passe-col">${fmtP(9)}</td>
            <td class="num-col passe-col">${fmtP(10)}</td>
            <td class="num-col half-col">${s.half2_sum !== null ? s.half2_sum : "–"}</td>
            <td class="num-col total-col">${s.total_sum !== null ? s.total_sum : "–"}</td>
            <td class="num-col tie-col">${s.total_sum !== null ? `${s.tens_count} / ${s.nines_count}` : "–"}</td>
            <td>
                <button type="button" class="btn btn-outline btn-sm" onclick="jumpToShooterScan(${s.id}, 1)" title="1. Hälfte (Zettel A) scannen/bearbeiten">
                    📸 A
                </button>
                <button type="button" class="btn btn-outline btn-sm" onclick="jumpToShooterScan(${s.id}, 2)" title="2. Hälfte (Zettel B) scannen/bearbeiten">
                    📸 B
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function formatGroupStandText(groupName, standNumber) {
    const g = (groupName || "").trim();
    const st = (standNumber || "").trim();
    if (g && st) return `${g} / St. ${st}`;
    if (g) return g;
    if (st) return `St. ${st}`;
    return "–";
}

function jumpToShooterScan(shooterId, half) {
    document.getElementById("scanClassFilter").value = "";
    state.selectedShooterId = shooterId;
    populateScanShooterDropdown();
    selectScanHalf(half);
    switchTab("scan");
}

function exportResults(format = "csv") {
    if (!state.activeCompetitionId) return;
    const classId = document.getElementById("resultsClassFilter").value;
    const query = classId ? `?class_id=${encodeURIComponent(classId)}` : "";
    window.location.href = `/api/competitions/${state.activeCompetitionId}/export.${format}${query}`;
}

// =========================================================================
// TAB 3: SHOOTERS MANAGEMENT
// =========================================================================
async function handleShooterSubmit(event) {
    event.preventDefault();
    if (!state.activeCompetitionId) return;

    const editId = document.getElementById("editShooterId").value;
    const payload = {
        last_name: document.getElementById("shooterLastName").value.trim(),
        first_name: document.getElementById("shooterFirstName").value.trim(),
        club: document.getElementById("shooterClub").value.trim(),
        class_id: parseInt(document.getElementById("shooterClassSelect").value, 10),
        group_name: document.getElementById("shooterGroup").value.trim(),
        stand_number: document.getElementById("shooterStand").value.trim(),
    };

    try {
        const url = editId
            ? `/api/shooters/${editId}`
            : `/api/competitions/${state.activeCompetitionId}/shooters`;
        const method = editId ? "PUT" : "POST";

        const res = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(data.error || "Fehler beim Speichern des Schützen.", "error");
            return;
        }

        if (!editId && data.id) {
            state.selectedShooterId = data.id;
        }
        showToast(editId ? "Schütze aktualisiert." : "Schütze erfolgreich angelegt.", "success");
        resetShooterForm();
        await refreshCompetitionData();
    } catch (err) {
        showToast("Netzwerkfehler.", "error");
    }
}

function editShooter(shooterId) {
    const s = state.shooters.find((item) => item.id === shooterId);
    if (!s) return;

    document.getElementById("editShooterId").value = s.id;
    document.getElementById("shooterLastName").value = s.last_name;
    document.getElementById("shooterFirstName").value = s.first_name;
    document.getElementById("shooterClub").value = s.club || "";
    document.getElementById("shooterClassSelect").value = s.class_id;
    document.getElementById("shooterGroup").value = s.group_name || "";
    document.getElementById("shooterStand").value = s.stand_number || "";

    document.getElementById("shooterFormTitle").textContent = "Schütze bearbeiten";
    document.getElementById("btnShooterSubmit").textContent = "Änderungen speichern";
    document.getElementById("btnShooterCancelEdit").classList.remove("hidden");
}

function resetShooterForm() {
    document.getElementById("editShooterId").value = "";
    document.getElementById("shooterLastName").value = "";
    document.getElementById("shooterFirstName").value = "";
    document.getElementById("shooterClub").value = "";
    document.getElementById("shooterGroup").value = "";
    document.getElementById("shooterStand").value = "";
    document.getElementById("shooterFormTitle").textContent = "Schütze anlegen";
    document.getElementById("btnShooterSubmit").textContent = "+ Schütze speichern";
    document.getElementById("btnShooterCancelEdit").classList.add("hidden");
}

async function deleteShooter(shooterId) {
    const s = state.shooters.find((item) => item.id === shooterId);
    if (!s) return;
    if (!confirm(`Schütze "${s.last_name}, ${s.first_name}" inklusive aller Ergebnisse wirklich löschen?`)) {
        return;
    }
    try {
        await fetch(`/api/shooters/${shooterId}`, { method: "DELETE" });
        showToast("Schütze gelöscht.", "info");
        await refreshCompetitionData();
    } catch (err) {
        showToast("Fehler beim Löschen.", "error");
    }
}

function renderManageShootersTable() {
    const classFilter = document.getElementById("manageShootersClassFilter").value;
    const tbody = document.getElementById("manageShootersTableBody");
    tbody.innerHTML = "";

    const list = state.shooters.filter((s) => !classFilter || String(s.class_id) === String(classFilter));
    list.sort((a, b) => `${a.last_name} ${a.first_name}`.localeCompare(`${b.last_name} ${b.first_name}`, "de"));

    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted" style="padding: 20px;">Noch keine Schützen angelegt.</td></tr>`;
        return;
    }

    list.forEach((s) => {
        const tr = document.createElement("tr");
        const h1Badge = s.half1_recorded
            ? `<span class="status-pill done">A: ${s.half1_sum}</span>`
            : `<span class="status-pill">A: –</span>`;
        const h2Badge = s.half2_recorded
            ? `<span class="status-pill done">B: ${s.half2_sum}</span>`
            : `<span class="status-pill">B: –</span>`;
        const grpStandText = formatGroupStandText(s.group_name, s.stand_number);

        tr.innerHTML = `
            <td><strong>${escapeHtml(s.last_name)}, ${escapeHtml(s.first_name)}</strong></td>
            <td>${escapeHtml(s.club || "–")}</td>
            <td>${escapeHtml(s.class_name)}</td>
            <td>${escapeHtml(grpStandText)}</td>
            <td>${h1Badge} ${h2Badge}</td>
            <td>
                <button type="button" class="btn btn-outline btn-sm" onclick="downloadShooterScorecardPdf(${s.id})" title="Vorbefüllten Schießzettel (PDF) für diesen Schützen herunterladen">📄 PDF</button>
                <button type="button" class="btn btn-outline btn-sm" onclick="jumpToShooterScan(${s.id}, 1)">📸 Scannen</button>
                <button type="button" class="btn btn-outline btn-sm" onclick="editShooter(${s.id})">✏️</button>
                <button type="button" class="btn-danger-sm" onclick="deleteShooter(${s.id})">🗑️</button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// Quick Shooter Modal (from Scan tab)
function openQuickShooterModal() {
    document.getElementById("quickShooterModal").classList.remove("hidden");
    document.getElementById("quickLastName").focus();
}

function closeQuickShooterModal() {
    document.getElementById("quickShooterModal").classList.add("hidden");
    document.getElementById("quickShooterForm").reset();
}

async function handleQuickShooterSubmit(event) {
    event.preventDefault();
    if (!state.activeCompetitionId) return;

    const payload = {
        last_name: document.getElementById("quickLastName").value.trim(),
        first_name: document.getElementById("quickFirstName").value.trim(),
        club: document.getElementById("quickClub").value.trim(),
        class_id: parseInt(document.getElementById("quickClassSelect").value, 10),
        group_name: document.getElementById("quickGroup").value.trim(),
        stand_number: document.getElementById("quickStand").value.trim(),
    };

    try {
        const res = await fetch(`/api/competitions/${state.activeCompetitionId}/shooters`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(data.error || "Fehler beim Anlegen.", "error");
            return;
        }
        state.selectedShooterId = data.id;
        closeQuickShooterModal();
        showToast("Schütze angelegt und ausgewählt!", "success");
        await refreshCompetitionData();
    } catch (err) {
        showToast("Fehler beim Anlegen des Schützen.", "error");
    }
}

// =========================================================================
// TAB 4: COMPETITION & CLASSES MANAGEMENT
// =========================================================================
async function handleCreateCompetition(event) {
    event.preventDefault();
    const payload = {
        name: document.getElementById("compName").value.trim(),
        date: document.getElementById("compDate").value,
        location: document.getElementById("compLocation").value.trim(),
        create_default_classes: document.getElementById("compDefaultClasses").checked,
    };

    try {
        const res = await fetch("/api/competitions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(data.error || "Fehler beim Anlegen.", "error");
            return;
        }
        localStorage.setItem("bgscore_active_comp", String(data.id));
        document.getElementById("compName").value = "";
        showToast("Wettkampf erfolgreich angelegt!", "success");
        await loadCompetitions();
    } catch (err) {
        showToast("Netzwerkfehler.", "error");
    }
}

async function deleteCompetition(compId) {
    if (state.competitions.length <= 1) {
        showToast("Der letzte verbleibende Wettkampf kann nicht gelöscht werden.", "error");
        return;
    }
    if (!confirm("Diesen Wettkampf inklusive aller zugehörigen Klassen und Schützen wirklich löschen?")) {
        return;
    }
    await fetch(`/api/competitions/${compId}`, { method: "DELETE" });
    showToast("Wettkampf gelöscht.", "info");
    await loadCompetitions();
}

function exportCompetitionJson(compId) {
    if (!compId) return;
    window.location.href = `/api/competitions/${compId}/export.json`;
}

function exportActiveCompetitionJson() {
    if (!state.activeCompetitionId) {
        showToast("Kein aktiver Wettkampf ausgewählt.", "warning");
        return;
    }
    exportCompetitionJson(state.activeCompetitionId);
}

async function handleCreateClass(event) {
    event.preventDefault();
    const nameInput = document.getElementById("newClassName");
    const name = nameInput.value.trim();
    if (!name || !state.activeCompetitionId) return;

    await quickAddClass(name);
    nameInput.value = "";
}

async function quickAddClass(className) {
    if (!state.activeCompetitionId) return;
    const res = await fetch(`/api/competitions/${state.activeCompetitionId}/classes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: className }),
    });
    if (res.ok) {
        showToast(`Klasse "${className}" hinzugefügt.`, "success");
        await loadCompetitions();
    }
}

async function deleteClass(classId) {
    const res = await fetch(`/api/classes/${classId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
        showToast(data.error || "Klasse konnte nicht gelöscht werden.", "error");
        return;
    }
    showToast("Klasse gelöscht.", "info");
    await loadCompetitions();
}

function renderCompetitionsAndClassesTab() {
    const comp = getActiveCompetition();
    const compList = document.getElementById("competitionsListContainer");
    compList.innerHTML = "";

    state.competitions.forEach((c) => {
        const isActive = c.id === state.activeCompetitionId;
        const locPart = c.location ? ` · 📍 ${escapeHtml(c.location)}` : "";
        const item = document.createElement("div");
        item.className = `list-item ${isActive ? "active-comp" : ""}`;
        item.innerHTML = `
            <div>
                <strong>${escapeHtml(c.name)}</strong>
                <div class="text-muted" style="font-size: 0.8rem;">
                    📅 ${escapeHtml(c.date)}${locPart} · 👥 ${c.shooter_count || 0} Schützen
                </div>
            </div>
            <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
                ${
                    isActive
                        ? `<span class="status-pill done">Aktiv</span>`
                        : `<button type="button" class="btn btn-outline btn-sm" onclick="switchActiveCompetition(${c.id})">Auswählen</button>`
                }
                <button type="button" class="btn btn-outline btn-sm" onclick="exportCompetitionJson(${c.id})" title="Wettkampf als JSON-Datei exportieren">📋 JSON</button>
                <button type="button" class="btn-danger-sm" onclick="deleteCompetition(${c.id})" title="Wettkampf löschen">🗑️</button>
            </div>
        `;
        compList.appendChild(item);
    });

    const classesContainer = document.getElementById("classesListContainer");
    classesContainer.innerHTML = "";
    if (!comp) return;

    document.getElementById("activeCompLabelInClasses").textContent = `${comp.name} (${comp.date})`;

    (comp.classes || []).forEach((cls) => {
        const shooterCount = state.shooters.filter((s) => s.class_id === cls.id).length;
        const item = document.createElement("div");
        item.className = "list-item";
        item.innerHTML = `
            <div>
                <strong>${escapeHtml(cls.name)}</strong>
                <span class="text-muted" style="font-size: 0.8rem; margin-left: 8px;">(${shooterCount} Schützen)</span>
            </div>
            <button type="button" class="btn-danger-sm" onclick="deleteClass(${cls.id})" title="Klasse löschen">🗑️</button>
        `;
        classesContainer.appendChild(item);
    });
}

async function switchActiveCompetition(compId) {
    state.activeCompetitionId = compId;
    localStorage.setItem("bgscore_active_comp", String(compId));
    renderCompetitionSelector();
    await refreshCompetitionData();
}

// =========================================================================
// QR Code / Smartphone Connection Modal
// =========================================================================
async function openQrModal() {
    document.getElementById("qrModal").classList.remove("hidden");
    try {
        const res = await fetch("/api/network-info");
        const data = await res.json();
        document.getElementById("lanUrlText").textContent = data.preferred_url || data.url;
        const qrContainer = document.getElementById("qrImageContainer");
        if (data.qr_code) {
            qrContainer.innerHTML = `<img src="${data.qr_code}" alt="QR Code">`;
        } else {
            qrContainer.innerHTML = "";
        }
    } catch (err) {
        document.getElementById("lanUrlText").textContent = window.location.origin;
    }
}

function closeQrModal() {
    document.getElementById("qrModal").classList.add("hidden");
}

// =========================================================================
// Pre-filled Scorecard PDF Generator Modal (Gesamt, Klasse, Verein, Gruppe)
// =========================================================================
let currentPdfGroupMode = "all";
let currentPdfGroupsData = {
    total_shooters: 0,
    by_class: [],
    by_club: [],
    by_group: [],
};

async function openScorecardPdfModal() {
    if (!state.activeCompetitionId) {
        showToast("Bitte zuerst einen Wettkampf auswählen.", "error");
        return;
    }
    document.getElementById("scorecardPdfModal").classList.remove("hidden");
    try {
        const res = await fetch(`/api/competitions/${state.activeCompetitionId}/scorecards-groups`);
        if (res.ok) {
            currentPdfGroupsData = await res.json();
        }
    } catch (err) {
        // Fallback to local state if needed
    }
    selectPdfGroupMode(currentPdfGroupMode);
}

function closeScorecardPdfModal() {
    document.getElementById("scorecardPdfModal").classList.add("hidden");
}

function selectPdfGroupMode(mode) {
    currentPdfGroupMode = mode;
    ["all", "class", "club", "group"].forEach((m) => {
        const btn = document.getElementById(`pdfModeBtn-${m}`);
        if (btn) btn.classList.toggle("active", m === mode);
    });
    renderPdfPreviewList();
}

function _getPdfOptionsQuery() {
    const half = document.getElementById("pdfHalfSelect") ? document.getElementById("pdfHalfSelect").value : "both";
    const dist = document.getElementById("pdfDistanceSelect") ? document.getElementById("pdfDistanceSelect").value : "";
    const params = new URLSearchParams({ half });
    if (dist) params.set("distance", dist);
    return params;
}

function renderPdfPreviewList() {
    const container = document.getElementById("pdfPreviewListContainer");
    const mainBtn = document.getElementById("btnDownloadAllPdfs");
    const zipBtn = document.getElementById("btnDownloadZipPdfs");
    if (!container || !mainBtn || !zipBtn) return;

    container.innerHTML = "";
    const totalShooters = currentPdfGroupsData.total_shooters || state.shooters.length;

    if (totalShooters === 0) {
        container.innerHTML = `
            <div class="pdf-preview-empty">
                Noch keine Schützen im aktiven Wettkampf angelegt. Du kannst trotzdem einen vorbefüllten Wettkampf-Zettel oder die Leer-Vorlage herunterladen.
            </div>
        `;
        mainBtn.textContent = "📥 Wettkampf-Schießzettel (PDF) herunterladen";
        zipBtn.classList.add("hidden");
        return;
    }

    if (currentPdfGroupMode === "all") {
        const item = document.createElement("div");
        item.className = "pdf-preview-item";
        item.innerHTML = `
            <div>
                <strong>Gesamt-PDF (Alle Schützen)</strong>
                <span class="text-muted" style="font-size: 0.82rem; margin-left: 6px;">(${totalShooters} Schützen)</span>
            </div>
            <button type="button" class="btn btn-outline btn-sm" onclick="downloadSingleScorecardPdf('all', '')">
                📄 PDF
            </button>
        `;
        container.appendChild(item);
        mainBtn.textContent = `📥 Gesamt-PDF für alle ${totalShooters} Schützen herunterladen`;
        zipBtn.classList.add("hidden");
        return;
    }

    let items = [];
    let modeLabel = "Klasse";
    let modePlural = "Klassen-PDFs";
    if (currentPdfGroupMode === "class") {
        items = currentPdfGroupsData.by_class || [];
        modeLabel = "Klasse";
        modePlural = "Klassen-PDFs";
    } else if (currentPdfGroupMode === "club") {
        items = currentPdfGroupsData.by_club || [];
        modeLabel = "Verein";
        modePlural = "Vereins-PDFs";
    } else if (currentPdfGroupMode === "group") {
        items = currentPdfGroupsData.by_group || [];
        modeLabel = "Gruppe";
        modePlural = "Gruppen-PDFs";
    }

    items.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "pdf-preview-item";
        row.innerHTML = `
            <div>
                <strong>${escapeHtml(modeLabel)}: ${escapeHtml(entry.label)}</strong>
                <span class="text-muted" style="font-size: 0.82rem; margin-left: 6px;">(${entry.count} Schützen)</span>
            </div>
            <button type="button" class="btn btn-outline btn-sm" data-pdf-key="${escapeHtml(entry.key)}">
                📄 PDF herunterladen
            </button>
        `;
        const btn = row.querySelector("button");
        btn.onclick = () => downloadSingleScorecardPdf(currentPdfGroupMode, entry.key);
        container.appendChild(row);
    });

    mainBtn.textContent = `📥 Alle ${items.length} ${modePlural} einzeln herunterladen (.pdf)`;
    zipBtn.textContent = `📦 Alle ${items.length} ${modePlural} gebündelt als ZIP (.zip) herunterladen`;
    zipBtn.classList.toggle("hidden", items.length <= 1);
}

function _triggerBrowserDownload(url) {
    const a = document.createElement("a");
    a.href = url;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 2000);
}

function downloadSingleScorecardPdf(mode, value) {
    if (!state.activeCompetitionId) return;
    const params = _getPdfOptionsQuery();
    params.set("mode", mode);
    if (value !== undefined && value !== null && value !== "") {
        params.set("value", value);
    }
    const url = `/api/competitions/${state.activeCompetitionId}/scorecards.pdf?${params.toString()}`;
    _triggerBrowserDownload(url);
}

function downloadShooterScorecardPdf(shooterId) {
    if (!state.activeCompetitionId) return;
    const params = new URLSearchParams({
        mode: "shooter",
        value: String(shooterId),
        half: "both",
    });
    const url = `/api/competitions/${state.activeCompetitionId}/scorecards.pdf?${params.toString()}`;
    _triggerBrowserDownload(url);
}

function triggerPrimaryPdfDownload() {
    if (!state.activeCompetitionId) return;
    if (currentPdfGroupMode === "all") {
        downloadSingleScorecardPdf("all", "");
        showToast("Gesamt-PDF mit vorbefüllten Schießzetteln wird heruntergeladen…", "success");
        return;
    }

    let items = [];
    if (currentPdfGroupMode === "class") items = currentPdfGroupsData.by_class || [];
    else if (currentPdfGroupMode === "club") items = currentPdfGroupsData.by_club || [];
    else if (currentPdfGroupMode === "group") items = currentPdfGroupsData.by_group || [];

    if (items.length === 0) {
        downloadSingleScorecardPdf("all", "");
        return;
    }

    items.forEach((entry, idx) => {
        setTimeout(() => {
            downloadSingleScorecardPdf(currentPdfGroupMode, entry.key);
        }, idx * 380);
    });
    showToast(`${items.length} PDF-Datei(en) werden erstellt und heruntergeladen…`, "success");
}

function downloadScorecardsZip() {
    if (!state.activeCompetitionId) return;
    const params = _getPdfOptionsQuery();
    params.set("mode", currentPdfGroupMode);
    const url = `/api/competitions/${state.activeCompetitionId}/scorecards.zip?${params.toString()}`;
    _triggerBrowserDownload(url);
    showToast("ZIP-Archiv mit allen PDFs wird heruntergeladen…", "success");
}

function escapeHtml(str) {
    return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}


// =========================================================================
// REAL-TIME AUTO-UPDATE & LIVE POLLING FOR RESULTS LIST
// =========================================================================

function computeShootersSignature(shooters) {
    if (!shooters || !Array.isArray(shooters)) return "";
    return shooters
        .map((s) => `${s.id}:${s.total_sum}:${s.half1_sum}:${s.half2_sum}:${s.tens_count}:${s.nines_count}:${s.rank_overall}:${s.rank_in_class}`)
        .join("|");
}

function updateLiveIndicatorUi() {
    const pill = document.getElementById("liveStatusPill");
    const label = document.getElementById("liveStatusLabel");
    const timeEl = document.getElementById("liveLastUpdatedTime");
    const toggleBtn = document.getElementById("btnToggleAutoRefresh");

    if (!pill || !label || !timeEl) return;

    if (isAutoRefreshPaused) {
        pill.classList.add("paused");
        label.innerHTML = "Live: <strong>Pausiert</strong>";
        if (toggleBtn) toggleBtn.innerHTML = "▶️ Fortsetzen";
    } else {
        pill.classList.remove("paused");
        label.innerHTML = "Live: <strong>Aktiv (alle 3s)</strong>";
        if (toggleBtn) toggleBtn.innerHTML = "⏸️ Pause";
    }

    if (lastRefreshTime) {
        const timeStr = lastRefreshTime.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        timeEl.textContent = `· ${timeStr}`;
    }
}

function startAutoRefreshLoop() {
    if (resultsAutoRefreshTimer) clearInterval(resultsAutoRefreshTimer);

    resultsAutoRefreshTimer = setInterval(async () => {
        if (document.hidden) return;
        if (isAutoRefreshPaused) return;

        if (state.activeTab === "results") {
            await pollResultsUpdate(false);
        } else {
            pollCounter = (pollCounter + 1) % 4;
            if (pollCounter === 0) {
                await pollResultsUpdate(true);
            }
        }
    }, 3000);
}

async function pollResultsUpdate(isBackground = false) {
    const comp = getActiveCompetition();
    if (!comp) return;

    try {
        const res = await fetch(`/api/competitions/${comp.id}/shooters`, {
            cache: "no-store",
            headers: { "Cache-Control": "no-cache" }
        });
        if (!res.ok) return;
        const freshShooters = await res.json();

        const newSig = computeShootersSignature(freshShooters);
        const hasChanged = newSig !== lastResultsSignature;

        if (hasChanged || !lastResultsSignature) {
            if (state.shooters && state.shooters.length > 0) {
                const oldMap = new Map(state.shooters.map((s) => [s.id, s]));
                freshShooters.forEach((s) => {
                    const old = oldMap.get(s.id);
                    if (
                        !old ||
                        old.total_sum !== s.total_sum ||
                        old.half1_sum !== s.half1_sum ||
                        old.half2_sum !== s.half2_sum
                    ) {
                        recentlyUpdatedShooterIds.add(s.id);
                        setTimeout(() => {
                            recentlyUpdatedShooterIds.delete(s.id);
                        }, 4000);
                    }
                });
            }

            state.shooters = freshShooters;
            lastResultsSignature = newSig;
            document.getElementById("navShooterCount").textContent = state.shooters.length;

            if (state.activeTab === "results") {
                renderResultsTable();
            } else if (!isBackground) {
                populateScanShooterDropdown();
                renderManageShootersTable();
            }
        }

        lastRefreshTime = new Date();
        updateLiveIndicatorUi();
    } catch (err) {
        console.warn("Auto-Update Polling Fehler:", err);
    }
}

async function manualRefreshResults() {
    const btn = document.getElementById("btnManualRefreshResults");
    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Lädt...";
    }
    try {
        await refreshCompetitionData();
        showToast("Ergebnisliste erfolgreich aktualisiert ✓", "success");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = "🔄 Aktualisieren";
        }
    }
}

function toggleAutoRefreshResults() {
    isAutoRefreshPaused = !isAutoRefreshPaused;
    updateLiveIndicatorUi();
    if (!isAutoRefreshPaused) {
        pollResultsUpdate(false);
    }
}

function toggleAutoSaveOnScan(enabled) {
    localStorage.setItem("bgscore_auto_save_scan", enabled ? "1" : "0");
    showToast(
        enabled
            ? "⚡ Auto-Speichern aktiv: Erkannte Schießzettel werden sofort gespeichert & die Ergebnisliste aktualisiert."
            : "Auto-Speichern deaktiviert (Klick auf 'Speichern' erforderlich).",
        "info"
    );
}

function resetScanForNextSheet() {
    state.evaluatedPasses = [];
    state.passeCrops = [];
    state.lastQrData = null;
    const photoEl = document.getElementById("viewfinderPhotoPreview");
    if (photoEl) {
        photoEl.src = "";
        photoEl.classList.add("hidden");
    }
    const idleEl = document.getElementById("viewfinderIdleBg");
    if (idleEl) idleEl.classList.remove("hidden");
    const previewDetails = document.getElementById("photoPreviewDetails");
    if (previewDetails) previewDetails.classList.add("hidden");
    const confBadge = document.getElementById("evalConfidenceBadge");
    if (confBadge) confBadge.classList.add("hidden");
    const emptyState = document.getElementById("evalEmptyState");
    if (emptyState) emptyState.classList.remove("hidden");
    const activeContent = document.getElementById("evalActiveContent");
    if (activeContent) activeContent.classList.add("hidden");
    const banner = document.getElementById("saveSuccessBanner");
    if (banner) banner.classList.add("hidden");
    renderQrDetectedBanner(null, null);
    if (window.innerWidth < 900) {
        const scanSec = document.getElementById("tab-scan");
        if (scanSec) scanSec.scrollIntoView({ behavior: "smooth" });
    }
}

// =========================================================================
// TOURNAMENT JSON IMPORT MODAL & LOGIC
// =========================================================================
let parsedImportData = null;

function initImportDropZone() {
    const dropZone = document.getElementById("importDropZone");
    if (!dropZone) return;

    dropZone.addEventListener("click", () => {
        const input = document.getElementById("importFileInput");
        if (input) input.click();
    });

    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });

    dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("dragover");
    });

    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            processImportFile(e.dataTransfer.files[0]);
        }
    });
}

function openImportModal() {
    const input = document.getElementById("importFileInput");
    if (input) input.value = "";
    parsedImportData = null;

    const previewContainer = document.getElementById("importPreviewContainer");
    if (previewContainer) previewContainer.classList.add("hidden");

    const errorContainer = document.getElementById("importErrorContainer");
    if (errorContainer) {
        errorContainer.classList.add("hidden");
        errorContainer.textContent = "";
    }

    const btnExec = document.getElementById("btnExecuteImport");
    if (btnExec) {
        btnExec.disabled = true;
        btnExec.textContent = "📥 Jetzt importieren";
    }

    const modal = document.getElementById("importModal");
    if (modal) modal.classList.remove("hidden");
}

function closeImportModal() {
    const modal = document.getElementById("importModal");
    if (modal) modal.classList.add("hidden");
    parsedImportData = null;
}

function handleImportFileSelect(event) {
    if (event.target.files && event.target.files.length > 0) {
        processImportFile(event.target.files[0]);
    }
}

async function processImportFile(file) {
    const errorContainer = document.getElementById("importErrorContainer");
    const previewContainer = document.getElementById("importPreviewContainer");
    const btnExec = document.getElementById("btnExecuteImport");

    if (!file) return;

    try {
        const text = await file.text();
        let data;
        try {
            data = JSON.parse(text);
        } catch (err) {
            showImportError(`Die Datei "${escapeHtml(file.name)}" enthält kein gültiges JSON: ${err.message}`);
            return;
        }

        if (typeof data !== "object" || data === null) {
            showImportError("Die Datei enthält kein gültiges JSON-Objekt.");
            return;
        }

        const turniername = (data.turniername || data.name || data.wettkampf || data.competition || "").trim();
        if (!turniername) {
            showImportError("In der JSON-Datei fehlt das Feld 'turniername'.");
            return;
        }

        const rawShooters = data.schuetzen || data.shooters;
        if (!Array.isArray(rawShooters)) {
            showImportError("In der JSON-Datei fehlt das Feld 'schuetzen' (Liste aller Schützen).");
            return;
        }

        const rawClasses = Array.isArray(data.klassen)
            ? data.klassen
            : (Array.isArray(data.classes) ? data.classes : []);

        const classesSet = new Set(
            rawClasses
                .map((k) => (typeof k === "string" ? k.trim() : (k.name || "").trim()))
                .filter(Boolean)
        );

        // Also add classes mentioned in shooters
        rawShooters.forEach((s) => {
            if (s && typeof s === "object") {
                const k = (s.klasse || s.class || s.class_name || "").trim();
                if (k) classesSet.add(k);
            }
        });

        const classesList = Array.from(classesSet);
        if (classesList.length === 0) {
            classesList.push("Allgemeine Klasse");
        }

        // Fill Preview
        document.getElementById("importPreviewName").textContent = turniername;
        document.getElementById("importPreviewDate").textContent = data.datum || data.date || "Heute";
        document.getElementById("importPreviewClassesCount").textContent = String(classesList.length);
        document.getElementById("importPreviewShootersCount").textContent = String(rawShooters.length);
        document.getElementById("importPreviewShootersCountBadge").textContent = String(rawShooters.length);

        const chipsList = document.getElementById("importPreviewClassesList");
        chipsList.innerHTML = "";
        classesList.forEach((cls) => {
            const chip = document.createElement("span");
            chip.className = "preview-chip";
            chip.textContent = cls;
            chipsList.appendChild(chip);
        });

        const tbody = document.getElementById("importPreviewShootersTbody");
        tbody.innerHTML = "";
        rawShooters.forEach((s, idx) => {
            if (!s || typeof s !== "object") return;
            const lastName = escapeHtml(s.name || s.last_name || s.nachname || "–");
            const firstName = escapeHtml(s.vorname || s.first_name || "–");
            const klasse = escapeHtml(s.klasse || s.class || s.class_name || "–");
            const verein = escapeHtml(s.verein || s.club || "–");
            const gruppe = escapeHtml(String(s.gruppe !== undefined ? s.gruppe : (s.group || s.group_name || "–")));
            const scheibe = escapeHtml(String(s.scheibe !== undefined ? s.scheibe : (s.stand || s.stand_number || "–")));

            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td>${idx + 1}</td>
                <td><strong>${lastName}</strong>, ${firstName}</td>
                <td><span class="preview-chip">${klasse}</span></td>
                <td>${verein}</td>
                <td>${gruppe}</td>
                <td>${scheibe}</td>
            `;
            tbody.appendChild(tr);
        });

        parsedImportData = data;
        if (errorContainer) {
            errorContainer.classList.add("hidden");
            errorContainer.textContent = "";
        }
        if (previewContainer) previewContainer.classList.remove("hidden");
        if (btnExec) btnExec.disabled = false;
    } catch (err) {
        showImportError(`Fehler beim Lesen der Datei: ${err.message}`);
    }
}

function showImportError(message) {
    const errorContainer = document.getElementById("importErrorContainer");
    const previewContainer = document.getElementById("importPreviewContainer");
    const btnExec = document.getElementById("btnExecuteImport");

    if (previewContainer) previewContainer.classList.add("hidden");
    if (btnExec) btnExec.disabled = true;

    if (errorContainer) {
        errorContainer.textContent = message;
        errorContainer.classList.remove("hidden");
    }
    parsedImportData = null;
}

async function executeImport() {
    if (!parsedImportData) return;

    const btnExec = document.getElementById("btnExecuteImport");
    if (btnExec) {
        btnExec.disabled = true;
        btnExec.textContent = "⏳ Wird importiert…";
    }

    try {
        const res = await fetch("/api/competitions/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(parsedImportData),
        });

        const result = await res.json();
        if (!res.ok) {
            showImportError(result.error || "Fehler beim Importieren des Turniers.");
            if (btnExec) {
                btnExec.disabled = false;
                btnExec.textContent = "📥 Jetzt importieren";
            }
            return;
        }

        closeImportModal();
        showToast(result.message || "Turnier erfolgreich importiert!", "success");

        // Automatically activate imported competition
        localStorage.setItem("bgscore_active_comp", String(result.id));
        await loadCompetitions();
        await switchTab("shooters");
    } catch (err) {
        showImportError(`Netzwerkfehler beim Importieren: ${err.message}`);
        if (btnExec) {
            btnExec.disabled = false;
            btnExec.textContent = "📥 Jetzt importieren";
        }
    }
}
