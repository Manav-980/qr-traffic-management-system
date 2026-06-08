import os
import re
import csv
import base64
import sqlite3
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_file,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import qrcode
from PIL import Image, ImageDraw, ImageFont

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =========================================================
# PATH CONFIG
# =========================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "parking.db")

QR_DIR = os.path.join(BASE_DIR, "static", "qr_codes")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

for folder in [QR_DIR, UPLOAD_DIR, EXPORT_DIR]:
    os.makedirs(folder, exist_ok=True)


# =========================================================
# FLASK CONFIG
# =========================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "qr-parking-system-secure-fallback")
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB limit


# =========================================================
# DATABASE LAYER
# =========================================================

def get_db():
    """Returns a thread-safe connection and enforces WAL mode & Foreign Keys."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Performance & Reliability Tuning
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def now_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_no TEXT UNIQUE NOT NULL,
                owner_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                vehicle_type TEXT NOT NULL,
                address TEXT,
                qr_filename TEXT,
                created_by TEXT NOT NULL,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_vehicles_no ON vehicles(vehicle_no);
            CREATE INDEX IF NOT EXISTS idx_vehicles_user ON vehicles(user_id);
            """
        )

        admin = conn.execute("SELECT * FROM admins WHERE email=?", ("admin@gmail.com",)).fetchone()
        if not admin:
            # Modern, explicit scrypt password hashing
            hashed_pw = generate_password_hash("admin123", method="scrypt")
            conn.execute(
                "INSERT INTO admins(name, email, password_hash, created_at) VALUES(?,?,?,?)",
                ("Admin", "admin@gmail.com", hashed_pw, now_time()),
            )
        conn.commit()


# =========================================================
# GLOBALLY INITIALIZE OCR ENGINE (Prevents multi-second delays per request)
# =========================================================

try:
    import cv2
    import easyocr
    import numpy as np
    # Initialize reader once globally. Disabling GPU explicitly if running on CPU setups.
    ocr_reader = easyocr.Reader(["en"], gpu=os.environ.get("USE_GPU", "False").lower() == "true")
    OCR_AVAILABLE = True
except ImportError as e:
    logger.error(f"OCR dependency missing: {e}. Falling back to manual inputs.")
    OCR_AVAILABLE = False


# =========================================================
# VEHICLE NUMBER PROCESSSING & FUZZY MATCHING
# =========================================================

STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "DN", "GA", "GJ",
    "HR", "HP", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP",
    "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UK",
    "UP", "WB"
}


def normalize_vehicle_no(text):
    if not text:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper().strip())


def correct_plate_by_position(text):
    s = normalize_vehicle_no(text)
    for word in ["IND", "INDIA", "KIA", "HONDA", "GOOGLE", "LENS", "CAMERA", "BACK", "NUMBER", "PLATE"]:
        s = s.replace(word, "")

    if len(s) < 6:
        return s

    letter_from_digit = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}
    digit_from_letter = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "T": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}

    chars = list(s)
    # Positions 0-1: Letters
    for i in range(min(2, len(chars))):
        if chars[i].isdigit():
            chars[i] = letter_from_digit.get(chars[i], chars[i])
    # Positions 2-3: Digits
    for i in range(2, min(4, len(chars))):
        if chars[i].isalpha():
            chars[i] = digit_from_letter.get(chars[i], chars[i])
    # Last 4 characters: Digits
    if len(chars) >= 8:
        for i in range(max(4, len(chars) - 4), len(chars)):
            if chars[i].isalpha():
                chars[i] = digit_from_letter.get(chars[i], chars[i])

    return "".join(chars)


def extract_candidates_from_text(text):
    raw = normalize_vehicle_no(text)
    corrected = correct_plate_by_position(raw)
    possible_texts = {raw, corrected}

    for item in list(possible_texts):
        possible_texts.add(item.replace("IND", "").replace("INDIA", ""))

    patterns = [
        r"[A-Z]{2}[0-9]{2}[A-Z]{1,3}[0-9]{4}",
        r"[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}",
        r"[A-Z]{2}[0-9]{2}[A-Z]{1,4}[0-9]{3,4}",
    ]

    candidates = []
    for t in possible_texts:
        for pattern in patterns:
            for match in re.finditer(pattern, t):
                candidate = correct_plate_by_position(match.group())
                if 8 <= len(candidate) <= 11:
                    candidates.append(candidate)

    if not candidates and 8 <= len(corrected) <= 12:
        candidates.append(corrected)

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda x: (x[:2] not in STATE_CODES, abs(len(x) - 10), len(x)))
    return candidates


def levenshtein_distance(a, b):
    a, b = normalize_vehicle_no(a), normalize_vehicle_no(b)
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace = previous[j - 1] + (ca != cb)
            current.append(min(insert, delete, replace))
        previous = current
    return previous[-1]


def find_best_vehicle_match(candidate):
    candidate = normalize_vehicle_no(candidate)
    if not candidate:
        return None, "", "none"

    with get_db() as conn:
        exact = conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (candidate,)).fetchone()
        if exact:
            return exact, candidate, "exact"
        vehicles = conn.execute("SELECT * FROM vehicles").fetchall()

    best_vehicle, best_distance, best_no = None, 999, candidate

    for vehicle in vehicles:
        db_no = normalize_vehicle_no(vehicle["vehicle_no"])
        dist = levenshtein_distance(candidate, db_no)

        if len(candidate) >= 4 and len(db_no) >= 4 and candidate[:2] == db_no[:2]:
            dist -= 1  # State match weight bonus
        if len(candidate) >= 2 and len(db_no) >= 2 and candidate[-2:] == db_no[-2:]:
            dist -= 1  # Unique identifier tail match bonus

        if dist < best_distance:
            best_distance = dist
            best_vehicle = vehicle
            best_no = db_no

    threshold = 2 if len(candidate) <= 10 else 3
    if best_vehicle and best_distance <= threshold:
        return best_vehicle, best_no, "fuzzy"

    return None, candidate, "none"


# =========================================================
# REFACTORED OCR PROCESSING ENGINE
# =========================================================

import requests

def extract_plate_text(image_path):
    """
    Lightweight cloud OCR implementation using the provided API key.
    Sends the image data directly to external endpoints, bypassing the 512MB RAM ceiling entirely.
    """
    try:
        import requests
        
        # Define API payload settings with your active key
        payload = {
            'apikey': 'K86971480888957',  # Your active free API key
            'language': 'eng',
            'isOverlayRequired': False,
            'OCREngine': '2'  # Engine 2 is optimized specifically for alphanumeric strings like license plates
        }
        
        # Stream the captured photo file directly over HTTPS
        with open(image_path, 'rb') as f:
            response = requests.post(
                'https://api.ocr.space/parse/image', 
                files={'image': f}, 
                data=payload,
                timeout=15
            )
            
        result = response.json()
        
        # Check if the API successfully recognized string characters
        if result.get("ParsedResults"):
            detected_text = result["ParsedResults"][0].get("ParsedText", "")
            logger.info(f"[API DEBUG] Raw Cloud OCR Output: {detected_text}")
            
            # Use your built-in high-accuracy positional candidates regex tracker
            candidates = extract_candidates_from_text(detected_text)
            logger.info(f"[API DEBUG] Extracted Candidates: {candidates}")
            
            # Look up accumulated candidates inside your local SQLite registration database
            for candidate in candidates:
                vehicle, matched_no, mode = find_best_vehicle_match(candidate)
                if vehicle:
                    logger.info(f"[API DEBUG] Database Match Located: {matched_no} ({mode})")
                    return matched_no
            
            # If no exact or fuzzy database hit occurs, return the primary candidate string directly
            return candidates[0] if candidates else ""
            
        return ""
    except Exception as e:
        logger.error(f"[API ERROR] Cloud transmission failure: {e}")
        # Fall back to file title extraction metric if web data fails
        fallback = extract_candidates_from_text(os.path.basename(image_path))
        return fallback[0] if fallback else ""

# =========================================================
# QR BADGE GENERATOR
# =========================================================

def generate_qr_badge(vehicle):
    vehicle_no = vehicle["vehicle_no"]
    vehicle_id = vehicle["id"]
    scan_url = url_for("vehicle_public", vehicle_no=vehicle_no, _external=True)

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(scan_url)
    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((300, 300))
    badge = Image.new("RGB", (520, 720), "white")
    draw = ImageDraw.Draw(badge)

    try:
        title_font = ImageFont.truetype("arialbd.ttf", 30)
        text_font = ImageFont.truetype("arial.ttf", 22)
        small_font = ImageFont.truetype("arial.ttf", 17)
    except Exception:
        title_font = text_font = small_font = None  # System font fallback

    draw.rectangle((0, 0, 520, 720), outline=(14, 165, 233), width=8)
    draw.rectangle((0, 0, 520, 100), fill=(14, 165, 233))
    draw.text((58, 30), "QR PARKING SYSTEM", fill="white", font=title_font)

    badge.paste(qr_img, (110, 145))
    draw.rounded_rectangle((55, 485, 465, 560), radius=18, fill=(240, 249, 255), outline=(14, 165, 233), width=3)
    draw.text((88, 508), f"VEHICLE NO: {vehicle_no}", fill=(15, 23, 42), font=text_font)
    draw.text((115, 590), "Scan QR to view vehicle details", fill=(71, 85, 105), font=small_font)
    draw.text((112, 630), "Smart Parking Verification", fill=(71, 85, 105), font=small_font)

    filename = f"vehicle_{vehicle_id}_{vehicle_no}.png"
    badge.save(os.path.join(QR_DIR, filename))
    return filename


# =========================================================
# MIDDLEWARE/DECORATORS
# =========================================================

def login_required(role):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if role == "admin" and not session.get("admin_id"):
                flash("Please log in as an administrator.", "warning")
                return redirect(url_for("admin_login"))
            if role == "user" and not session.get("user_id"):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("user_login"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# =========================================================
# CORE APP CONTROLLER ROUTES
# =========================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with get_db() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE email=?", (email,)).fetchone()

        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_id"] = admin["id"]
            session["admin_name"] = admin["name"]
            flash("Admin session initialized.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid administrative credentials.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    q = request.args.get("q", "").strip()
    with get_db() as conn:
        if q:
            like = f"%{q}%"
            vehicles = conn.execute(
                "SELECT * FROM vehicles WHERE vehicle_no LIKE ? OR owner_name LIKE ? OR phone LIKE ? ORDER BY id DESC",
                (like, like, like)
            ).fetchall()
        else:
            vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id DESC").fetchall()

        stats = {
            "total_users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
            "total_vehicles": conn.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"], # <-- Renamed key
            "admin_added": conn.execute("SELECT COUNT(*) c FROM vehicles WHERE created_by='admin'").fetchone()["c"],
            "user_added": conn.execute("SELECT COUNT(*) c FROM vehicles WHERE created_by='user'").fetchone()["c"]
        }

    return render_template("admin_dashboard.html", vehicles=vehicles, q=q, **stats)

@app.route("/admin/users")
@login_required("admin")
def admin_users():
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/add-vehicle", methods=["GET", "POST"])
@login_required("admin")
def admin_add_vehicle():
    if request.method == "POST":
        return save_vehicle("admin", None, "admin_dashboard")
    return render_template("vehicle_form.html", title="Register Vehicle", vehicle=None, action=url_for("admin_add_vehicle"))


@app.route("/admin/edit-vehicle/<int:vehicle_id>", methods=["GET", "POST"])
@login_required("admin")
def admin_edit_vehicle(vehicle_id):
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()

    if not vehicle:
        flash("Vehicle record not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        return update_vehicle(vehicle_id, "admin_dashboard")
    return render_template("vehicle_form.html", title="Modify Vehicle Data", vehicle=vehicle, action=url_for("admin_edit_vehicle", vehicle_id=vehicle_id))



@app.route("/admin/delete-vehicle/<int:vehicle_id>")
@login_required("admin")
def admin_delete_vehicle(vehicle_id):
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if vehicle and vehicle["qr_filename"]:
            try:
                os.remove(os.path.join(QR_DIR, vehicle["qr_filename"]))
            except OSError:
                pass
        conn.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))
        conn.commit()

    flash("Vehicle deletion completed.", "success")
    return redirect(url_for("admin_dashboard"))


# =========================================================
# END-USER INTERFACES
# =========================================================

@app.route("/user/register", methods=["GET", "POST"])
def user_register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All profile parameters are required.", "danger")
            return redirect(url_for("user_register"))

        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users(name, email, password_hash, created_at) VALUES(?,?,?,?)",
                    (name, email, generate_password_hash(password, method="scrypt"), now_time()),
                )
                conn.commit()
            flash("Account provisioned. Please authenticate.", "success")
            return redirect(url_for("user_login"))
        except sqlite3.IntegrityError:
            flash("Email domain already registered.", "danger")

    return render_template("user_register.html")



@app.route("/user/login", methods=["GET", "POST"])
def user_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Authentication successful.", "success")
            return redirect(url_for("user_dashboard"))

        flash("Invalid account email or password.", "danger")
    return render_template("user_login.html")


@app.route("/user/dashboard")
@login_required("user")
def user_dashboard():
    with get_db() as conn:
        vehicles = conn.execute("SELECT * FROM vehicles WHERE user_id=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    return render_template("user_dashboard.html", vehicles=vehicles)


@app.route("/user/add-vehicle", methods=["GET", "POST"])
@login_required("user")
def user_add_vehicle():
    if request.method == "POST":
        return save_vehicle("user", session["user_id"], "user_dashboard")
    return render_template("vehicle_form.html", title="Register Asset", vehicle=None, action=url_for("user_add_vehicle"))



@app.route("/user/edit-vehicle/<int:vehicle_id>", methods=["GET", "POST"])
@login_required("user")
def user_edit_vehicle(vehicle_id):
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, session["user_id"])).fetchone()

    if not vehicle:
        flash("Unauthorized or missing asset file.", "danger")
        return redirect(url_for("user_dashboard"))

    if request.method == "POST":
        return update_vehicle(vehicle_id, "user_dashboard", session["user_id"])
    return render_template("vehicle_form.html", title="Update Asset Info", vehicle=vehicle, action=url_for("user_edit_vehicle", vehicle_id=vehicle_id))


@app.route("/user/delete-vehicle/<int:vehicle_id>")
@login_required("user")
def user_delete_vehicle(vehicle_id):
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, session["user_id"])).fetchone()
        if vehicle and vehicle["qr_filename"]:
            try:
                os.remove(os.path.join(QR_DIR, vehicle["qr_filename"]))
            except OSError:
                pass
        conn.execute("DELETE FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, session["user_id"]))
        conn.commit()

    flash("Asset profile cleanly expunged.", "success")
    return redirect(url_for("user_dashboard"))


# =========================================================
# PERSISTENCE DATA LOADER MUTATIONS
# =========================================================

def save_vehicle(created_by, user_id, redirect_endpoint):
    vehicle_no = normalize_vehicle_no(request.form.get("vehicle_no", ""))
    owner_name = request.form.get("owner_name", "").strip()
    phone = request.form.get("phone", "").strip()
    vehicle_type = request.form.get("vehicle_type", "").strip()
    address = request.form.get("address", "").strip()

    if not vehicle_no or not owner_name or not phone or not vehicle_type:
        flash("Mandatory parameters missing structural data.", "danger")
        return redirect(request.referrer or url_for(redirect_endpoint))

    try:
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO vehicles(vehicle_no, owner_name, phone, vehicle_type, address, created_by, user_id, created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (vehicle_no, owner_name, phone, vehicle_type, address, created_by, user_id, now_time()),
            )
            vehicle_id = cur.lastrowid
            vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            
            qr_filename = generate_qr_badge(vehicle)
            conn.execute("UPDATE vehicles SET qr_filename=? WHERE id=?", (qr_filename, vehicle_id))
            conn.commit()

        flash("Asset logged and custom verification token prepared.", "success")
    except sqlite3.IntegrityError:
        flash("Vehicle registration number identifier collision.", "danger")

    return redirect(url_for(redirect_endpoint))


def update_vehicle(vehicle_id, redirect_endpoint, user_id=None):
    vehicle_no = normalize_vehicle_no(request.form.get("vehicle_no", ""))
    owner_name = request.form.get("owner_name", "").strip()
    phone = request.form.get("phone", "").strip()
    vehicle_type = request.form.get("vehicle_type", "").strip()
    address = request.form.get("address", "").strip()

    if not vehicle_no or not owner_name or not phone or not vehicle_type:
        flash("Validation parameters failed schema validation.", "danger")
        return redirect(request.referrer or url_for(redirect_endpoint))

    try:
        with get_db() as conn:
            if user_id:
                conn.execute(
                    "UPDATE vehicles SET vehicle_no=?, owner_name=?, phone=?, vehicle_type=?, address=? WHERE id=? AND user_id=?",
                    (vehicle_no, owner_name, phone, vehicle_type, address, vehicle_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE vehicles SET vehicle_no=?, owner_name=?, phone=?, vehicle_type=?, address=? WHERE id=?",
                    (vehicle_no, owner_name, phone, vehicle_type, address, vehicle_id),
                )

            vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if vehicle:
                # Cleanup old file if renaming
                if vehicle["qr_filename"]:
                    try:
                        os.remove(os.path.join(QR_DIR, vehicle["qr_filename"]))
                    except OSError:
                        pass
                qr_filename = generate_qr_badge(vehicle)
                conn.execute("UPDATE vehicles SET qr_filename=? WHERE id=?", (qr_filename, vehicle_id))
            conn.commit()

        flash("Asset structural update committed successfully.", "success")
    except sqlite3.IntegrityError:
        flash("Identifier transformation conflicted with registered vehicle.", "danger")

    return redirect(url_for(redirect_endpoint))


# =========================================================
# GATEWAY VALIDATIONS & SCANNERS
# =========================================================

@app.route("/vehicle/<vehicle_no>")
def vehicle_public(vehicle_no):
    vehicle_no = normalize_vehicle_no(vehicle_no)
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (vehicle_no,)).fetchone()
    return render_template("vehicle_public.html", vehicle=vehicle, vehicle_no=vehicle_no)


@app.route("/download-qr/<int:vehicle_id>")
def download_qr(vehicle_id):
    with get_db() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()

    if not vehicle:
        flash("Resource target identity unmapped.", "danger")
        return redirect(url_for("index"))

    filename = vehicle["qr_filename"]
    path = os.path.join(QR_DIR, filename if filename else "")

    if not filename or not os.path.exists(path):
        with get_db() as conn:
            filename = generate_qr_badge(vehicle)
            conn.execute("UPDATE vehicles SET qr_filename=? WHERE id=?", (filename, vehicle_id))
            conn.commit()
        path = os.path.join(QR_DIR, filename)

    return send_file(path, as_attachment=True)


@app.route("/scan-number-plate", methods=["GET", "POST"])
def scan_number_plate():
    vehicle, detected_no, matched_no, match_mode, image_url, message = None, None, None, "none", None, None

    if request.method == "POST":
        manual_no = request.form.get("manual_no", "").strip()
        image = request.files.get("plate_image")

        if manual_no:
            detected_no = normalize_vehicle_no(manual_no)
        elif image and image.filename:
            filename = secure_filename(image.filename)
            saved_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
            path = os.path.join(UPLOAD_DIR, saved_name)
            image.save(path)

            image_url = url_for("static", filename=f"uploads/{saved_name}")
            detected_no = extract_plate_text(path)
        else:
            message = "Empty context payload. Upload visual data or fallback string key."

        if detected_no:
            vehicle, matched_no, match_mode = find_best_vehicle_match(detected_no)
            if not vehicle:
                message = "Vehicle metadata missed system directory registration records."

    return render_template(
        "scan_plate.html",
        extracted_text=detected_no,
        matched_no=matched_no,
        match_mode=match_mode,
        vehicle=vehicle,
        image_url=image_url,
        message=message,
    )


@app.route("/scan-plate-camera", methods=["GET", "POST"])
def scan_plate_camera():
    vehicle, detected_no, matched_no, match_mode, image_url, message = None, None, None, "none", None, None

    if request.method == "POST":
        image_data = request.form.get("camera_image")
        if image_data and "," in image_data:
            try:
                _, encoded = image_data.split(",", 1)
                image_bytes = base64.b64decode(encoded)
                filename = f"camera_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
                path = os.path.join(UPLOAD_DIR, filename)

                with open(path, "wb") as f:
                    f.write(image_bytes)

                image_url = url_for("static", filename=f"uploads/{filename}")
                detected_no = extract_plate_text(path)

                if detected_no:
                    vehicle, matched_no, match_mode = find_best_vehicle_match(detected_no)
                    if not vehicle:
                        message = "Target registry match failed on camera validation scan."
                else:
                    message = "Image clarity analysis failed positional bounding metrics."
            except Exception as e:
                logger.error(f"Camera frame exception processing: {e}")
                message = "Media payload structural error."
        else:
            message = "Media streaming capture error buffer trace empty."

    return render_template(
        "scan_plate_camera.html",
        extracted_text=detected_no,
        matched_no=matched_no,
        match_mode=match_mode,
        vehicle=vehicle,
        image_url=image_url,
        message=message,
    )


# =========================================================
# COMPLIANCE REPORT EXPORTS
# =========================================================

@app.route("/export/vehicles")
@login_required("admin")
def export_vehicles():
    path = os.path.join(EXPORT_DIR, "vehicles_export.csv")
    with get_db() as conn:
        vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id DESC").fetchall()

    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ID", "Vehicle No", "Owner", "Phone", "Type", "Address", "Created By", "Created At"])
            for v in vehicles:
                writer.writerow([v["id"], v["vehicle_no"], v["owner_name"], v["phone"], v["vehicle_type"], v["address"], v["created_by"], v["created_at"]])
        return send_file(path, as_attachment=True)
    except IOError as e:
        logger.error(f"Failed to generate CSV export resource: {e}")
        flash("Export task error occurred.", "danger")
        return redirect(url_for("admin_dashboard"))


# =========================================================
# MIDDLEWARE EXCEPTION HANDLING BOOTSTRAPS
# =========================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    flash("Asset image configuration bounds error. Keep attachments smaller than 16MB.", "danger")
    return redirect(request.referrer or url_for("scan_plate_camera"))


@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


if __name__ == "__main__":
    init_db()
    # Explicitly handling standard WSGI loop options cleanly.
    app.run(host="127.0.0.1", port=5000, debug=True)