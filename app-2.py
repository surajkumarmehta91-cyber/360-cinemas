import os, sqlite3, secrets, string, re
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
except ImportError:
    cloudinary = None
    CLOUDINARY_AVAILABLE = False

try:
    import psycopg2
    import psycopg2.extras
    POSTGRES_AVAILABLE = True
except ImportError:
    psycopg2 = None
    POSTGRES_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-this-secret-key")
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "cinema360.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
# Render Free has an ephemeral filesystem. Configure Cloudinary env vars there for durable uploads.
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "").strip()
if CLOUDINARY_AVAILABLE and CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL, secure=True)
elif CLOUDINARY_AVAILABLE and all(os.environ.get(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")):
    cloudinary.config(
        cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
        api_key=os.environ["CLOUDINARY_API_KEY"],
        api_secret=os.environ["CLOUDINARY_API_SECRET"],
        secure=True,
    )
ALLOWED = {"png", "jpg", "jpeg", "webp"}

class CompatCursor:
    """Small SQLite-style cursor adapter so existing routes can run on PostgreSQL."""
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class CompatConnection:
    def __init__(self, raw):
        self.raw = raw
        self.is_postgres = True

    def execute(self, sql, params=()):
        # SQLite PRAGMAs have no PostgreSQL equivalent.
        if sql.strip().upper().startswith("PRAGMA "):
            return CompatCursor(self.raw.cursor())
        query = sql.replace("?", "%s")
        lastrowid = None
        # Preserve the SQLite cursor.lastrowid behaviour used by existing routes.
        match = re.match(r"\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", query, re.I)
        table = match.group(1).lower() if match else ""
        if match and table in {"movies", "shows", "panoramas", "seat_positions", "users", "bookings", "messages"} and "RETURNING" not in query.upper():
            query = query.rstrip().rstrip(";") + " RETURNING id"
            cur = self.raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute(query, params or ())
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
            return CompatCursor(cur, lastrowid)
        cur = self.raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(query, params or ())
        return CompatCursor(cur, lastrowid)

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()


def db():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        if not POSTGRES_AVAILABLE:
            raise RuntimeError("DATABASE_URL is set but psycopg2-binary is not installed.")
        # Neon connection strings sometimes use postgres://; psycopg2 accepts postgresql://.
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        raw = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.DictCursor, connect_timeout=15)
        return CompatConnection(raw)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_db():
    """Create tables in the configured database. Existing records are never deleted."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        con = db()
        schema = """
        CREATE TABLE IF NOT EXISTS movies (
          id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
          poster TEXT DEFAULT '', active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS shows (
          id BIGSERIAL PRIMARY KEY, movie_id BIGINT NOT NULL, show_time TEXT NOT NULL,
          price INTEGER NOT NULL DEFAULT 250, active INTEGER DEFAULT 1,
          FOREIGN KEY(movie_id) REFERENCES movies(id)
        );
        CREATE TABLE IF NOT EXISTS panoramas (
          id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, image_path TEXT NOT NULL,
          active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS seat_positions (
          id BIGSERIAL PRIMARY KEY, panorama_id BIGINT NOT NULL, seat_number TEXT NOT NULL,
          yaw DOUBLE PRECISION NOT NULL DEFAULT 0, pitch DOUBLE PRECISION NOT NULL DEFAULT 0,
          hfov DOUBLE PRECISION NOT NULL DEFAULT 100, UNIQUE(panorama_id, seat_number),
          FOREIGN KEY(panorama_id) REFERENCES panoramas(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
          id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookings (
          id BIGSERIAL PRIMARY KEY, ticket_id TEXT UNIQUE, customer_name TEXT NOT NULL,
          contact TEXT NOT NULL, show_id BIGINT NOT NULL, seat_number TEXT NOT NULL,
          utr TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL,
          user_id BIGINT, cancel_requested INTEGER NOT NULL DEFAULT 0, cancel_reason TEXT DEFAULT '',
          FOREIGN KEY(show_id) REFERENCES shows(id), FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
          id BIGSERIAL PRIMARY KEY, booking_id BIGINT NOT NULL, body TEXT NOT NULL,
          created_at TEXT NOT NULL, sender TEXT NOT NULL DEFAULT 'admin',
          FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
        """
        for statement in schema.split(';'):
            if statement.strip():
                con.execute(statement)
        # Add columns for databases created by older app versions.
        con.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_id BIGINT")
        con.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancel_requested INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancel_reason TEXT DEFAULT ''")
        if con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            con.execute("INSERT INTO settings(key,value) VALUES('upi_id','') ON CONFLICT(key) DO NOTHING")
            con.execute("INSERT INTO settings(key,value) VALUES('qr_path','') ON CONFLICT(key) DO NOTHING")
        con.commit(); con.close()
        return

    con = db()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    con.executescript("""
    CREATE TABLE IF NOT EXISTS movies (
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT DEFAULT '',
      poster TEXT DEFAULT '', active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS shows (
      id INTEGER PRIMARY KEY AUTOINCREMENT, movie_id INTEGER NOT NULL, show_time TEXT NOT NULL,
      price INTEGER NOT NULL DEFAULT 250, active INTEGER DEFAULT 1,
      FOREIGN KEY(movie_id) REFERENCES movies(id)
    );
    CREATE TABLE IF NOT EXISTS panoramas (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, image_path TEXT NOT NULL,
      active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS seat_positions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, panorama_id INTEGER NOT NULL, seat_number TEXT NOT NULL,
      yaw REAL NOT NULL DEFAULT 0, pitch REAL NOT NULL DEFAULT 0, hfov REAL NOT NULL DEFAULT 100,
      UNIQUE(panorama_id, seat_number), FOREIGN KEY(panorama_id) REFERENCES panoramas(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, contact TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bookings (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT UNIQUE, customer_name TEXT NOT NULL,
      contact TEXT NOT NULL, show_id INTEGER NOT NULL, seat_number TEXT NOT NULL,
      utr TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL,
      user_id INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0, cancel_reason TEXT DEFAULT '',
      FOREIGN KEY(show_id) REFERENCES shows(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER NOT NULL, body TEXT NOT NULL,
      created_at TEXT NOT NULL, sender TEXT NOT NULL DEFAULT 'admin',
      FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
    """)
    booking_cols = {r["name"] for r in con.execute("PRAGMA table_info(bookings)").fetchall()}
    if "user_id" not in booking_cols:
        con.execute("ALTER TABLE bookings ADD COLUMN user_id INTEGER REFERENCES users(id)")
    if "cancel_requested" not in booking_cols:
        con.execute("ALTER TABLE bookings ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
    if "cancel_reason" not in booking_cols:
        con.execute("ALTER TABLE bookings ADD COLUMN cancel_reason TEXT DEFAULT ''")
    if con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
        con.execute("INSERT INTO settings(key,value) VALUES('upi_id','')")
        con.execute("INSERT INTO settings(key,value) VALUES('qr_path','')")
    con.commit(); con.close()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED

def save_upload(field):
    """Save images durably on Render via Cloudinary; use local storage for development."""
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    if not allowed_file(f.filename):
        flash("Please upload PNG, JPG, JPEG or WEBP image.", "error")
        return None

    name = secrets.token_hex(8) + "_" + secure_filename(f.filename)
    cloudinary_ready = bool(
        CLOUDINARY_AVAILABLE and (
            CLOUDINARY_URL or all(os.environ.get(k) for k in
            ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))
        )
    )
    if cloudinary_ready:
        try:
            result = cloudinary.uploader.upload(
                f,
                folder="cinema360",
                public_id=os.path.splitext(name)[0],
                resource_type="image",
                overwrite=False,
            )
            return result["secure_url"]
        except Exception as exc:
            app.logger.exception("Cloudinary image upload failed")
            flash("Image upload failed. Check Cloudinary settings and try again.", "error")
            return None

    # Local development fallback only. Render Free files are erased on restart/redeploy.
    if os.environ.get("RENDER"):
        flash("Permanent image storage is not configured. Add Cloudinary environment variables in Render first.", "error")
        return None
    f.save(os.path.join(UPLOAD_DIR, name))
    return "uploads/" + name


def image_src(path):
    """Return remote image URLs unchanged and resolve legacy local paths safely."""
    if not path:
        return ""
    path = str(path).strip()
    if path.startswith(("https://", "http://")):
        return path
    return url_for("static", filename=path.lstrip("/"))

def get_settings():
    con = db()
    rows = con.execute("SELECT key,value FROM settings").fetchall()
    con.close()
    return {r["key"]: r["value"] for r in rows}

@app.context_processor
def inject_globals():
    return {"site_name": "CINEMA 360", "admin_logged_in": bool(session.get("admin")),
                "current_user": session.get("user_name"), "user_logged_in": bool(session.get("user_id")),
                "image_src": image_src}

# USER ACCOUNT AND BOOKING HISTORY
@app.route("/account", methods=["GET", "POST"])
def account():
    if session.get("user_id"):
        return redirect(url_for("my_bookings"))
    mode = request.args.get("mode", "login")
    form_error = ""
    if request.method == "POST":
        mode = request.form.get("mode", "login")
        contact = request.form.get("contact", "").strip()
        password = request.form.get("password", "")
        con = db()
        if mode == "register":
            name = request.form.get("name", "").strip()
            if not name or not contact or len(password) < 6:
                form_error = "Name, mobile/contact and password (minimum 6 characters) are required."
            else:
                # Explicit duplicate check gives the user a clear error and never creates a second account.
                existing = con.execute("SELECT id FROM users WHERE lower(contact)=lower(?)", (contact,)).fetchone()
                if existing:
                    form_error = "This mobile/contact is already registered. Please log in instead."
                else:
                    try:
                        cur = con.execute(
                            "INSERT INTO users(name,contact,password_hash,created_at) VALUES(?,?,?,?)",
                            (name, contact, generate_password_hash(password), datetime.now().strftime("%Y-%m-%d %H:%M"))
                        )
                        con.commit()
                        session["user_id"] = cur.lastrowid
                        session["user_name"] = name
                        con.close()
                        flash("Account created successfully.", "success")
                        return redirect(url_for("my_bookings"))
                    except (sqlite3.IntegrityError, psycopg2.IntegrityError if POSTGRES_AVAILABLE else sqlite3.IntegrityError):
                        con.rollback()
                        form_error = "This mobile/contact is already registered. Please log in instead."
        else:
            user = con.execute("SELECT * FROM users WHERE lower(contact)=lower(?)", (contact,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                con.close()
                return redirect(url_for("my_bookings"))
            form_error = "Incorrect mobile/contact or password."
        con.close()
    return render_template("account.html", mode=mode, form_error=form_error)

@app.route("/account/logout")
def account_logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    return redirect(url_for("home"))

@app.route("/my-bookings")
def my_bookings():
    if not session.get("user_id"):
        flash("Please log in to view your bookings.", "error")
        return redirect(url_for("account"))
    con = db()
    bookings = con.execute("""SELECT b.*,s.show_time,s.price,m.title FROM bookings b
        JOIN shows s ON s.id=b.show_id JOIN movies m ON m.id=s.movie_id
        WHERE b.user_id=? ORDER BY b.id DESC""", (session["user_id"],)).fetchall()
    con.close()
    return render_template("my_bookings.html", bookings=bookings)

@app.route("/my-bookings/<int:booking_id>/cancel", methods=["POST"])
def request_cancel(booking_id):
    if not session.get("user_id"):
        flash("Please log in first.", "error")
        return redirect(url_for("account"))
    reason = request.form.get("reason", "").strip()[:500]
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (booking_id, session["user_id"])).fetchone()
    if not b:
        con.close(); abort(404)
    if b["status"] != "CONFIRMED" or b["cancel_requested"]:
        flash("Cancellation request is available only for confirmed tickets without an existing request.", "error")
    else:
        con.execute("UPDATE bookings SET cancel_requested=1,cancel_reason=? WHERE id=?", (reason, booking_id))
        con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                    (booking_id, "Cancellation request submitted. Your seat remains reserved until admin reviews it.", datetime.now().strftime("%Y-%m-%d %H:%M"), "customer"))
        con.commit()
        flash("Cancellation request sent. Seat will be released only after admin approval.", "success")
    con.close()
    return redirect(url_for("my_bookings"))

@app.route("/account/message/<int:booking_id>")
def account_ticket_redirect(booking_id):
    if not session.get("user_id"):
        return redirect(url_for("account"))
    con=db()
    row=con.execute("SELECT id FROM bookings WHERE id=? AND user_id=?", (booking_id,session["user_id"])).fetchone()
    con.close()
    if not row: abort(404)
    return redirect(url_for("ticket",booking_id=booking_id))

# PUBLIC WEBSITE AND BOOKING FLOW
@app.route("/")
def home():
    con = db()
    movies = con.execute("""SELECT m.*, s.id AS show_id, s.show_time, s.price
                            FROM movies m JOIN shows s ON s.movie_id=m.id
                            WHERE m.active=1 AND s.active=1 ORDER BY m.id DESC, s.show_time""").fetchall()
    panorama = con.execute("SELECT * FROM panoramas WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return render_template("index.html", movies=movies, panorama=panorama, settings=get_settings())

@app.route("/book/<int:show_id>", methods=["POST"])
def book(show_id):
    if not session.get("user_id"):
        flash("Create an account or log in before booking, so your tickets are saved.", "error")
        return redirect(url_for("account"))
    name = request.form.get("customer_name", "").strip()
    contact = request.form.get("contact", "").strip()
    seat = request.form.get("seat_number", "").strip().upper()
    if not name or not contact or not seat:
        flash("Name, contact and seat are required.", "error")
        return redirect(url_for("home"))
    con = db()
    show = con.execute("SELECT * FROM shows WHERE id=? AND active=1", (show_id,)).fetchone()
    if not show:
        con.close(); abort(404)
    # Prevent booking a seat already awaiting payment or confirmed for this show.
    occupied = con.execute("SELECT id FROM bookings WHERE show_id=? AND seat_number=? AND status IN ('PENDING','CONFIRMED')",
                           (show_id, seat)).fetchone()
    if occupied:
        con.close()
        flash("That seat is no longer available. Please choose another.", "error")
        return redirect(url_for("home"))
    cur = con.execute("""INSERT INTO bookings(customer_name,contact,show_id,seat_number,status,created_at,user_id)
                         VALUES(?,?,?,?,?,?,?)""",
                      (name, contact, show_id, seat, "PENDING", datetime.now().strftime("%Y-%m-%d %H:%M"), session["user_id"]))
    booking_id = cur.lastrowid
    con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                (booking_id, "Booking request received. Pay using the displayed QR and submit your UTR. Admin confirmation is required.", datetime.now().strftime("%Y-%m-%d %H:%M"), "system"))
    con.commit(); con.close()
    return redirect(url_for("payment", booking_id=booking_id))

@app.route("/payment/<int:booking_id>", methods=["GET", "POST"])
def payment(booking_id):
    con = db()
    booking = con.execute("""SELECT b.*, s.show_time,s.price,m.title FROM bookings b
                            JOIN shows s ON s.id=b.show_id JOIN movies m ON m.id=s.movie_id
                            WHERE b.id=?""", (booking_id,)).fetchone()
    if not booking:
        con.close(); abort(404)
    if not session.get("admin") and booking["user_id"] != session.get("user_id"):
        con.close()
        flash("Please log in with the account used for this booking.", "error")
        return redirect(url_for("account"))
    settings = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings").fetchall()}
    if request.method == "POST":
        utr = request.form.get("utr", "").strip()
        if len(utr) < 6:
            flash("Please enter a valid UTR/reference number.", "error")
        elif booking["status"] != "PENDING":
            flash("This booking is no longer awaiting payment.", "error")
        else:
            con.execute("UPDATE bookings SET utr=? WHERE id=?", (utr, booking_id))
            con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                        (booking_id, "UTR submitted. Your payment is awaiting admin verification.", datetime.now().strftime("%Y-%m-%d %H:%M"), "system"))
            con.commit()
            flash("UTR submitted. Your ticket will be confirmed after admin verification.", "success")
            booking = con.execute("""SELECT b.*, s.show_time,s.price,m.title FROM bookings b
                            JOIN shows s ON s.id=b.show_id JOIN movies m ON m.id=s.movie_id WHERE b.id=?""", (booking_id,)).fetchone()
    panorama = con.execute("SELECT * FROM panoramas WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return render_template("payment.html", booking=booking, settings=settings, panorama=panorama)

@app.route("/ticket/<int:booking_id>")
def ticket(booking_id):
    con = db()
    booking = con.execute("""SELECT b.*,s.show_time,s.price,m.title FROM bookings b
                            JOIN shows s ON s.id=b.show_id JOIN movies m ON m.id=s.movie_id WHERE b.id=?""", (booking_id,)).fetchone()
    messages = con.execute("SELECT * FROM messages WHERE booking_id=? ORDER BY id DESC", (booking_id,)).fetchall()
    panorama = con.execute("SELECT * FROM panoramas WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    if not booking: abort(404)
    if not session.get("admin") and booking["user_id"] != session.get("user_id"):
        flash("Log in with the account used for this booking to view its ticket.", "error")
        return redirect(url_for("account"))
    return render_template("ticket.html", booking=booking, messages=messages, panorama=panorama)


# API ENDPOINTS USED BY THE WEBSITE
@app.route("/api/seats/<int:show_id>")
def api_seats(show_id):
    con = db()
    rows = con.execute("""SELECT seat_number FROM bookings
                          WHERE show_id=? AND status IN ('PENDING','CONFIRMED')""", (show_id,)).fetchall()
    con.close()
    return {"occupied": [r["seat_number"] for r in rows]}

@app.route("/api/seat-position")
def api_seat_position():
    seat = request.args.get("seat", "").strip().upper()
    con = db()
    row = con.execute("""SELECT p.yaw,p.pitch,p.hfov FROM seat_positions p
                         JOIN panoramas a ON a.id=p.panorama_id
                         WHERE a.active=1 AND p.seat_number=? ORDER BY p.id DESC LIMIT 1""", (seat,)).fetchone()
    con.close()
    if not row:
        return {"found": False}
    return {"found": True, "yaw": row["yaw"], "pitch": row["pitch"], "hfov": row["hfov"]}

# ADMIN LOGIN AND DASHBOARD PAGES
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        # Change these credentials before public deployment.
        if request.form.get("username") == os.environ.get("ADMIN_USERNAME", "admin") and request.form.get("password") == os.environ.get("ADMIN_PASSWORD", "change-this-password"):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    con = db()
    stats = {
        "movies": con.execute("SELECT COUNT(*) FROM movies").fetchone()[0],
        "shows": con.execute("SELECT COUNT(*) FROM shows").fetchone()[0],
        "pending": con.execute("SELECT COUNT(*) FROM bookings WHERE status='PENDING'").fetchone()[0],
        "confirmed": con.execute("SELECT COUNT(*) FROM bookings WHERE status='CONFIRMED'").fetchone()[0],
    }
    bookings = con.execute("""SELECT b.*,s.show_time,m.title FROM bookings b JOIN shows s ON s.id=b.show_id
                               JOIN movies m ON m.id=s.movie_id ORDER BY b.id DESC LIMIT 100""").fetchall()
    movies = con.execute("SELECT * FROM movies ORDER BY id DESC").fetchall()
    shows = con.execute("SELECT s.*,m.title FROM shows s JOIN movies m ON m.id=s.movie_id ORDER BY s.id DESC").fetchall()
    panoramas = con.execute("SELECT * FROM panoramas ORDER BY id DESC").fetchall()
    seat_positions = con.execute("SELECT panorama_id,seat_number,yaw,pitch,hfov FROM seat_positions ORDER BY seat_number").fetchall()
    con.close()
    return render_template("admin.html", stats=stats, bookings=bookings, movies=movies, shows=shows, panoramas=panoramas, seat_positions=seat_positions, settings=get_settings())


@app.route("/admin/movies")
@admin_required
def admin_movies_page():
    con = db()
    movies = con.execute("SELECT * FROM movies ORDER BY id DESC").fetchall()
    shows = con.execute("SELECT s.*,m.title FROM shows s JOIN movies m ON m.id=s.movie_id ORDER BY s.id DESC").fetchall()
    con.close()
    return render_template("admin_movies.html", movies=movies, shows=shows)

@app.route("/admin/payments")
@admin_required
def admin_payments_page():
    return render_template("admin_payments.html", settings=get_settings())

@app.route("/admin/bookings")
@admin_required
def admin_bookings_page():
    con = db()
    bookings = con.execute("""SELECT b.*,s.show_time,m.title FROM bookings b JOIN shows s ON s.id=b.show_id
                               JOIN movies m ON m.id=s.movie_id ORDER BY b.id DESC LIMIT 200""").fetchall()
    stats = {
        "pending": con.execute("SELECT COUNT(*) FROM bookings WHERE status='PENDING'").fetchone()[0],
        "confirmed": con.execute("SELECT COUNT(*) FROM bookings WHERE status='CONFIRMED'").fetchone()[0],
        "rejected": con.execute("SELECT COUNT(*) FROM bookings WHERE status='REJECTED'").fetchone()[0],
    }
    con.close()
    return render_template("admin_bookings.html", bookings=bookings, stats=stats)

# ADMIN: PANORAMA AND SEAT POSITION SETTINGS
@app.route("/admin/panorama", methods=["POST"])
@admin_required
def admin_panorama():
    name = request.form.get("name", "Main Cinema").strip() or "Main Cinema"
    image = save_upload("panorama")
    if image:
        con = db()
        con.execute("UPDATE panoramas SET active=0")
        con.execute("INSERT INTO panoramas(name,image_path,active) VALUES(?,?,1)", (name, image))
        con.commit(); con.close()
        flash("360° image uploaded.", "success")
    return redirect(url_for("admin_dashboard") + "#panorama")

@app.route("/admin/seat-position", methods=["POST"])
@admin_required
def admin_seat_position():
    panorama_id = request.form.get("panorama_id", type=int)
    seat = request.form.get("seat_number", "").strip().upper()
    yaw = request.form.get("yaw", type=float)
    pitch = request.form.get("pitch", type=float)
    hfov = request.form.get("hfov", type=float)
    if not panorama_id or not seat or yaw is None or pitch is None or hfov is None:
        flash("Select a panorama, seat number and pick a position first.", "error")
        return redirect(url_for("admin_dashboard") + "#panorama")
    con = db()
    con.execute("""INSERT INTO seat_positions(panorama_id,seat_number,yaw,pitch,hfov) VALUES(?,?,?,?,?)
                   ON CONFLICT(panorama_id,seat_number) DO UPDATE SET yaw=excluded.yaw,pitch=excluded.pitch,hfov=excluded.hfov""",
                (panorama_id, seat, yaw, pitch, hfov))
    con.commit(); con.close()
    flash(f"Position for seat {seat} saved.", "success")
    return redirect(url_for("admin_dashboard") + "#panorama")



@app.route("/admin/panorama/edit/<int:panorama_id>", methods=["POST"])
@admin_required
def admin_panorama_edit(panorama_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Panorama name is required.", "error")
        return redirect(url_for("admin_dashboard") + "#panorama")

    con = db()
    current = con.execute("SELECT * FROM panoramas WHERE id=?", (panorama_id,)).fetchone()
    if not current:
        con.close()
        flash("Panorama not found.", "error")
        return redirect(url_for("admin_dashboard") + "#panorama")

    new_image = save_upload("panorama")
    if new_image:
        con.execute("UPDATE panoramas SET name=?, image_path=? WHERE id=?",
                    (name, new_image, panorama_id))
    else:
        con.execute("UPDATE panoramas SET name=? WHERE id=?", (name, panorama_id))
    con.commit()
    con.close()
    flash("Panorama updated successfully.", "success")
    return redirect(url_for("admin_dashboard") + "#panorama")


@app.route("/admin/panorama/delete/<int:panorama_id>", methods=["POST"])
@admin_required
def admin_panorama_delete(panorama_id):
    con = db()
    current = con.execute("SELECT * FROM panoramas WHERE id=?", (panorama_id,)).fetchone()
    if not current:
        con.close()
        flash("Panorama not found.", "error")
        return redirect(url_for("admin_dashboard") + "#panorama")

    # Remove saved seat positions tied to this panorama before deleting it.
    con.execute("DELETE FROM seat_positions WHERE panorama_id=?", (panorama_id,))
    con.execute("DELETE FROM panoramas WHERE id=?", (panorama_id,))
    remaining = con.execute("SELECT id FROM panoramas ORDER BY id DESC LIMIT 1").fetchone()
    if remaining:
        con.execute("UPDATE panoramas SET active=0")
        con.execute("UPDATE panoramas SET active=1 WHERE id=?", (remaining["id"],))
    con.commit()
    con.close()

    # Remove the uploaded file only if it lives in this app's uploads directory.
    image_path = current["image_path"] or ""
    if image_path.startswith("uploads/"):
        full_path = os.path.join(UPLOAD_DIR, os.path.basename(image_path))
        if os.path.isfile(full_path):
            try:
                os.remove(full_path)
            except OSError:
                pass
    flash("Panorama deleted.", "success")
    return redirect(url_for("admin_dashboard") + "#panorama")

# ADMIN: MOVIES AND SHOWTIMES
@app.route("/admin/movie", methods=["POST"])
@admin_required
def admin_movie():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    poster = save_upload("poster") or ""
    if title:
        con = db()
        cur = con.execute("INSERT INTO movies(title,description,poster) VALUES(?,?,?)", (title, description, poster))
        show_time = request.form.get("show_time", "18:00").strip() or "18:00"
        price = request.form.get("price", 250, type=int) or 250
        con.execute("INSERT INTO shows(movie_id,show_time,price) VALUES(?,?,?)", (cur.lastrowid, show_time, max(1, price)))
        con.commit(); con.close()
        flash("Movie and first show added.", "success")
    else:
        flash("Movie title is required.", "error")
    return redirect(url_for("admin_movies_page"))

@app.route("/admin/movie/<int:movie_id>/edit", methods=["POST"])
@admin_required
def admin_movie_edit(movie_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    poster = save_upload("poster")
    if not title:
        flash("Movie title is required.", "error")
        return redirect(url_for("admin_movies_page"))
    con = db()
    movie = con.execute("SELECT id FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not movie:
        con.close(); abort(404)
    if poster:
        con.execute("UPDATE movies SET title=?,description=?,poster=? WHERE id=?", (title, description, poster, movie_id))
    else:
        con.execute("UPDATE movies SET title=?,description=? WHERE id=?", (title, description, movie_id))
    con.commit(); con.close()
    flash("Movie details updated successfully.", "success")
    return redirect(url_for("admin_movies_page"))

@app.route("/admin/movie/<int:movie_id>/delete", methods=["POST"])
@admin_required
def admin_movie_delete(movie_id):
    con = db()
    movie = con.execute("SELECT id,title FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not movie:
        con.close()
        flash("Movie not found.", "error")
        return redirect(url_for("admin_movies_page"))
    show_ids = [r[0] for r in con.execute("SELECT id FROM shows WHERE movie_id=?", (movie_id,)).fetchall()]
    if show_ids:
        placeholders = ",".join("?" for _ in show_ids)
        bookings_count = con.execute(f"SELECT COUNT(*) FROM bookings WHERE show_id IN ({placeholders})", show_ids).fetchone()[0]
    else:
        bookings_count = 0
    if bookings_count:
        con.close()
        flash("This movie has bookings, so it cannot be deleted. Cancel/resolve its bookings first to protect customer records.", "error")
        return redirect(url_for("admin_movies_page"))
    con.execute("DELETE FROM shows WHERE movie_id=?", (movie_id,))
    con.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    con.commit()
    con.close()
    flash(f"Movie '{movie['title']}' and its showtimes deleted.", "success")
    return redirect(url_for("admin_movies_page"))

@app.route("/admin/show/<int:show_id>/edit", methods=["POST"])
@admin_required
def admin_show_edit(show_id):
    show_time = request.form.get("show_time", "").strip()
    price = request.form.get("price", 250, type=int) or 250
    if not show_time or price < 1:
        flash("Enter a valid showtime and ticket price.", "error")
        return redirect(url_for("admin_movies_page"))
    con = db()
    cur = con.execute("UPDATE shows SET show_time=?,price=? WHERE id=?", (show_time, price, show_id))
    con.commit(); con.close()
    flash("Showtime and price updated.", "success" if cur.rowcount else "Showtime not found.")
    return redirect(url_for("admin_movies_page"))

@app.route("/admin/show", methods=["POST"])
@admin_required
def admin_show():
    movie_id = request.form.get("movie_id", type=int)
    show_time = request.form.get("show_time", "").strip()
    price = request.form.get("price", 250, type=int) or 250
    if movie_id and show_time:
        con = db(); con.execute("INSERT INTO shows(movie_id,show_time,price) VALUES(?,?,?)", (movie_id, show_time, max(1, price))); con.commit(); con.close()
        flash("Showtime added.", "success")
    return redirect(url_for("admin_movies_page"))

# ADMIN: PAYMENT APPROVAL, CANCELLATIONS, AND MESSAGES
@app.route("/admin/booking/<int:booking_id>/approve", methods=["POST"])
@admin_required
def approve_booking(booking_id):
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not b:
        con.close(); abort(404)
    if b["status"] == "PENDING":
        ticket_id = "CX360-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        con.execute("UPDATE bookings SET status='CONFIRMED',ticket_id=? WHERE id=?", (ticket_id, booking_id))
        con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                    (booking_id, f"Payment approved! Your ticket is confirmed. Ticket ID: {ticket_id}. Seat: {b['seat_number']}. Show your ticket at entry.", datetime.now().strftime("%Y-%m-%d %H:%M"), "admin"))
        con.commit()
        flash(f"Payment approved. Ticket ID: {ticket_id}", "success")
    con.close()
    return redirect(url_for("admin_bookings_page"))

@app.route("/admin/booking/<int:booking_id>/reject", methods=["POST"])
@admin_required
def reject_booking(booking_id):
    reason = request.form.get("reason", "Payment could not be verified. Please contact the cinema.").strip()
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b and b["status"] == "PENDING":
        con.execute("UPDATE bookings SET status='REJECTED' WHERE id=?", (booking_id,))
        con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                    (booking_id, reason, datetime.now().strftime("%Y-%m-%d %H:%M"), "admin"))
        con.commit()
    con.close()
    flash("Booking rejected and message saved.", "success")
    return redirect(url_for("admin_bookings_page"))

@app.route("/admin/booking/<int:booking_id>/cancel/approve", methods=["POST"])
@admin_required
def approve_cancellation(booking_id):
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not b:
        con.close(); abort(404)
    if b["cancel_requested"] and b["status"] == "CONFIRMED":
        con.execute("UPDATE bookings SET status='CANCELLED',cancel_requested=0 WHERE id=?", (booking_id,))
        con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                    (booking_id, "Your cancellation request was approved. Seat " + b["seat_number"] + " is now released.", datetime.now().strftime("%Y-%m-%d %H:%M"), "admin"))
        con.commit()
        flash("Cancellation approved. Seat released and available again.", "success")
    else:
        flash("No pending cancellation request found.", "error")
    con.close()
    return redirect(url_for("admin_bookings_page"))

@app.route("/admin/booking/<int:booking_id>/cancel/reject", methods=["POST"])
@admin_required
def reject_cancellation(booking_id):
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not b:
        con.close(); abort(404)
    if b["cancel_requested"]:
        con.execute("UPDATE bookings SET cancel_requested=0,cancel_reason='' WHERE id=?", (booking_id,))
        con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                    (booking_id, "Your cancellation request was declined. Your ticket remains confirmed.", datetime.now().strftime("%Y-%m-%d %H:%M"), "admin"))
        con.commit()
        flash("Cancellation request declined; seat remains booked.", "success")
    con.close()
    return redirect(url_for("admin_bookings_page"))

@app.route("/admin/booking/<int:booking_id>/message", methods=["POST"])
@admin_required
def send_message(booking_id):
    body = request.form.get("body", "").strip()
    if body:
        con = db()
        exists = con.execute("SELECT id FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if exists:
            con.execute("INSERT INTO messages(booking_id,body,created_at,sender) VALUES(?,?,?,?)",
                        (booking_id, body, datetime.now().strftime("%Y-%m-%d %H:%M"), "admin"))
            con.commit()
        con.close()
    flash("Message saved to booking notifications.", "success")
    return redirect(url_for("admin_bookings_page"))

# ADMIN: GENERAL SETTINGS
@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    upi = request.form.get("upi_id", "").strip()
    qr = save_upload("qr") 
    con = db()
    con.execute("INSERT INTO settings(key,value) VALUES('upi_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (upi,))
    if qr:
        con.execute("INSERT INTO settings(key,value) VALUES('qr_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (qr,))
    con.commit(); con.close()
    flash("Payment settings saved.", "success")
    return redirect(url_for("admin_payments_page"))

try:
    init_db()
except Exception:
    app.logger.exception("Database initialization failed")
    raise

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
