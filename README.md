# QR-Based Traffic Management System

A Flask + SQLite web project for managing vehicles using QR codes.

## Features

- User registration with OTP verification
- User login
- User dashboard
- Users can add their own vehicles
- Auto QR generation
- QR scan page with Call and WhatsApp Alert
- Admin dashboard
- Admin can view all vehicles
- Admin can add/edit/delete vehicles
- Export vehicle records to Excel/PDF

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## User Flow

```text
User Register → OTP Verify → User Login → Add Vehicle → QR Generated
```

## Demo OTP Mode

By default, OTP works without Twilio.

```text
OTP: 123456
```

## Real Twilio OTP Setup

Create a `.env` file in the project folder:

```env
USE_TWILIO_OTP=true
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=+1234567890
```

Install packages:

```bash
pip install twilio python-dotenv
```

Run:

```bash
python app.py
```

Important: On Twilio trial account, the receiver phone number must be verified in Twilio first.

## Admin Login

```text
URL: /admin/login
Username: admin
Password: admin123
```

## Important

If you used an older project database and get database errors, delete `traffic.db` and run again.


## QR Template Design Added

Generated QR codes are now poster-style:

- Dark background
- Yellow heading: SCAN TO CONTACT OWNER
- Yellow border around QR
- Vehicle number at bottom
- Download QR option for user and admin
