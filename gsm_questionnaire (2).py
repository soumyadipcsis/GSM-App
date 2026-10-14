"""
Monolithic GSM Questionnaire Application
For basic keypad phones + USB GSM modem/SIM + construction-worker survey in Goa.

Install:
    pip install flask pyserial

Configure environment variables before running:
    DASHBOARD_PASSWORD = a strong password
    FLASK_SECRET_KEY = a long random secret
    GSM_PORT = COM5 (Windows) or /dev/ttyUSB0 (Linux)
    GSM_BAUD = 9600
    GSM_DB_PATH = questionnaire.db (optional)

Run:
    python gsm_questionnaire.py

Open dashboard:
    http://127.0.0.1:5000

Run with GSM modem:
    python gsm_questionnaire.py --modem

Run dashboard only:
    python gsm_questionnaire.py --web

SMS commands:
    START / SURVEY / REGISTER - start questionnaire
    CONTINUE - repeat current prompt
    STOP - stop survey
    YES - consent to participate

Important:
- Test with a spare SIM and test phones before field use.
- Do not collect Aadhaar numbers via SMS. The "ID" field is treated as a survey ID.
- Symptom question is not a diagnosis; follow the project's public-health protocol.
- Modem AT commands vary by model. Test and adapt for your exact modem.
"""

import os
import sys
import re
import csv
import io
import time
import sqlite3
import secrets
import threading
from datetime import datetime, date
from flask import Flask, request, redirect, url_for, render_template_string, Response, session

try:
    import serial
except ImportError:
    serial = None

APP_NAME = "GSM Worker Questionnaire"
DB_PATH = os.environ.get("GSM_DB_PATH", "questionnaire.db")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "change-me-now")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "replace-this-with-a-long-random-secret")
GSM_PORT = os.environ.get("GSM_PORT", "COM5" if os.name == "nt" else "/dev/ttyUSB0")
GSM_BAUD = int(os.environ.get("GSM_BAUD", "9600"))
POLL_SECONDS = int(os.environ.get("GSM_POLL_SECONDS", "8"))

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 participant_id TEXT UNIQUE NOT NULL,
 phone TEXT NOT NULL,
 full_name TEXT,
 id_number TEXT,
 site_name TEXT,
 address TEXT,
 age INTEGER,
 gender TEXT,
 mobile_number TEXT,
 government_id_type TEXT,
 home_town TEXT,
 travel_outside_goa TEXT,
 departure_date TEXT,
 return_date TEXT,
 malaria_symptoms TEXT,
 status TEXT DEFAULT 'in_progress',
 current_step INTEGER DEFAULT 0,
 followup_sent INTEGER DEFAULT 0,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sms_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phone TEXT NOT NULL,
 direction TEXT NOT NULL,
 message TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_seen (
 modem_index TEXT PRIMARY KEY,
 seen_at TEXT NOT NULL
);
"""

CONSENT_TEXT = (
    "Goa Worker Survey. This survey collects registration and travel information "
    "and asks about malaria-like symptoms after return. Participation is voluntary. "
    "Reply YES to consent or NO to stop. Do not send Aadhaar numbers by SMS."
)

# The original questionnaire's "ID" field is implemented as a survey ID.
# The government ID field records only Aadhaar/Other, not the ID number.
FIELDS = [
    ("full_name", "Please enter your full name as per ID."),
    ("id_number", "Enter the survey ID provided by the field team (not your government ID number)."),
    ("site_name", "Enter your construction site name or site code."),
    ("address", "Enter your current address (short text)."),
    ("age", "Enter your age in completed years (number only)."),
    ("gender", "Enter gender: M for Male or F for Female."),
    ("mobile_number", "Enter your mobile number, or reply SAME to use this phone number."),
    ("government_id_type", "Enter government ID type: A for Aadhaar, O for Other. Do not send the ID number."),
    ("home_town", "Enter your home town."),
    ("travel_outside_goa", "Are you planning to visit outside Goa? Reply Y for Yes or N for No."),
]
DATE_FMT = "%d-%m-%Y"


def now():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    return conn


def log_sms(conn, phone, direction, message):
    conn.execute(
        "INSERT INTO sms_log(phone,direction,message,created_at) VALUES(?,?,?,?)",
        (phone, direction, str(message)[:1000], now())
    )
    conn.commit()


def modem_command(ser, command, wait=0.5):
    ser.write((command + "\r").encode())
    time.sleep(wait)
    return ser.read_all().decode(errors="ignore")


def send_sms(ser, conn, phone, message):
    """Send an SMS using a standard AT-command sequence."""
    try:
        modem_command(ser, "AT+CMGF=1")
        ser.write(('AT+CMGS="%s"\r' % phone).encode())
        time.sleep(0.7)
        ser.read_all()
        ser.write(message.encode(errors="replace") + bytes([26]))
        time.sleep(3)
        response = ser.read_all().decode(errors="ignore")
        log_sms(conn, phone, "OUT", message)
        print(f"[SMS OUT] {phone}: {message}")
        if "ERROR" in response.upper():
            print("[MODEM WARNING] Modem returned an error:", response)
        return response
    except Exception as exc:
        print(f"[SMS ERROR] Could not send to {phone}: {exc}")
        log_sms(conn, phone, "OUT_ERROR", message + " [SEND ERROR]")
        return ""


def parse_cmgl(raw):
    """Parse common text-mode AT+CMGL response; may need adaptation for some modems."""
    lines = [line.strip() for line in raw.replace("\r", "").split("\n") if line.strip()]
    messages = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("+CMGL:"):
            header = lines[i]
            body = lines[i + 1] if i + 1 < len(lines) else ""
            match = re.search(r'\+CMGL:\s*(\d+),\s*"[^"]*",\s*"([^"]+)"', header)
            if match:
                messages.append((match.group(1), match.group(2), body))
            i += 2
        else:
            i += 1
    return messages


def get_active_person(conn, phone):
    return conn.execute(
        """SELECT * FROM participants
           WHERE phone=? AND status IN ('consent','in_progress','symptom_followup')
           ORDER BY id DESC LIMIT 1""", (phone,)
    ).fetchone()


def start_session(ser, conn, phone):
    active = get_active_person(conn, phone)
    if active:
        send_sms(ser, conn, phone, "A survey is already in progress. Reply CONTINUE to repeat the current question or STOP to stop.")
        return
    participant_id = "GOA-" + secrets.token_hex(3).upper()
    conn.execute(
        """INSERT INTO participants
           (participant_id,phone,status,current_step,created_at,updated_at)
           VALUES(?,?,'consent',-1,?,?)""",
        (participant_id, phone, now(), now())
    )
    conn.commit()
    send_sms(ser, conn, phone, CONSENT_TEXT)


def send_next_question(ser, conn, participant_id):
    p = conn.execute("SELECT * FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
    if not p:
        return
    step = p["current_step"]

    if p["status"] == "symptom_followup":
        send_sms(ser, conn, p["phone"],
                 "Since returning to Goa, have you had malaria-like symptoms such as fever or chills? Reply Y for Yes or N for No. This is not a diagnosis; seek medical care for symptoms.")
        return

    if 0 <= step < len(FIELDS):
        send_sms(ser, conn, p["phone"], FIELDS[step][1])
    elif step == len(FIELDS):
        if p["travel_outside_goa"] == "Yes":
            send_sms(ser, conn, p["phone"], "Enter date of departure from Goa as DD-MM-YYYY.")
        else:
            conn.execute("UPDATE participants SET status='completed', updated_at=? WHERE participant_id=?",
                         (now(), participant_id))
            conn.commit()
            send_sms(ser, conn, p["phone"], "Thank you. Your registration questionnaire is complete.")
    elif step == len(FIELDS) + 1:
        send_sms(ser, conn, p["phone"], "Enter expected return date to Goa as DD-MM-YYYY.")
    else:
        conn.execute("UPDATE participants SET status='completed', updated_at=? WHERE participant_id=?",
                     (now(), participant_id))
        conn.commit()
        send_sms(ser, conn, p["phone"],
                 "Thank you. Registration is complete. We will ask about symptoms after your return.")


def save_field_answer(ser, conn, p, text):
    step = p["current_step"]
    upper = text.strip().upper()

    if 0 <= step < len(FIELDS):
        field = FIELDS[step][0]
        value = text.strip()
        error = None

        if field == "age":
            if not value.isdigit() or not (16 <= int(value) <= 100):
                error = "Please enter a valid age in years (16-100)."
            else:
                value = int(value)
        elif field == "gender":
            if upper not in ("M", "F", "MALE", "FEMALE"):
                error = "Reply M for Male or F for Female."
            else:
                value = "Male" if upper in ("M", "MALE") else "Female"
        elif field == "mobile_number":
            value = p["phone"] if upper == "SAME" else value
            if not re.fullmatch(r"\+?[0-9]{8,15}", str(value).replace(" ", "")):
                error = "Enter a valid mobile number, or reply SAME to use this phone."
        elif field == "government_id_type":
            if upper not in ("A", "O", "AADHAAR", "OTHER"):
                error = "Reply A for Aadhaar or O for Other. Do not send any ID number."
            else:
                value = "Aadhaar" if upper in ("A", "AADHAAR") else "Other"
        elif field == "travel_outside_goa":
            if upper not in ("Y", "N", "YES", "NO"):
                error = "Reply Y for Yes or N for No."
            else:
                value = "Yes" if upper in ("Y", "YES") else "No"
        elif not value:
            error = "This answer cannot be blank. Please try again."

        if error:
            send_sms(ser, conn, p["phone"], error)
            return

        next_step = step + 1
        if field == "travel_outside_goa" and value == "No":
            conn.execute(
                """UPDATE participants SET travel_outside_goa=?, current_step=?, status='completed',
                   updated_at=? WHERE participant_id=?""",
                (value, len(FIELDS) + 2, now(), p["participant_id"])
            )
            conn.commit()
            send_sms(ser, conn, p["phone"], "Thank you. Your registration questionnaire is complete.")
            return

        conn.execute(
            f"UPDATE participants SET {field}=?, current_step=?, updated_at=? WHERE participant_id=?",
            (value, next_step, now(), p["participant_id"])
        )
        conn.commit()
        send_next_question(ser, conn, p["participant_id"])
        return

    if step == len(FIELDS):
        try:
            d = datetime.strptime(text.strip(), DATE_FMT).date()
            if d < date.today():
                raise ValueError
        except ValueError:
            send_sms(ser, conn, p["phone"],
                     "Please enter a valid future departure date as DD-MM-YYYY, e.g. 25-11-2026.")
            return
        conn.execute("UPDATE participants SET departure_date=?, current_step=?, updated_at=? WHERE participant_id=?",
                     (d.isoformat(), step + 1, now(), p["participant_id"]))
        conn.commit()
        send_next_question(ser, conn, p["participant_id"])
        return

    if step == len(FIELDS) + 1:
        try:
            d = datetime.strptime(text.strip(), DATE_FMT).date()
            departure = date.fromisoformat(p["departure_date"])
            if d < departure:
                raise ValueError
        except ValueError:
            send_sms(ser, conn, p["phone"],
                     "Return date must be on or after departure. Use DD-MM-YYYY.")
            return
        conn.execute(
            """UPDATE participants SET return_date=?, current_step=?, status='completed',
               updated_at=? WHERE participant_id=?""",
            (d.isoformat(), step + 1, now(), p["participant_id"])
        )
        conn.commit()
        send_sms(ser, conn, p["phone"],
                 "Thank you. Registration is complete. We will send a symptom follow-up after your return.")
        return

    send_sms(ser, conn, p["phone"], "No active question. Reply START to begin or contact the field team.")


def process_sms(ser, conn, phone, message):
    text = message.strip()
    upper = text.upper()
    log_sms(conn, phone, "IN", text)
    print(f"[SMS IN] {phone}: {text}")

    if upper in ("START", "SURVEY", "REGISTER"):
        start_session(ser, conn, phone)
        return

    p = get_active_person(conn, phone)
    if not p:
        # A follow-up may belong to a completed participant.
        followup = conn.execute(
            """SELECT * FROM participants WHERE phone=? AND status='symptom_followup'
               ORDER BY id DESC LIMIT 1""", (phone,)
        ).fetchone()
        if followup:
            if upper in ("Y", "YES", "N", "NO"):
                answer = "Yes" if upper in ("Y", "YES") else "No"
                conn.execute(
                    "UPDATE participants SET malaria_symptoms=?,status='completed',updated_at=? WHERE participant_id=?",
                    (answer, now(), followup["participant_id"])
                )
                conn.commit()
                send_sms(ser, conn, phone,
                         "Thank you. Your follow-up response has been recorded. If you have fever or chills, seek advice from a qualified health professional.")
            else:
                send_sms(ser, conn, phone, "Please reply Y for Yes or N for No.")
            return
        send_sms(ser, conn, phone, "To start the worker survey, reply START. Contact the field team if you need help.")
        return

    if upper == "STOP":
        conn.execute("UPDATE participants SET status='stopped',updated_at=? WHERE participant_id=?",
                     (now(), p["participant_id"]))
        conn.commit()
        send_sms(ser, conn, phone, "Survey stopped. Contact the field team if you want to restart.")
        return

    if p["status"] == "consent":
        if upper == "YES":
            conn.execute(
                "UPDATE participants SET status='in_progress',current_step=0,updated_at=? WHERE participant_id=?",
                (now(), p["participant_id"])
            )
            conn.commit()
            send_sms(ser, conn, phone, "Consent recorded. " + FIELDS[0][1])
        elif upper == "NO":
            conn.execute("UPDATE participants SET status='stopped',updated_at=? WHERE participant_id=?",
                         (now(), p["participant_id"]))
            conn.commit()
            send_sms(ser, conn, phone, "You have opted out. No further questionnaire questions will be sent.")
        else:
            send_sms(ser, conn, phone, "Please reply YES to consent or NO to stop.")
        return

    if upper == "CONTINUE":
        send_next_question(ser, conn, p["participant_id"])
        return

    if p["status"] == "in_progress":
        save_field_answer(ser, conn, p, text)
        return

    send_sms(ser, conn, phone, "Reply START to begin a new questionnaire or contact the field team.")


def send_due_followups(ser, conn):
    # This prototype sends the symptom follow-up on the recorded return date or later.
    rows = conn.execute(
        """SELECT * FROM participants
           WHERE travel_outside_goa='Yes' AND return_date IS NOT NULL
           AND malaria_symptoms IS NULL AND followup_sent=0 AND status='completed'"""
    ).fetchall()
    today = date.today()
    for p in rows:
        try:
            return_date = date.fromisoformat(p["return_date"])
        except (TypeError, ValueError):
            continue
        if return_date <= today:
            send_sms(ser, conn, p["phone"],
                     "Welcome back. Since returning to Goa, have you had malaria-like symptoms such as fever or chills? Reply Y for Yes or N for No. This is not a diagnosis; seek medical care for symptoms.")
            conn.execute(
                "UPDATE participants SET followup_sent=1,status='symptom_followup',updated_at=? WHERE participant_id=?",
                (now(), p["participant_id"])
            )
            conn.commit()


def modem_loop():
    if serial is None:
        print("ERROR: pyserial is not installed. Run: pip install pyserial")
        return
    print(f"[MODEM] Opening {GSM_PORT} at {GSM_BAUD} baud")
    try:
        with serial.Serial(GSM_PORT, GSM_BAUD, timeout=1) as ser, get_db() as conn:
            print("[MODEM]", modem_command(ser, "AT").strip())
            modem_command(ser, "ATE0")
            modem_command(ser, "AT+CMGF=1")
            modem_command(ser, 'AT+CPMS="SM","SM","SM"')
            print("[MODEM] Listening for SMS. Press Ctrl+C to stop.")
            while True:
                raw = modem_command(ser, 'AT+CMGL="REC UNREAD"', 1)
                for index, phone, body in parse_cmgl(raw):
                    # Caution: storage indices can be reused by some modems.
                    key = "SM-" + index
                    seen = conn.execute("SELECT 1 FROM inbound_seen WHERE modem_index=?", (key,)).fetchone()
                    if seen:
                        # Delete the already-seen message to avoid polling it forever.
                        modem_command(ser, "AT+CMGD=" + index)
                        continue
                    conn.execute("INSERT OR IGNORE INTO inbound_seen(modem_index,seen_at) VALUES(?,?)",
                                 (key, now()))
                    conn.commit()
                    process_sms(ser, conn, phone, body)
                    modem_command(ser, "AT+CMGD=" + index)
                send_due_followups(ser, conn)
                time.sleep(POLL_SECONDS)
    except Exception as exc:
        print("[MODEM ERROR]", exc)
        print("Check SIM, modem serial port, baud rate, SMS capability, and network registration.")


# ------------------------------ WEB DASHBOARD ------------------------------

LOGIN_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GSM Questionnaire Login</title><style>
body{font:16px system-ui;max-width:760px;margin:40px auto;padding:0 16px;background:#f3f6fb;color:#172033}
.card{background:white;padding:22px;border-radius:14px;box-shadow:0 2px 8px #0001}
input{padding:10px;width:95%;max-width:360px;margin:8px 0}button,a{padding:10px 14px;background:#174ea6;color:white;border:0;border-radius:7px;text-decoration:none}
</style></head><body><div class="card"><h1>GSM Worker Questionnaire</h1>
<p>Researcher dashboard login</p><form method="post"><label>Dashboard password</label><br>
<input type="password" name="password" required><br><button type="submit">Login</button></form></div></body></html>
"""

DASHBOARD_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GSM Questionnaire Dashboard</title><style>
body{font:15px system-ui;max-width:1500px;margin:22px auto;padding:0 14px;background:#f3f6fb;color:#172033}
.card{background:white;padding:16px;border-radius:12px;margin:12px 0;box-shadow:0 1px 5px #0001}
table{width:100%;border-collapse:collapse;background:white}th,td{padding:8px;border-bottom:1px solid #e0e5ed;text-align:left;font-size:13px;vertical-align:top}
th{background:#edf2fa}.scroll{overflow-x:auto}.btn{display:inline-block;padding:8px 11px;background:#174ea6;color:white;border-radius:7px;text-decoration:none;margin-right:6px}
.note{color:#5b6577;font-size:13px}
</style></head><body>
<h1>GSM Worker Questionnaire</h1>
<p>Construction-worker field collection · Goa</p>
<p><a class="btn" href="/export.csv">Export CSV</a><a class="btn" href="/logout">Logout</a></p>
<div class="card"><b>Summary</b><p>Total: {{total}} · Completed: {{completed}} · Follow-up pending: {{pending}} · Symptom “Yes”: {{symptom_yes}}</p></div>
<div class="card"><h2>Participants</h2><div class="scroll"><table><tr>
<th>Participant ID</th><th>Name</th><th>Survey ID</th><th>Phone</th><th>Site</th><th>Address</th><th>Age</th><th>Gender</th><th>ID type</th><th>Hometown</th><th>Travel?</th><th>Departure</th><th>Return</th><th>Symptoms</th><th>Status</th><th>Created</th>
</tr>{% for p in participants %}<tr>
<td>{{p.participant_id}}</td><td>{{p.full_name or ''}}</td><td>{{p.id_number or ''}}</td><td>{{p.phone}}</td><td>{{p.site_name or ''}}</td><td>{{p.address or ''}}</td><td>{{p.age or ''}}</td><td>{{p.gender or ''}}</td><td>{{p.government_id_type or ''}}</td><td>{{p.home_town or ''}}</td><td>{{p.travel_outside_goa or ''}}</td><td>{{p.departure_date or ''}}</td><td>{{p.return_date or ''}}</td><td>{{p.malaria_symptoms or 'Pending'}}</td><td>{{p.status}}</td><td>{{p.created_at}}</td>
</tr>{% endfor %}</table></div></div>
<div class="card"><h2>Recent SMS log</h2><div class="scroll"><table><tr><th>Time</th><th>Phone</th><th>Direction</th><th>Message</th></tr>
{% for s in logs %}<tr><td>{{s.created_at}}</td><td>{{s.phone}}</td><td>{{s.direction}}</td><td>{{s.message}}</td></tr>{% endfor %}</table></div></div>
<p class="note">Prototype warning: use strong secrets, HTTPS, restricted network access, role-based access, secure backups, and a retention policy before handling real participant or health data.</p>
</body></html>
"""


def is_logged_in():
    return session.get("gsm_auth") is True


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["gsm_auth"] = True
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_HTML + "<p style='color:#b91c1c'>Incorrect password.</p>")
    if is_logged_in():
        return redirect(url_for("dashboard"))
    return render_template_string(LOGIN_HTML)


@app.route("/dashboard")
def dashboard():
    if not is_logged_in():
        return redirect(url_for("login"))
    with get_db() as conn:
        participants = conn.execute("SELECT * FROM participants ORDER BY id DESC").fetchall()
        logs = conn.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 100").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM participants WHERE status='completed'").fetchone()[0]
        pending = conn.execute("""SELECT COUNT(*) FROM participants
          WHERE travel_outside_goa='Yes' AND return_date IS NOT NULL AND malaria_symptoms IS NULL""").fetchone()[0]
        symptom_yes = conn.execute("SELECT COUNT(*) FROM participants WHERE malaria_symptoms='Yes'").fetchone()[0]
    return render_template_string(DASHBOARD_HTML, participants=participants, logs=logs,
                                  total=total, completed=completed, pending=pending,
                                  symptom_yes=symptom_yes)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/export.csv")
def export_csv():
    if not is_logged_in():
        return redirect(url_for("login"))
    fields = [
        "participant_id", "phone", "full_name", "id_number", "site_name", "address", "age",
        "gender", "mobile_number", "government_id_type", "home_town", "travel_outside_goa",
        "departure_date", "return_date", "malaria_symptoms", "status", "created_at", "updated_at"
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    with get_db() as conn:
        for row in conn.execute("SELECT * FROM participants ORDER BY id"):
            writer.writerow([row[field] for field in fields])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=questionnaire_export.csv"})


def run_web():
    # Local-only dashboard; do not bind to 0.0.0.0 without proper deployment security.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False, use_reloader=False)


if __name__ == "__main__":
    with get_db() as conn:
        pass

    if "--modem" in sys.argv:
        modem_loop()
    elif "--web" in sys.argv:
        run_web()
    else:
        # Monolithic default: start dashboard and GSM worker together.
        thread = threading.Thread(target=modem_loop, daemon=True)
        thread.start()
        run_web()
