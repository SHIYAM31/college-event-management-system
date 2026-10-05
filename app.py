import base64
import csv
import io
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import qrcode
except ImportError:  # app still works; the pass shows the code as text
    qrcode = None

EVENT_TYPES = ["Symposium", "Workshop", "Cultural", "Sports", "Seminar", "Other"]
DT_FORMAT = "%Y-%m-%dT%H:%M"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "change-this-secret-key"),
    DATABASE=os.environ.get("EVENTHUB_DB", os.path.join(app.root_path, "eventhub.db")),
    MAIL_SERVER=os.environ.get("MAIL_SERVER"),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
    MAIL_USERNAME=os.environ.get("MAIL_USERNAME"),
    MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD"),
    MAIL_FROM=os.environ.get("MAIL_FROM", "eventhub@college.local"),
)


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def init_db():
    db = get_db()
    with app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf8"))
    if not query("SELECT 1 FROM users WHERE role = 'admin'", one=True):
        db.execute(
            "INSERT INTO users (name, email, password_hash, role, department) VALUES (?,?,?,?,?)",
            ("Admin", "admin@college.edu", generate_password_hash("admin123"), "admin", "Administration"))
        db.execute(
            "INSERT INTO users (name, email, password_hash, role, department) VALUES (?,?,?,?,?)",
            ("Organizer", "organizer@college.edu", generate_password_hash("org123"), "organizer", "CSE"))
        db.commit()


@app.cli.command("init-db")
def init_db_command():
    """Create tables and the default admin / organizer accounts."""
    init_db()
    print("Database ready. Default logins: admin@college.edu / admin123, "
          "organizer@college.edu / org123 (change these passwords).")


with app.app_context():
    if not os.path.exists(app.config["DATABASE"]):
        init_db()


# ---------------------------------------------------------------- helpers
def now_str():
    return datetime.now().strftime(DT_FORMAT)


def parse_dt(value):
    try:
        return datetime.strptime(value, DT_FORMAT)
    except (TypeError, ValueError):
        return None


@app.template_filter("fmt_dt")
def fmt_dt(value):
    dt = parse_dt(value) if value and "T" in value else None
    if dt:
        return dt.strftime("%d %b %Y, %I:%M %p")
    return value or ""


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["EVENT_TYPES"] = EVENT_TYPES


@app.before_request
def load_user_and_check_csrf():
    uid = session.get("user_id")
    g.user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True) if uid else None
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid CSRF token")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in first.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def send_email(to, subject, body, png_bytes=None):
    """Send an email. With MAIL_SERVER unset, the mail is printed to the console."""
    cfg = app.config
    if not cfg["MAIL_SERVER"]:
        print(f"[MAIL to {to}] {subject}\n{body}\n")
        return True
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], to, subject
        msg.set_content(body)
        if png_bytes:
            msg.add_attachment(png_bytes, maintype="image", subtype="png", filename="event-pass.png")
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"]) as smtp:
            smtp.starttls()
            if cfg["MAIL_USERNAME"]:
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(msg)
        return True
    except Exception as exc:  # a mail failure must not undo a registration
        app.logger.error("Email to %s failed: %s", to, exc)
        return False


def qr_png(token):
    if qrcode is None:
        return None
    buf = io.BytesIO()
    qrcode.make(token).save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(token):
    png = qr_png(token)
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else None


def event_stats(event_id):
    ev = query("SELECT capacity FROM events WHERE id = ?", (event_id,), one=True)
    registered = query("SELECT COUNT(*) c FROM registrations WHERE event_id = ?", (event_id,), one=True)["c"]
    present = query("""SELECT COUNT(*) c FROM attendance a JOIN registrations r ON r.id = a.reg_id
                       WHERE r.event_id = ?""", (event_id,), one=True)["c"]
    avg = query("SELECT AVG(rating) a, COUNT(*) n FROM feedback WHERE event_id = ?", (event_id,), one=True)
    attendance_rate = round(present / registered * 100, 1) if registered else 0.0
    return {
        "registered": registered,
        "present": present,
        "capacity": ev["capacity"],
        "attendance_rate": attendance_rate,
        "fill_rate": round(registered / ev["capacity"] * 100, 1),
        "noshow_rate": round(100 - attendance_rate, 1) if registered else 0.0,
        "avg_rating": round(avg["a"], 2) if avg["a"] is not None else None,
        "ratings": avg["n"],
        "departments": query("""SELECT COALESCE(u.department, 'N/A') AS department,
                                       COUNT(r.id) AS registered, COUNT(a.id) AS present
                                FROM registrations r JOIN users u ON u.id = r.user_id
                                LEFT JOIN attendance a ON a.reg_id = r.id
                                WHERE r.event_id = ? GROUP BY u.department ORDER BY registered DESC""",
                             (event_id,)),
    }


def own_event_or_404(event_id):
    ev = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True)
    if ev is None:
        abort(404)
    if g.user["role"] != "admin" and ev["organizer_id"] != g.user["id"]:
        abort(403)
    return ev


def validate_event_form(form):
    data = {k: (form.get(k) or "").strip() for k in
            ("title", "type", "description", "event_date", "venue", "capacity", "deadline")}
    errors = []
    if not data["title"]:
        errors.append("Title is required.")
    if data["type"] not in EVENT_TYPES:
        errors.append("Choose a valid event type.")
    if not data["venue"]:
        errors.append("Venue is required.")
    ev_dt, dl_dt = parse_dt(data["event_date"]), parse_dt(data["deadline"])
    if not ev_dt:
        errors.append("Enter a valid event date and time.")
    if not dl_dt:
        errors.append("Enter a valid registration deadline.")
    if ev_dt and dl_dt and dl_dt > ev_dt:
        errors.append("Registration deadline must be before the event.")
    try:
        data["capacity"] = int(data["capacity"])
        if data["capacity"] <= 0:
            raise ValueError
    except ValueError:
        errors.append("Capacity must be a positive number.")
    return data, errors


# ---------------------------------------------------------------- auth
@app.route("/")
def index():
    return redirect(url_for("events") if g.user else url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        department = request.form.get("department", "").strip()
        year = request.form.get("year", "").strip()
        if not name or "@" not in email or len(password) < 6 or not department:
            flash("Fill all fields; password needs at least 6 characters.", "error")
        elif query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            flash("This email is already registered.", "error")
        else:
            get_db().execute(
                "INSERT INTO users (name, email, password_hash, role, department, year) VALUES (?,?,?,?,?,?)",
                (name, email, generate_password_hash(password), "student", department,
                 int(year) if year.isdigit() else None))
            get_db().commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next", "")
            if user["role"] == "admin":
                default = url_for("admin_dashboard")
            elif user["role"] == "organizer":
                default = url_for("organizer_events")
            else:
                default = url_for("events")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- student
@app.route("/events")
@login_required
def events():
    q = request.args.get("q", "").strip()
    etype = request.args.get("type", "")
    past = request.args.get("past") == "1"
    sql = """SELECT e.*, u.name AS organizer_name,
                    (SELECT COUNT(*) FROM registrations r WHERE r.event_id = e.id) AS reg_count,
                    (SELECT 1 FROM registrations r WHERE r.event_id = e.id AND r.user_id = ?) AS mine
             FROM events e JOIN users u ON u.id = e.organizer_id
             WHERE e.status = 'approved' AND e.event_date {} ?""".format("<" if past else ">=")
    args = [g.user["id"], now_str()]
    if etype in EVENT_TYPES:
        sql += " AND e.type = ?"
        args.append(etype)
    if q:
        sql += " AND (e.title LIKE ? OR e.venue LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY e.event_date" + (" DESC" if past else "")
    return render_template("events.html", events=query(sql, args), q=q, etype=etype, past=past)


@app.route("/events/<int:event_id>")
@login_required
def event_detail(event_id):
    ev = query("""SELECT e.*, u.name AS organizer_name FROM events e
                  JOIN users u ON u.id = e.organizer_id WHERE e.id = ?""", (event_id,), one=True)
    if ev is None:
        abort(404)
    if ev["status"] != "approved" and g.user["role"] != "admin" and ev["organizer_id"] != g.user["id"]:
        abort(404)
    reg_count = query("SELECT COUNT(*) c FROM registrations WHERE event_id = ?", (event_id,), one=True)["c"]
    reg = query("SELECT * FROM registrations WHERE event_id = ? AND user_id = ?",
                (event_id, g.user["id"]), one=True)
    attended = bool(reg and query("SELECT 1 FROM attendance WHERE reg_id = ?", (reg["id"],), one=True))
    given = query("SELECT 1 FROM feedback WHERE event_id = ? AND user_id = ?",
                  (event_id, g.user["id"]), one=True)
    now = now_str()
    return render_template(
        "event_detail.html", ev=ev, reg_count=reg_count, reg=reg, attended=attended,
        can_feedback=attended and not given and ev["event_date"] <= now,
        open_for_reg=ev["status"] == "approved" and ev["deadline"] >= now and reg_count < ev["capacity"])


@app.post("/events/<int:event_id>/register")
@role_required("student")
def register_for_event(event_id):
    ev = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True)
    if ev is None or ev["status"] != "approved":
        abort(404)
    if ev["deadline"] < now_str():
        flash("Registration closed.", "error")
        return redirect(url_for("event_detail", event_id=event_id))
    if query("SELECT 1 FROM registrations WHERE event_id = ? AND user_id = ?",
             (event_id, g.user["id"]), one=True):
        flash("Already registered.", "error")
        return redirect(url_for("event_detail", event_id=event_id))
    token = secrets.token_urlsafe(16)  # 128 bits of randomness
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO registrations (event_id, user_id, qr_token, reg_date)
               SELECT ?, ?, ?, ?
               WHERE (SELECT COUNT(*) FROM registrations WHERE event_id = ?) <
                     (SELECT capacity FROM events WHERE id = ?)""",
            (event_id, g.user["id"], token, now_str(), event_id, event_id))
        db.commit()
    except sqlite3.IntegrityError:
        flash("Already registered.", "error")
        return redirect(url_for("event_detail", event_id=event_id))
    if cur.rowcount == 0:
        flash("Event full.", "error")
        return redirect(url_for("event_detail", event_id=event_id))
    send_email(
        g.user["email"], f"Registered: {ev['title']}",
        f"Hi {g.user['name']},\n\nYou are registered for {ev['title']} on {fmt_dt(ev['event_date'])} "
        f"at {ev['venue']}.\nShow the attached QR pass at the entrance.\nPass code: {token}\n",
        qr_png(token))
    flash("Registered. Your QR pass is below and has been emailed to you.", "success")
    return redirect(url_for("my_registrations"))


@app.route("/my/registrations")
@role_required("student")
def my_registrations():
    rows = query("""SELECT r.*, e.title, e.event_date, e.venue, e.status,
                           (SELECT scan_time FROM attendance a WHERE a.reg_id = r.id) AS scan_time
                    FROM registrations r JOIN events e ON e.id = r.event_id
                    WHERE r.user_id = ? ORDER BY e.event_date DESC""", (g.user["id"],))
    return render_template("my_registrations.html",
                           rows=[(r, qr_data_uri(r["qr_token"])) for r in rows])


@app.route("/events/<int:event_id>/feedback", methods=["GET", "POST"])
@role_required("student")
def feedback(event_id):
    ev = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True)
    if ev is None:
        abort(404)
    attended = query("""SELECT 1 FROM attendance a JOIN registrations r ON r.id = a.reg_id
                        WHERE r.event_id = ? AND r.user_id = ?""", (event_id, g.user["id"]), one=True)
    if not attended:
        flash("Only students who attended can give feedback.", "error")
        return redirect(url_for("event_detail", event_id=event_id))
    if request.method == "POST":
        rating = request.form.get("rating", "")
        if not rating.isdigit() or not 1 <= int(rating) <= 5:
            flash("Choose a rating from 1 to 5.", "error")
        else:
            try:
                get_db().execute("INSERT INTO feedback (event_id, user_id, rating, comment) VALUES (?,?,?,?)",
                                 (event_id, g.user["id"], int(rating), request.form.get("comment", "").strip()))
                get_db().commit()
                flash("Thank you for your feedback.", "success")
            except sqlite3.IntegrityError:
                flash("You have already given feedback.", "error")
            return redirect(url_for("event_detail", event_id=event_id))
    return render_template("feedback.html", ev=ev)


# ---------------------------------------------------------------- organizer
@app.route("/organizer/events")
@role_required("organizer")
def organizer_events():
    rows = query("SELECT * FROM events WHERE organizer_id = ? ORDER BY event_date DESC", (g.user["id"],))
    return render_template("organizer_events.html", rows=[(r, event_stats(r["id"])) for r in rows])


@app.route("/organizer/events/new", methods=["GET", "POST"])
@role_required("organizer")
def event_new():
    data = {}
    if request.method == "POST":
        data, errors = validate_event_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            get_db().execute(
                """INSERT INTO events (title, type, description, event_date, venue, capacity, deadline,
                                       organizer_id, status) VALUES (?,?,?,?,?,?,?,?, 'pending')""",
                (data["title"], data["type"], data["description"], data["event_date"], data["venue"],
                 data["capacity"], data["deadline"], g.user["id"]))
            get_db().commit()
            flash("Event submitted for admin approval.", "success")
            return redirect(url_for("organizer_events"))
    return render_template("event_form.html", ev=data, editing=False)


@app.route("/organizer/events/<int:event_id>/edit", methods=["GET", "POST"])
@role_required("organizer")
def event_edit(event_id):
    ev = own_event_or_404(event_id)
    if request.method == "POST":
        data, errors = validate_event_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("event_form.html", ev=data, editing=True, event_id=event_id)
        key_change = (data["event_date"] != ev["event_date"] or data["capacity"] != ev["capacity"])
        status = "pending" if ev["status"] == "rejected" or (ev["status"] == "approved" and key_change) \
            else ev["status"]
        get_db().execute(
            """UPDATE events SET title=?, type=?, description=?, event_date=?, venue=?, capacity=?,
                                 deadline=?, status=?, reject_reason=NULL WHERE id=?""",
            (data["title"], data["type"], data["description"], data["event_date"], data["venue"],
             data["capacity"], data["deadline"], status, event_id))
        get_db().commit()
        flash("Event updated." + (" Sent for approval again." if status == "pending" else ""), "success")
        return redirect(url_for("organizer_events"))
    return render_template("event_form.html", ev=ev, editing=True, event_id=event_id)


@app.route("/organizer/events/<int:event_id>/scan")
@role_required("organizer", "admin")
def scan_page(event_id):
    ev = own_event_or_404(event_id)
    return render_template("scan.html", ev=ev, stats=event_stats(event_id))


@app.post("/attendance/scan")
@role_required("organizer", "admin")
def attendance_scan():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    try:
        event_id = int(data.get("event_id"))
    except (TypeError, ValueError):
        return jsonify(status="error", message="Event missing"), 400
    ev = own_event_or_404(event_id)
    reg = query("""SELECT r.id, r.event_id, u.name, u.department FROM registrations r
                   JOIN users u ON u.id = r.user_id WHERE r.qr_token = ?""", (token,), one=True)
    if reg is None or reg["event_id"] != ev["id"]:
        return jsonify(status="invalid", message="Invalid pass for this event"), 400
    try:
        get_db().execute("INSERT INTO attendance (reg_id, scan_time, scanned_by) VALUES (?,?,?)",
                         (reg["id"], now_str(), g.user["id"]))
        get_db().commit()
    except sqlite3.IntegrityError:
        return jsonify(status="duplicate", message=f"{reg['name']} is already marked"), 409
    stats = event_stats(event_id)
    return jsonify(status="ok", message=f"Present: {reg['name']} ({reg['department'] or 'N/A'})",
                   present=stats["present"], registered=stats["registered"])


@app.route("/events/<int:event_id>/report")
@role_required("organizer", "admin")
def report(event_id):
    ev = own_event_or_404(event_id)
    return render_template("report.html", ev=ev, stats=event_stats(event_id), rows=report_rows(event_id))


def report_rows(event_id):
    return query("""SELECT u.name, u.email, u.department, u.year, r.reg_date,
                           a.scan_time, f.rating, f.comment
                    FROM registrations r JOIN users u ON u.id = r.user_id
                    LEFT JOIN attendance a ON a.reg_id = r.id
                    LEFT JOIN feedback f ON f.event_id = r.event_id AND f.user_id = r.user_id
                    WHERE r.event_id = ? ORDER BY u.name""", (event_id,))


@app.route("/events/<int:event_id>/report.csv")
@role_required("organizer", "admin")
def report_csv(event_id):
    ev = own_event_or_404(event_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Email", "Department", "Year", "Registered on", "Status", "Scan time", "Rating", "Comment"])
    for r in report_rows(event_id):
        w.writerow([r["name"], r["email"], r["department"], r["year"], r["reg_date"],
                    "Present" if r["scan_time"] else "Absent", r["scan_time"] or "",
                    r["rating"] or "", r["comment"] or ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=event-{ev['id']}-report.csv"})


# ---------------------------------------------------------------- admin
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    totals = {
        "events": query("SELECT COUNT(*) c FROM events", one=True)["c"],
        "approved": query("SELECT COUNT(*) c FROM events WHERE status = 'approved'", one=True)["c"],
        "registrations": query("SELECT COUNT(*) c FROM registrations", one=True)["c"],
        "present": query("SELECT COUNT(*) c FROM attendance", one=True)["c"],
        "students": query("SELECT COUNT(*) c FROM users WHERE role = 'student'", one=True)["c"],
    }
    pending = query("""SELECT e.*, u.name AS organizer_name FROM events e
                       JOIN users u ON u.id = e.organizer_id WHERE e.status = 'pending'
                       ORDER BY e.created_at""")
    all_events = query("""SELECT e.*, u.name AS organizer_name FROM events e
                          JOIN users u ON u.id = e.organizer_id ORDER BY e.event_date DESC""")
    depts = query("""SELECT COALESCE(u.department, 'N/A') AS department,
                            COUNT(r.id) AS registered, COUNT(a.id) AS present
                     FROM registrations r JOIN users u ON u.id = r.user_id
                     LEFT JOIN attendance a ON a.reg_id = r.id
                     GROUP BY u.department ORDER BY registered DESC""")
    return render_template("admin_dashboard.html", totals=totals, pending=pending, depts=depts,
                           rows=[(e, event_stats(e["id"])) for e in all_events])


@app.post("/admin/events/<int:event_id>/<action>")
@role_required("admin")
def admin_event_action(event_id, action):
    ev = query("SELECT e.*, u.email AS org_email FROM events e JOIN users u ON u.id = e.organizer_id "
               "WHERE e.id = ?", (event_id,), one=True)
    if ev is None or action not in ("approve", "reject", "cancel"):
        abort(404)
    reason = request.form.get("reason", "").strip()
    new = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[action]
    get_db().execute("UPDATE events SET status = ?, reject_reason = ? WHERE id = ?",
                     (new, reason if action != "approve" else None, event_id))
    get_db().commit()
    send_email(ev["org_email"], f"Event {new}: {ev['title']}",
               f"Your event '{ev['title']}' was {new}." + (f"\nReason: {reason}" if reason else ""))
    flash(f"Event {new}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    return render_template("admin_users.html", users=query("SELECT * FROM users ORDER BY role, name"))


@app.post("/admin/users/<int:user_id>/role")
@role_required("admin")
def admin_user_role(user_id):
    role = request.form.get("role")
    if role not in ("student", "organizer", "admin") or user_id == g.user["id"]:
        abort(400)
    get_db().execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    get_db().commit()
    flash("Role updated.", "success")
    return redirect(url_for("admin_users"))


@app.cli.command("send-reminders")
def send_reminders():
    """Email everyone registered for events happening tomorrow (run daily)."""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = query("""SELECT e.title, e.event_date, e.venue, u.email, u.name
                    FROM events e JOIN registrations r ON r.event_id = e.id
                    JOIN users u ON u.id = r.user_id
                    WHERE e.status = 'approved' AND substr(e.event_date, 1, 10) = ?""", (tomorrow,))
    for r in rows:
        send_email(r["email"], f"Reminder: {r['title']} tomorrow",
                   f"Hi {r['name']}, {r['title']} is on {fmt_dt(r['event_date'])} at {r['venue']}. "
                   "Bring your QR pass.")
    print(f"{len(rows)} reminder(s) sent.")


if __name__ == "__main__":
    app.run(debug=True)
