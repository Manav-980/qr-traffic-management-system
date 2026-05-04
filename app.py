
import os
import random
from datetime import datetime
from functools import wraps

import pandas as pd
import psycopg2
import psycopg2.extras
import qrcode
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file

try:
    from dotenv import load_dotenv
    from twilio.rest import Client
except Exception:
    load_dotenv = None
    Client = None

if load_dotenv:
    load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change_this_secret_key")

DATABASE_URL = os.getenv("DATABASE_URL")
USE_TWILIO_OTP = os.getenv("USE_TWILIO_OTP", "false").lower() == "true"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

QR_FOLDER = os.path.join("static", "qr_codes")
EXPORT_FOLDER = "exports"
os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)


def get_db():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL missing. Add PostgreSQL DATABASE_URL in .env or Render Environment.")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            email VARCHAR(150) UNIQUE NOT NULL,
            phone VARCHAR(20) NOT NULL,
            password VARCHAR(255) NOT NULL,
            created_at VARCHAR(50)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            vehicle_no VARCHAR(50) UNIQUE NOT NULL,
            owner_name VARCHAR(150) NOT NULL,
            phone VARCHAR(20) NOT NULL,
            address TEXT,
            qr_filename VARCHAR(255),
            created_at VARCHAR(50)
        )
    """)
    cur.execute("SELECT id FROM admin WHERE username=%s", ("admin",))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO admin(username,password) VALUES(%s,%s)", ("admin", "admin123"))
    conn.commit()
    cur.close()
    conn.close()


def create_qr_template(scan_url, vehicle_no, qr_filename):
    poster_width, poster_height = 900, 1150
    bg_color, yellow, white = (32, 32, 32), (224, 194, 65), (255, 255, 255)
    poster = Image.new("RGB", (poster_width, poster_height), bg_color)
    draw = ImageDraw.Draw(poster)
    try:
        title_font = ImageFont.truetype("arialbd.ttf", 72)
        small_font = ImageFont.truetype("arialbd.ttf", 34)
        tiny_font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        title_font = small_font = tiny_font = ImageFont.load_default()

    for text, y in [("SCAN TO", 65), ("CONTACT OWNER", 145)]:
        box = draw.textbbox((0, 0), text, font=title_font)
        draw.text(((poster_width - (box[2]-box[0])) / 2, y), text, fill=yellow, font=title_font)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=14, border=2)
    qr.add_data(scan_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((650, 650))

    draw.rounded_rectangle([105, 285, 795, 975], radius=18, fill=yellow)
    draw.rectangle([125, 305, 775, 955], fill=white)
    poster.paste(qr_img, (125, 305))

    vehicle_text = f"VEHICLE NO: {vehicle_no}"
    box = draw.textbbox((0, 0), vehicle_text, font=small_font)
    draw.text(((poster_width - (box[2]-box[0])) / 2, 1000), vehicle_text, fill=yellow, font=small_font)
    footer = "QR TRAFFIC MANAGEMENT SYSTEM"
    box = draw.textbbox((0, 0), footer, font=tiny_font)
    draw.text(((poster_width - (box[2]-box[0])) / 2, 1060), footer, fill=white, font=tiny_font)

    path = os.path.join(QR_FOLDER, qr_filename)
    poster.save(path, quality=95)
    return path


def generate_otp():
    return str(random.randint(100000, 999999)) if USE_TWILIO_OTP else "123456"


def send_otp(phone, otp):
    if not USE_TWILIO_OTP:
        return "DEMO_OTP"
    if not Client:
        raise Exception("Install twilio and python-dotenv")
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        raise Exception("Twilio credentials missing")
    clean_phone = phone.strip().replace(" ", "").replace("+91", "")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(body=f"Your QR Traffic System OTP is {otp}", from_=TWILIO_PHONE_NUMBER, to=f"+91{clean_phone}")
    return msg.sid


def one(sql, params=()):
    conn = get_db(); cur = conn.cursor(); cur.execute(sql, params); row = cur.fetchone(); cur.close(); conn.close(); return row


def all_rows(sql, params=()):
    conn = get_db(); cur = conn.cursor(); cur.execute(sql, params); rows = cur.fetchall(); cur.close(); conn.close(); return rows


def execute(sql, params=(), returning=False):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(sql, params)
        row = cur.fetchone() if returning else None
        conn.commit(); return row
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


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


init_db()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/user/register", methods=["GET", "POST"])
def user_register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        if not name or not email or not phone or not password:
            flash("All fields are required.", "danger"); return redirect(url_for("user_register"))
        if one("SELECT id FROM users WHERE email=%s", (email,)):
            flash("Email already registered.", "danger"); return redirect(url_for("user_register"))
        otp = generate_otp()
        session["pending_user"] = {"name": name, "email": email, "phone": phone, "password": password}
        session["user_register_otp"] = otp
        try:
            send_otp(phone, otp)
            flash("OTP sent to your mobile number." if USE_TWILIO_OTP else "Demo OTP mode active. Use OTP: 123456", "success" if USE_TWILIO_OTP else "warning")
            return redirect(url_for("verify_user_otp"))
        except Exception as e:
            flash(f"OTP sending failed: {e}", "danger")
    return render_template("user_register.html")


@app.route("/user/verify-otp", methods=["GET", "POST"])
def verify_user_otp():
    if "pending_user" not in session:
        flash("No pending registration found.", "danger"); return redirect(url_for("user_register"))
    if request.method == "POST":
        if request.form.get("otp", "").strip() != session.get("user_register_otp"):
            flash("Invalid OTP. Please try again.", "danger"); return redirect(url_for("verify_user_otp"))
        u = session["pending_user"]
        try:
            execute("INSERT INTO users(name,email,phone,password,created_at) VALUES(%s,%s,%s,%s,%s)",
                    (u["name"], u["email"], u["phone"], u["password"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            session.pop("pending_user", None); session.pop("user_register_otp", None)
            flash("OTP verified. Registration successful. Please login.", "success")
            return redirect(url_for("user_login"))
        except psycopg2.errors.UniqueViolation:
            flash("Email already registered.", "danger")
    return render_template("verify_user_otp.html")


@app.route("/user/resend-otp")
def resend_user_otp():
    if "pending_user" not in session:
        flash("No pending registration found.", "danger"); return redirect(url_for("user_register"))
    otp = generate_otp(); session["user_register_otp"] = otp
    send_otp(session["pending_user"]["phone"], otp)
    flash("OTP resent." if USE_TWILIO_OTP else "Demo OTP resent. Use OTP: 123456", "success" if USE_TWILIO_OTP else "warning")
    return redirect(url_for("verify_user_otp"))


@app.route("/user/login", methods=["GET", "POST"])
def user_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        user = one("SELECT * FROM users WHERE email=%s AND password=%s", (email, password))
        if user:
            session.clear(); session["user_id"] = user["id"]; session["user_name"] = user["name"]
            flash("Login successful.", "success"); return redirect(url_for("user_dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("user_login.html")


@app.route("/user/logout")
def user_logout():
    session.clear(); flash("Logged out successfully.", "success"); return redirect(url_for("user_login"))


@app.route("/user/dashboard")
@user_required
def user_dashboard():
    vehicles = all_rows("SELECT * FROM vehicles WHERE user_id=%s ORDER BY id DESC", (session["user_id"],))
    return render_template("user_dashboard.html", vehicles=vehicles)


@app.route("/user/add-vehicle", methods=["GET", "POST"])
@user_required
def user_add_vehicle():
    if request.method == "POST":
        vehicle_no = request.form.get("vehicle_no", "").upper().strip()
        address = request.form.get("address", "").strip()
        if not vehicle_no:
            flash("Vehicle number is required.", "danger"); return redirect(url_for("user_add_vehicle"))
        user = one("SELECT * FROM users WHERE id=%s", (session["user_id"],))
        try:
            row = execute("""
                INSERT INTO vehicles(user_id,vehicle_no,owner_name,phone,address,created_at)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING id
            """, (session["user_id"], vehicle_no, user["name"], user["phone"], address, datetime.now().strftime("%Y-%m-%d %H:%M:%S")), True)
            vehicle_id = row["id"]; qr_filename = f"vehicle_{vehicle_id}.png"
            create_qr_template(url_for("vehicle_details", vehicle_id=vehicle_id, _external=True), vehicle_no, qr_filename)
            execute("UPDATE vehicles SET qr_filename=%s WHERE id=%s", (qr_filename, vehicle_id))
            flash("Vehicle added and QR generated successfully.", "success"); return redirect(url_for("user_dashboard"))
        except psycopg2.errors.UniqueViolation:
            flash("This vehicle number already exists.", "danger")
    return render_template("user_add_vehicle.html")


@app.route("/user/delete-vehicle/<int:vehicle_id>")
@user_required
def user_delete_vehicle(vehicle_id):
    vehicle = one("SELECT * FROM vehicles WHERE id=%s AND user_id=%s", (vehicle_id, session["user_id"]))
    if vehicle:
        if vehicle["qr_filename"]:
            path = os.path.join(QR_FOLDER, vehicle["qr_filename"])
            if os.path.exists(path): os.remove(path)
        execute("DELETE FROM vehicles WHERE id=%s", (vehicle_id,))
        flash("Vehicle deleted successfully.", "success")
    else:
        flash("Vehicle not found.", "danger")
    return redirect(url_for("user_dashboard"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        admin = one("SELECT * FROM admin WHERE username=%s AND password=%s", (request.form.get("username", "").strip(), request.form.get("password", "").strip()))
        if admin:
            session.clear(); session["admin_id"] = admin["id"]; session["username"] = admin["username"]
            flash("Admin login successful.", "success"); return redirect(url_for("admin_dashboard"))
        flash("Invalid admin username or password.", "danger")
    return render_template("admin_login.html")


@app.route("/login")
def login():
    return redirect(url_for("admin_login"))


@app.route("/admin/logout")
def admin_logout():
    session.clear(); flash("Admin logged out successfully.", "success"); return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    search = request.args.get("search", "").strip()
    if search:
        q = f"%{search}%"
        vehicles = all_rows("""
            SELECT vehicles.*, users.email AS user_email FROM vehicles
            LEFT JOIN users ON vehicles.user_id=users.id
            WHERE vehicle_no ILIKE %s OR owner_name ILIKE %s OR phone ILIKE %s OR users.email ILIKE %s
            ORDER BY vehicles.id DESC
        """, (q, q, q, q))
    else:
        vehicles = all_rows("""
            SELECT vehicles.*, users.email AS user_email FROM vehicles
            LEFT JOIN users ON vehicles.user_id=users.id
            ORDER BY vehicles.id DESC
        """)
    total = one("SELECT COUNT(*) AS count FROM vehicles")["count"]
    users_count = one("SELECT COUNT(*) AS count FROM users")["count"]
    return render_template("admin_dashboard.html", vehicles=vehicles, total=total, users_count=users_count, search=search)


@app.route("/dashboard")
@admin_required
def dashboard():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/register-vehicle", methods=["GET", "POST"])
@admin_required
def admin_register_vehicle():
    if request.method == "POST":
        vehicle_no = request.form.get("vehicle_no", "").upper().strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        if not vehicle_no or not owner_name or not phone:
            flash("Vehicle number, owner name, and phone are required.", "danger"); return redirect(url_for("admin_register_vehicle"))
        try:
            row = execute("""
                INSERT INTO vehicles(user_id,vehicle_no,owner_name,phone,address,created_at)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING id
            """, (None, vehicle_no, owner_name, phone, address, datetime.now().strftime("%Y-%m-%d %H:%M:%S")), True)
            vehicle_id = row["id"]; qr_filename = f"vehicle_{vehicle_id}.png"
            create_qr_template(url_for("vehicle_details", vehicle_id=vehicle_id, _external=True), vehicle_no, qr_filename)
            execute("UPDATE vehicles SET qr_filename=%s WHERE id=%s", (qr_filename, vehicle_id))
            flash("Vehicle registered and QR generated successfully.", "success"); return redirect(url_for("admin_dashboard"))
        except psycopg2.errors.UniqueViolation:
            flash("This vehicle number already exists.", "danger")
    return render_template("admin_register_vehicle.html")


@app.route("/register")
@admin_required
def register():
    return redirect(url_for("admin_register_vehicle"))


@app.route("/vehicle/<int:vehicle_id>")
def vehicle_details(vehicle_id):
    vehicle = one("SELECT * FROM vehicles WHERE id=%s", (vehicle_id,))
    if vehicle is None:
        return render_template("not_found.html"), 404
    return render_template("vehicle_details.html", vehicle=vehicle)


@app.route("/admin/edit/<int:vehicle_id>", methods=["GET", "POST"])
@admin_required
def edit_vehicle(vehicle_id):
    vehicle = one("SELECT * FROM vehicles WHERE id=%s", (vehicle_id,))
    if vehicle is None:
        flash("Vehicle not found.", "danger"); return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        try:
            execute("""
                UPDATE vehicles SET vehicle_no=%s, owner_name=%s, phone=%s, address=%s WHERE id=%s
            """, (request.form.get("vehicle_no", "").upper().strip(), request.form.get("owner_name", "").strip(), request.form.get("phone", "").strip(), request.form.get("address", "").strip(), vehicle_id))
            flash("Vehicle updated successfully.", "success"); return redirect(url_for("admin_dashboard"))
        except psycopg2.errors.UniqueViolation:
            flash("Vehicle number already exists.", "danger")
    return render_template("edit_vehicle.html", vehicle=vehicle)


@app.route("/admin/delete/<int:vehicle_id>")
@admin_required
def delete_vehicle(vehicle_id):
    vehicle = one("SELECT * FROM vehicles WHERE id=%s", (vehicle_id,))
    if vehicle:
        if vehicle["qr_filename"]:
            path = os.path.join(QR_FOLDER, vehicle["qr_filename"])
            if os.path.exists(path): os.remove(path)
        execute("DELETE FROM vehicles WHERE id=%s", (vehicle_id,))
        flash("Vehicle deleted successfully.", "success")
    else:
        flash("Vehicle not found.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/download-qr/<int:vehicle_id>")
def download_qr(vehicle_id):
    vehicle = one("SELECT * FROM vehicles WHERE id=%s", (vehicle_id,))
    if not vehicle or not vehicle["qr_filename"]:
        flash("QR code not found.", "danger"); return redirect(url_for("home"))
    path = os.path.join(QR_FOLDER, vehicle["qr_filename"])
    if not os.path.exists(path):
        create_qr_template(url_for("vehicle_details", vehicle_id=vehicle_id, _external=True), vehicle["vehicle_no"], vehicle["qr_filename"])
    return send_file(path, as_attachment=True)


@app.route("/export/excel")
@admin_required
def export_excel():
    rows = all_rows("SELECT vehicle_no,owner_name,phone,address,created_at FROM vehicles ORDER BY id DESC")
    file_path = os.path.join(EXPORT_FOLDER, "vehicle_records.xlsx")
    pd.DataFrame(rows).to_excel(file_path, index=False)
    return send_file(file_path, as_attachment=True)


@app.route("/export/pdf")
@admin_required
def export_pdf():
    vehicles = all_rows("SELECT vehicle_no,owner_name,phone,address,created_at FROM vehicles ORDER BY id DESC")
    file_path = os.path.join(EXPORT_FOLDER, "vehicle_records.pdf")
    pdf = FPDF(); pdf.add_page(); pdf.set_font("Arial", "B", 16)
    pdf.cell(190, 10, "QR Traffic Management - Vehicle Records", ln=True, align="C"); pdf.ln(8)
    pdf.set_font("Arial", "B", 10)
    for h,w in [("Vehicle No",35),("Owner",40),("Phone",35),("Address",45),("Created At",35)]: pdf.cell(w, 8, h, border=1)
    pdf.ln(); pdf.set_font("Arial", "", 9)
    for v in vehicles:
        pdf.cell(35, 8, str(v["vehicle_no"])[:16], border=1)
        pdf.cell(40, 8, str(v["owner_name"])[:18], border=1)
        pdf.cell(35, 8, str(v["phone"])[:15], border=1)
        pdf.cell(45, 8, str(v["address"] or "")[:20], border=1)
        pdf.cell(35, 8, str(v["created_at"])[:16], border=1); pdf.ln()
    pdf.output(file_path); return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
