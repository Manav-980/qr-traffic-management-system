import os
import sqlite3
import random
from datetime import datetime
from functools import wraps

import qrcode
from PIL import Image, ImageDraw, ImageFont
import pandas as pd
from fpdf import FPDF
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file
)

# Optional Twilio support.
# If Twilio is not configured, the app uses demo OTP: 123456
try:
    from dotenv import load_dotenv
    from twilio.rest import Client
except Exception:
    load_dotenv = None
    Client = None

app = Flask(__name__)
app.secret_key = "change_this_secret_key"

if load_dotenv:
    load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
USE_TWILIO_OTP = os.getenv("USE_TWILIO_OTP", "false").lower() == "true"

DB_NAME = "traffic.db" 

QR_FOLDER = os.path.join("static", "qr_codes")
EXPORT_FOLDER = "exports"

os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)


# ---------------- DATABASE CONNECTION ----------------
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- INITIALIZE DATABASE ----------------
def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            vehicle_no TEXT UNIQUE NOT NULL,
            owner_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT,
            qr_filename TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cur.execute("SELECT * FROM admin WHERE username = ?", ("admin",))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO admin(username, password) VALUES(?, ?)",
            ("admin", "admin123")
        )

    conn.commit()
    conn.close()


init_db()


# ---------------- DECORATORS ----------------
def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "admin_id" not in session:
            flash("Please login as admin first.", "warning")
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper


def user_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("user_login"))
        return func(*args, **kwargs)
    return wrapper




# ---------------- QR TEMPLATE DESIGN ----------------
def create_qr_template(scan_url, vehicle_no, qr_filename):
    """
    Creates a poster-style QR image:
    dark background + yellow heading + QR box + vehicle number.
    """
    poster_width = 900
    poster_height = 1150

    bg_color = (32, 32, 32)
    yellow = (224, 194, 65)
    white = (255, 255, 255)
    black = (0, 0, 0)

    poster = Image.new("RGB", (poster_width, poster_height), bg_color)
    draw = ImageDraw.Draw(poster)

    try:
        title_font = ImageFont.truetype("arialbd.ttf", 72)
        small_font = ImageFont.truetype("arialbd.ttf", 34)
        tiny_font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        title_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        tiny_font = ImageFont.load_default()

    # Heading
    line1 = "SCAN TO"
    line2 = "CONTACT OWNER"

    bbox1 = draw.textbbox((0, 0), line1, font=title_font)
    bbox2 = draw.textbbox((0, 0), line2, font=title_font)

    draw.text(((poster_width - (bbox1[2] - bbox1[0])) / 2, 65), line1, fill=yellow, font=title_font)
    draw.text(((poster_width - (bbox2[2] - bbox2[0])) / 2, 145), line2, fill=yellow, font=title_font)

    # Generate QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=14,
        border=2
    )
    qr.add_data(scan_url)
    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((650, 650))

    # Outer yellow border
    outer_x = 105
    outer_y = 285
    outer_size = 690
    draw.rounded_rectangle(
        [outer_x, outer_y, outer_x + outer_size, outer_y + outer_size],
        radius=18,
        fill=yellow
    )

    # White QR area
    inner_x = 125
    inner_y = 305
    inner_size = 650
    draw.rectangle(
        [inner_x, inner_y, inner_x + inner_size, inner_y + inner_size],
        fill=white
    )

    poster.paste(qr_img, (inner_x, inner_y))

    # Vehicle number bottom
    vehicle_text = f"VEHICLE NO: {vehicle_no}"
    bbox3 = draw.textbbox((0, 0), vehicle_text, font=small_font)
    draw.text(
        ((poster_width - (bbox3[2] - bbox3[0])) / 2, 1000),
        vehicle_text,
        fill=yellow,
        font=small_font
    )

    footer = "QR TRAFFIC MANAGEMENT SYSTEM"
    bbox4 = draw.textbbox((0, 0), footer, font=tiny_font)
    draw.text(
        ((poster_width - (bbox4[2] - bbox4[0])) / 2, 1060),
        footer,
        fill=white,
        font=tiny_font
    )

    qr_path = os.path.join(QR_FOLDER, qr_filename)
    poster.save(qr_path, quality=95)

    return qr_path


# ---------------- OTP HELPERS ----------------
def generate_otp():
    if USE_TWILIO_OTP:
        return str(random.randint(100000, 999999))
    return "123456"


def send_otp(phone, otp):
    """
    Sends OTP using Twilio if USE_TWILIO_OTP=true.
    Otherwise, demo mode is used and OTP is 123456.
    """
    if not USE_TWILIO_OTP:
        return "DEMO_OTP"

    if not Client:
        raise Exception("Twilio package is not installed. Run: pip install twilio python-dotenv")

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        raise Exception("Twilio credentials are missing in .env file.")

    clean_phone = phone.strip().replace(" ", "").replace("+91", "")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    message = client.messages.create(
        body=f"Your QR Traffic System verification OTP is {otp}",
        from_=TWILIO_PHONE_NUMBER,
        to=f"+91{clean_phone}"
    )

    return message.sid


# ---------------- HOME ----------------
@app.route("/")
def home():
    return render_template("index.html")


# ---------------- USER REGISTER WITH OTP ----------------
@app.route("/user/register", methods=["GET", "POST"])
def user_register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()

        if not name or not email or not phone or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("user_register"))

        conn = get_db()
        existing_user = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()

        if existing_user:
            flash("Email already registered.", "danger")
            return redirect(url_for("user_register"))

        otp = generate_otp()

        session["pending_user"] = {
            "name": name,
            "email": email,
            "phone": phone,
            "password": password
        }
        session["user_register_otp"] = otp

        try:
            send_otp(phone, otp)

            if USE_TWILIO_OTP:
                flash("OTP sent to your mobile number.", "success")
            else:
                flash("Demo OTP mode active. Use OTP: 123456", "warning")

            return redirect(url_for("verify_user_otp"))

        except Exception as e:
            flash(f"OTP sending failed: {e}", "danger")
            return redirect(url_for("user_register"))

    return render_template("user_register.html")


# ---------------- VERIFY USER OTP ----------------
@app.route("/user/verify-otp", methods=["GET", "POST"])
def verify_user_otp():
    if "pending_user" not in session:
        flash("No pending registration found.", "danger")
        return redirect(url_for("user_register"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()

        if entered_otp != session.get("user_register_otp"):
            flash("Invalid OTP. Please try again.", "danger")
            return redirect(url_for("verify_user_otp"))

        user_data = session["pending_user"]

        conn = get_db()
        try:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO users(name, email, phone, password, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                user_data["name"],
                user_data["email"],
                user_data["phone"],
                user_data["password"],
                created_at
            ))

            conn.commit()

            session.pop("pending_user", None)
            session.pop("user_register_otp", None)

            flash("OTP verified. Registration successful. Please login.", "success")
            return redirect(url_for("user_login"))

        except sqlite3.IntegrityError:
            flash("Email already registered.", "danger")
            return redirect(url_for("user_register"))

        finally:
            conn.close()

    return render_template("verify_user_otp.html")


# ---------------- RESEND USER OTP ----------------
@app.route("/user/resend-otp")
def resend_user_otp():
    if "pending_user" not in session:
        flash("No pending registration found.", "danger")
        return redirect(url_for("user_register"))

    otp = generate_otp()
    session["user_register_otp"] = otp
    phone = session["pending_user"]["phone"]

    try:
        send_otp(phone, otp)

        if USE_TWILIO_OTP:
            flash("OTP resent successfully.", "success")
        else:
            flash("Demo OTP resent. Use OTP: 123456", "warning")

    except Exception as e:
        flash(f"OTP resend failed: {e}", "danger")

    return redirect(url_for("verify_user_otp"))


# ---------------- USER LOGIN ----------------
@app.route("/user/login", methods=["GET", "POST"])
def user_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ? AND password = ?",
            (email, password)
        ).fetchone()
        conn.close()

        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Login successful.", "success")
            return redirect(url_for("user_dashboard"))

        flash("Invalid email or password.", "danger")

    return render_template("user_login.html")


# ---------------- USER LOGOUT ----------------
@app.route("/user/logout")
def user_logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("user_login"))


# ---------------- USER DASHBOARD ----------------
@app.route("/user/dashboard")
@user_required
def user_dashboard():
    user_id = session["user_id"]

    conn = get_db()
    vehicles = conn.execute("""
        SELECT * FROM vehicles
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()
    conn.close()

    return render_template("user_dashboard.html", vehicles=vehicles)


# ---------------- USER ADD VEHICLE ----------------
@app.route("/user/add-vehicle", methods=["GET", "POST"])
@user_required
def user_add_vehicle():
    if request.method == "POST":
        vehicle_no = request.form.get("vehicle_no", "").upper().strip()
        address = request.form.get("address", "").strip()

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()

        if not vehicle_no:
            flash("Vehicle number is required.", "danger")
            conn.close()
            return redirect(url_for("user_add_vehicle"))

        try:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO vehicles(user_id, vehicle_no, owner_name, phone, address, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session["user_id"],
                vehicle_no,
                user["name"],
                user["phone"],
                address,
                created_at
            ))

            vehicle_id = cur.lastrowid
            scan_url = url_for("vehicle_details", vehicle_id=vehicle_id, _external=True)
            qr_filename = f"vehicle_{vehicle_id}.png"

            create_qr_template(scan_url, vehicle_no, qr_filename)

            cur.execute(
                "UPDATE vehicles SET qr_filename = ? WHERE id = ?",
                (qr_filename, vehicle_id)
            )

            conn.commit()
            flash("Vehicle added and QR generated successfully.", "success")
            return redirect(url_for("user_dashboard"))

        except sqlite3.IntegrityError:
            flash("This vehicle number already exists.", "danger")

        finally:
            conn.close()

    return render_template("user_add_vehicle.html")
 


# ---------------- USER DELETE OWN VEHICLE ----------------
@app.route("/user/delete-vehicle/<int:vehicle_id>")
@user_required
def user_delete_vehicle(vehicle_id):
    conn = get_db()
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ? AND user_id = ?",
        (vehicle_id, session["user_id"])
    ).fetchone()

    if vehicle:
        if vehicle["qr_filename"]:
            qr_path = os.path.join(QR_FOLDER, vehicle["qr_filename"])
            if os.path.exists(qr_path):
                os.remove(qr_path)

        conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        conn.commit()
        flash("Vehicle deleted successfully.", "success")
    else:
        flash("Vehicle not found.", "danger")

    conn.close()
    return redirect(url_for("user_dashboard"))


# ---------------- ADMIN LOGIN ----------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_db()
        admin = conn.execute(
            "SELECT * FROM admin WHERE username = ? AND password = ?",
            (username, password)
        ).fetchone()
        conn.close()

        if admin:
            session.clear()
            session["admin_id"] = admin["id"]
            session["username"] = admin["username"]
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin username or password.", "danger")

    return render_template("admin_login.html")


# Old route support
@app.route("/login", methods=["GET", "POST"])
def login():
    return redirect(url_for("admin_login"))


# ---------------- ADMIN LOGOUT ----------------
@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Admin logged out successfully.", "success")
    return redirect(url_for("admin_login"))


# ---------------- ADMIN DASHBOARD ----------------
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    search = request.args.get("search", "").strip()

    conn = get_db()

    if search:
        vehicles = conn.execute("""
            SELECT vehicles.*, users.email AS user_email
            FROM vehicles
            LEFT JOIN users ON vehicles.user_id = users.id
            WHERE vehicle_no LIKE ?
               OR owner_name LIKE ?
               OR phone LIKE ?
               OR users.email LIKE ?
            ORDER BY vehicles.id DESC
        """, (f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%")).fetchall()
    else:
        vehicles = conn.execute("""
            SELECT vehicles.*, users.email AS user_email
            FROM vehicles
            LEFT JOIN users ON vehicles.user_id = users.id
            ORDER BY vehicles.id DESC
        """).fetchall()

    total = conn.execute("SELECT COUNT(*) AS count FROM vehicles").fetchone()["count"]
    users_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    conn.close()

    return render_template(
        "admin_dashboard.html",
        vehicles=vehicles,
        total=total,
        users_count=users_count,
        search=search
    )


# Old route support
@app.route("/dashboard")
@admin_required
def dashboard():
    return redirect(url_for("admin_dashboard"))


# ---------------- ADMIN ADD VEHICLE ----------------
@app.route("/admin/register-vehicle", methods=["GET", "POST"])
@admin_required
def admin_register_vehicle():
    if request.method == "POST":
        vehicle_no = request.form.get("vehicle_no", "").upper().strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()

        if not vehicle_no or not owner_name or not phone:
            flash("Vehicle number, owner name, and phone are required.", "danger")
            return redirect(url_for("admin_register_vehicle"))

        conn = get_db()

        try:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vehicles(user_id, vehicle_no, owner_name, phone, address, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (None, vehicle_no, owner_name, phone, address, created_at))

            vehicle_id = cur.lastrowid

            scan_url = url_for("vehicle_details", vehicle_id=vehicle_id, _external=True)
            qr_filename = f"vehicle_{vehicle_id}.png"
            qr_path = os.path.join(QR_FOLDER, qr_filename)

            create_qr_template(scan_url, vehicle_no, qr_filename)

            cur.execute(
                "UPDATE vehicles SET qr_filename = ? WHERE id = ?",
                (qr_filename, vehicle_id)
            )

            conn.commit()
            flash("Vehicle registered and QR generated successfully.", "success")
            return redirect(url_for("admin_dashboard"))

        except sqlite3.IntegrityError:
            flash("This vehicle number already exists.", "danger")

        finally:
            conn.close()

    return render_template("admin_register_vehicle.html")


# Old route support
@app.route("/register", methods=["GET", "POST"])
@admin_required
def register():
    return redirect(url_for("admin_register_vehicle"))


# ---------------- PUBLIC VEHICLE DETAILS PAGE ----------------
@app.route("/vehicle/<int:vehicle_id>")
def vehicle_details(vehicle_id):
    conn = get_db()
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ?",
        (vehicle_id,)
    ).fetchone()
    conn.close()

    if vehicle is None:
        return render_template("not_found.html"), 404

    return render_template("vehicle_details.html", vehicle=vehicle)


# ---------------- ADMIN EDIT VEHICLE ----------------
@app.route("/admin/edit/<int:vehicle_id>", methods=["GET", "POST"])
@admin_required
def edit_vehicle(vehicle_id):
    conn = get_db()
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ?",
        (vehicle_id,)
    ).fetchone()

    if vehicle is None:
        conn.close()
        flash("Vehicle not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        vehicle_no = request.form.get("vehicle_no", "").upper().strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()

        try:
            conn.execute("""
                UPDATE vehicles
                SET vehicle_no = ?, owner_name = ?, phone = ?, address = ?
                WHERE id = ?
            """, (vehicle_no, owner_name, phone, address, vehicle_id))
            conn.commit()
            flash("Vehicle updated successfully.", "success")
            return redirect(url_for("admin_dashboard"))

        except sqlite3.IntegrityError:
            flash("Vehicle number already exists.", "danger")

    conn.close()
    return render_template("edit_vehicle.html", vehicle=vehicle)


# ---------------- ADMIN DELETE VEHICLE ----------------
@app.route("/admin/delete/<int:vehicle_id>")
@admin_required
def delete_vehicle(vehicle_id):
    conn = get_db()
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ?",
        (vehicle_id,)
    ).fetchone()

    if vehicle:
        if vehicle["qr_filename"]:
            qr_path = os.path.join(QR_FOLDER, vehicle["qr_filename"])
            if os.path.exists(qr_path):
                os.remove(qr_path)

        conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        conn.commit()
        flash("Vehicle deleted successfully.", "success")
    else:
        flash("Vehicle not found.", "danger")

    conn.close()
    return redirect(url_for("admin_dashboard"))




# ---------------- DOWNLOAD QR ----------------
@app.route("/download-qr/<int:vehicle_id>")
def download_qr(vehicle_id):
    conn = get_db()
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ?",
        (vehicle_id,)
    ).fetchone()
    conn.close()

    if not vehicle or not vehicle["qr_filename"]:
        flash("QR code not found.", "danger")
        return redirect(url_for("home"))

    file_path = os.path.join(QR_FOLDER, vehicle["qr_filename"])

    if not os.path.exists(file_path):
        flash("QR image file not found.", "danger")
        return redirect(url_for("home"))

    return send_file(file_path, as_attachment=True)

# ---------------- EXPORT EXCEL ----------------
@app.route("/export/excel")
@admin_required
def export_excel():
    conn = get_db()
    rows = conn.execute("""
        SELECT vehicle_no, owner_name, phone, address, created_at
        FROM vehicles
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    data = [dict(row) for row in rows]
    df = pd.DataFrame(data)

    file_path = os.path.join(EXPORT_FOLDER, "vehicle_records.xlsx")
    df.to_excel(file_path, index=False)

    return send_file(file_path, as_attachment=True)


# ---------------- EXPORT PDF ----------------
@app.route("/export/pdf")
@admin_required
def export_pdf():
    conn = get_db()
    vehicles = conn.execute("""
        SELECT vehicle_no, owner_name, phone, address, created_at
        FROM vehicles
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    file_path = os.path.join(EXPORT_FOLDER, "vehicle_records.pdf")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(190, 10, "QR Traffic Management - Vehicle Records", ln=True, align="C")
    pdf.ln(8)

    pdf.set_font("Arial", "B", 10)
    pdf.cell(35, 8, "Vehicle No", border=1)
    pdf.cell(40, 8, "Owner", border=1)
    pdf.cell(35, 8, "Phone", border=1)
    pdf.cell(45, 8, "Address", border=1)
    pdf.cell(35, 8, "Created At", border=1)
    pdf.ln()

    pdf.set_font("Arial", "", 9)

    for v in vehicles:
        pdf.cell(35, 8, str(v["vehicle_no"])[:16], border=1)
        pdf.cell(40, 8, str(v["owner_name"])[:18], border=1)
        pdf.cell(35, 8, str(v["phone"])[:15], border=1)
        pdf.cell(45, 8, str(v["address"] or "")[:20], border=1)
        pdf.cell(35, 8, str(v["created_at"])[:16], border=1)
        pdf.ln()

    pdf.output(file_path)

    return send_file(file_path, as_attachment=True)


# ---------------- RUN APP ----------------

if __name__ == "__main__":
    app.run()