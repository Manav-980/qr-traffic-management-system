# QR Traffic Management System - PostgreSQL Version

This version uses PostgreSQL through `DATABASE_URL`.

## Render Setup

1. Create a PostgreSQL database on Render.
2. Copy the **Internal Database URL**.
3. Add it to your Web Service environment variables as `DATABASE_URL`.
4. Add:

```env
USE_TWILIO_OTP=false
SECRET_KEY=any-random-secret
```

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python -m gunicorn app:app
```

## Local Setup

Create `.env`:

```env
DATABASE_URL=postgresql://postgres:your_password@localhost:5432/qr_traffic_db
USE_TWILIO_OTP=false
SECRET_KEY=local-secret
```

Run:

```bash
pip install -r requirements.txt
python app.py
```

Default admin: `/admin/login` → `admin / admin123`
