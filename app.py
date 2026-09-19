import csv
import io
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import date, datetime
from functools import lru_cache, wraps
from urllib.parse import urlparse

import gspread
import requests
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
import features

app = Flask(__name__)

# Vercel packages application code on a read-only filesystem. Its only writable
# location is /tmp, so runtime state must live there instead of beside app.py.
IS_VERCEL = bool(os.getenv("VERCEL"))
runtime_path = os.getenv(
    "WILDTRACK_RUNTIME_DIR",
    os.path.join("/tmp", "wildtrack") if IS_VERCEL else app.instance_path,
)
os.makedirs(runtime_path, exist_ok=True)

environment_secret = os.getenv("FLASK_SECRET_KEY")
if environment_secret:
    local_secret = environment_secret
elif IS_VERCEL:
    # Keep a missing environment variable from crashing the deployment. This
    # fallback is intentionally ephemeral; production should set the variable.
    local_secret = secrets.token_hex(32)
else:
    secret_path = os.path.join(runtime_path, "session-secret")
    try:
        with open(secret_path, "x", encoding="utf-8") as secret_file:
            secret_file.write(secrets.token_hex(32))
        os.chmod(secret_path, 0o600)
    except FileExistsError:
        pass
    with open(secret_path, encoding="utf-8") as secret_file:
        local_secret = secret_file.read().strip()

app.config.update(
    SECRET_KEY=local_secret,
    DATABASE=os.getenv(
        "WILDTRACK_DATABASE",
        os.path.join(runtime_path, "wildtrack.db")
        if IS_VERCEL
        else os.path.join(app.root_path, "data", "wildtrack.db"),
    ),
    UPLOAD_FOLDER=os.getenv("WILDTRACK_UPLOAD_FOLDER", os.path.join(runtime_path, "uploads")),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_VERCEL,
)

SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID", "1GimbfdLW2aQtIhUWw1lXM4JJ4ZBALlzQldT-o6FhFzE"
).strip()
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Species").strip()
SHEET_RANGE = os.getenv("GOOGLE_SHEET_RANGE", "A4:W").strip()
SHEET_PUBLIC = os.getenv("GOOGLE_SHEET_PUBLIC", "true").strip().lower() in {"1", "true", "yes"}
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json").strip()
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

_cache = {"time": 0, "data": []}
CACHE_SECONDS = 120
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,24}$")

RISK = {
    "Critically Endangered": 5,
    "Endangered": 4,
    "Vulnerable": 3,
    "Near Threatened": 2,
    "Least Concern": 1,
    "Data Deficient": 0,
}

STATUS_CODE = {
    "Critically Endangered": "CR",
    "Endangered": "EN",
    "Vulnerable": "VU",
    "Near Threatened": "NT",
    "Least Concern": "LC",
    "Data Deficient": "DD",
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db():
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            email TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            species_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, species_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species_id TEXT NOT NULL,
            location TEXT NOT NULL,
            observed_on TEXT NOT NULL,
            notes TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    features.migrate(connection)
    connection.close()


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = get_db().execute(
        "SELECT id, username, email, created_at, is_admin, disabled, notify_species, notify_observations, notify_events FROM users WHERE id = ?", (user_id,)
    ).fetchone() if user_id else None
    if g.user and g.user["disabled"]:
        session.pop("user_id", None)
        g.user = None


@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(24)
    return {"csrf_token": session["csrf_token"], "today": date.today().isoformat()}


def validate_csrf():
    submitted = request.form.get("csrf_token", "")
    if not submitted or not secrets.compare_digest(submitted, session.get("csrf_token", "")):
        abort(400, "The form expired. Refresh the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("Log in to use your personal conservation workspace.", "info")
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def safe_next_url(value):
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c)<32 for c in value):
        return None
    return value


def _normalise(row):
    """Make Google-Sheet or CSV rows consistent for the templates."""
    cleaned = {str(k).strip(): v.strip() if isinstance(v,str) else v for k, v in row.items()}
    try:
        cleaned["Risk_Score"] = int(float(cleaned.get("Risk_Score") or RISK.get(cleaned.get("IUCN_Status"), 0)))
    except (TypeError, ValueError, OverflowError):
        cleaned["Risk_Score"] = RISK.get(cleaned.get("IUCN_Status"), 0)
    for field in ("IUCN_Source", "Wikipedia_Source"):
        value = cleaned.get(field, "")
        if not isinstance(value,str) or urlparse(value).scheme not in {"https","http"}:
            cleaned[field] = ""
    cleaned["Status_Code"] = STATUS_CODE.get(cleaned.get("IUCN_Status"), "")
    for field, limit in (("Map_Latitude", 90), ("Map_Longitude", 180)):
        try:
            coordinate = float(cleaned.get(field))
            cleaned[field] = coordinate if math.isfinite(coordinate) and abs(coordinate) <= limit else None
        except (TypeError, ValueError):
            cleaned[field] = None
    return cleaned


def _load_from_google_sheets():
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is not set.")

    if SHEET_PUBLIC:
        response = requests.get(
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq",
            params={"tqx": "out:csv", "sheet": SHEET_NAME, "range": SHEET_RANGE},
            headers={"User-Agent": "WildTrack educational conservation app/1.0"},
            timeout=12,
        )
        response.raise_for_status()
        records = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
        required_headers = {"Species_ID", "Common_Name"}
        if not records.fieldnames or not required_headers.issubset(records.fieldnames):
            raise RuntimeError("The Google Sheet header row does not match the WildTrack data model.")
        data = [_normalise(row) for row in records if row.get("Species_ID")]
        if not data:
            raise RuntimeError("The Google Sheet did not return any species rows.")
        return data

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(f"Missing {SERVICE_ACCOUNT_FILE}.")
    client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    worksheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    return [_normalise(row) for row in worksheet.get_all_records(head=4) if row.get("Species_ID")]


def _load_from_csv():
    path = os.path.join(app.root_path, "data", "species_sample.csv")
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return [_normalise(row) for row in csv.DictReader(file)]


def load_species(force=False):
    """Read from Google Sheets, with a short cache to avoid unnecessary API calls."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["time"] < CACHE_SECONDS:
        return features.apply_edits(_cache["data"])
    try:
        data = _load_from_google_sheets()
        source = "Google Sheets · Live"
    except Exception as exc:
        app.logger.info("Google Sheets unavailable; using local sample data: %s", exc)
        data = _load_from_csv()
        source = "Local CSV sample"
    _cache.update(time=now, data=data, source=source)
    if source.startswith("Google Sheets"):
        features.sync_changes(data)
    return features.apply_edits(data)


def find_species(species_id):
    return next((item for item in load_species() if item.get("Species_ID") == species_id), None)


@lru_cache(maxsize=256)
def wikipedia_image(scientific_name):
    """Fetch a Wikipedia thumbnail and fall back to a neutral placeholder."""
    try:
        response = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": scientific_name,
                "gsrlimit": 1,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 900,
                "format": "json",
            },
            headers={"User-Agent": "WildTrack educational conservation app/1.0"},
            timeout=5,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if page.get("thumbnail", {}).get("source"):
                return page["thumbnail"]["source"]
    except Exception:
        pass
    return "https://placehold.co/900x600/153d32/e4f3eb?text=WildTrack"


@app.route("/")
def index():
    species = load_species()
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "").strip()
    animal_class = request.args.get("class", "").strip()
    nepal = request.args.get("nepal", "").strip()
    trend = request.args.get("trend", "").strip()
    sort = request.args.get("sort", "risk").strip()
    filtered = species
    if q:
        filtered = [
            item for item in filtered
            if any(q in str(item.get(field, "")).lower() for field in (
                "Common_Name", "Scientific_Name", "Native_Region", "Countries", "Habitat"
            ))
        ]
    if status:
        filtered = [item for item in filtered if item.get("IUCN_Status") == status]
    if animal_class:
        filtered = [item for item in filtered if item.get("Class") == animal_class]
    if nepal == "yes":
        filtered = [item for item in filtered if item.get("Nepal_Relevance") == "Yes"]
    if trend:
        filtered = [item for item in filtered if item.get("Population_Trend") == trend]
    if sort == "name":
        filtered = sorted(filtered, key=lambda item: item.get("Common_Name", ""))
    elif sort == "region":
        filtered = sorted(filtered, key=lambda item: (item.get("Native_Region", ""), item.get("Common_Name", "")))
    else:
        filtered = sorted(filtered, key=lambda item: (-item.get("Risk_Score", 0), item.get("Common_Name", "")))
    statuses = sorted({item.get("IUCN_Status") for item in species if item.get("IUCN_Status")})
    classes = sorted({item.get("Class") for item in species if item.get("Class")})
    trends = sorted({item.get("Population_Trend") for item in species if item.get("Population_Trend")})
    stats = {
        "total": len(species),
        "cr": sum(item.get("IUCN_Status") == "Critically Endangered" for item in species),
        "en": sum(item.get("IUCN_Status") == "Endangered" for item in species),
        "vu": sum(item.get("IUCN_Status") == "Vulnerable" for item in species),
        "decreasing": sum(item.get("Population_Trend") == "Decreasing" for item in species),
        "nepal": sum(item.get("Nepal_Relevance") == "Yes" for item in species),
    }
    chart_data = {
        "labels": ["Critically Endangered", "Endangered", "Vulnerable"],
        "values": [stats["cr"], stats["en"], stats["vu"]],
    }
    favorite_ids = set()
    if g.user:
        favorite_ids = {
            row["species_id"] for row in get_db().execute(
                "SELECT species_id FROM favorites WHERE user_id = ?", (g.user["id"],)
            ).fetchall()
        }
    return render_template(
        "index.html", species=filtered, all_species=species, stats=stats, statuses=statuses,
        classes=classes, trends=trends, chart_data=chart_data, favorite_ids=favorite_ids,
        source=_cache.get("source", ""), data_source_url=SHEET_URL,
        featured=features.featured_species(species),
    )


@app.route("/species/<species_id>")
def species_detail(species_id):
    item = find_species(species_id)
    if not item:
        abort(404)
    item = dict(item)
    item["Image_URL"] = wikipedia_image(item.get("Scientific_Name", ""))
    is_favorite = False
    if g.user:
        is_favorite = get_db().execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND species_id = ?", (g.user["id"], species_id)
        ).fetchone() is not None
    observations = get_db().execute(
        """SELECT o.*, u.username
           FROM observations o JOIN users u ON u.id = o.user_id
           WHERE o.species_id = ? AND o.status='approved' AND u.disabled=0 ORDER BY o.observed_on DESC, o.id DESC LIMIT 6""",
        (species_id,),
    ).fetchall()
    observations = [features.public_observation(row,item) for row in observations]
    return render_template("species.html", s=item, is_favorite=is_favorite, observations=observations)


@app.route("/register", methods=("GET", "POST"))
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        error = None
        if not USERNAME_PATTERN.fullmatch(username):
            error = "Username must be 3–24 characters using letters, numbers, or underscores."
        elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            error = "Enter a valid email address."
        elif not 8 <= len(password) <= 200:
            error = "Password must be 8–200 characters."
        elif password != confirm:
            error = "Passwords do not match."
        if error is None:
            try:
                cursor = get_db().execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                    (username, email, generate_password_hash(password, method="pbkdf2:sha256:600000")),
                )
                get_db().commit()
                language = session.get("lang", "en")
                session.clear()
                session["lang"] = language
                session["user_id"] = cursor.lastrowid
                flash("Welcome to WildTrack. Your conservation workspace is ready.", "success")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                error = "That username or email is already registered."
        flash(error, "error")
    return render_template("auth/register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        validate_csrf()
        identity = request.form.get("identity", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (identity, identity),
        ).fetchone()
        if len(password)>200 or user is None or user["disabled"] or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username, email, or password.", "error")
        else:
            language = session.get("lang", "en")
            session.clear()
            session["lang"] = language
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['username']}.", "success")
            return redirect(safe_next_url(request.form.get("next")) or url_for("dashboard"))
    return render_template("auth/login.html", next_url=safe_next_url(request.args.get("next")))


@app.post("/logout")
def logout():
    validate_csrf()
    language = session.get("lang", "en")
    session.clear()
    session["lang"] = language
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.post("/species/<species_id>/favorite")
@login_required
def toggle_favorite(species_id):
    validate_csrf()
    item = find_species(species_id)
    if not item:
        abort(404)
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND species_id = ?", (g.user["id"], species_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM favorites WHERE user_id = ? AND species_id = ?", (g.user["id"], species_id))
        message = f"{item['Common_Name']} removed from your watchlist."
    else:
        db.execute("INSERT INTO favorites (user_id, species_id) VALUES (?, ?)", (g.user["id"], species_id))
        message = f"{item['Common_Name']} added to your watchlist."
    db.commit()
    flash(message, "success")
    return redirect(safe_next_url(request.form.get("next")) or url_for("species_detail", species_id=species_id))


@app.route("/dashboard")
@login_required
def dashboard():
    species_by_id = {item["Species_ID"]: item for item in load_species()}
    favorite_rows = get_db().execute(
        "SELECT species_id, created_at FROM favorites WHERE user_id = ? ORDER BY created_at DESC", (g.user["id"],)
    ).fetchall()
    favorites = [species_by_id[row["species_id"]] for row in favorite_rows if row["species_id"] in species_by_id]
    observations = get_db().execute(
        """SELECT *
           FROM observations WHERE user_id = ? ORDER BY observed_on DESC, id DESC""",
        (g.user["id"],),
    ).fetchall()
    observation_cards = [dict(row) | {"species": species_by_id.get(row["species_id"])} for row in observations]
    return render_template("dashboard.html", favorites=favorites, observations=observation_cards, progress=features.progress(g.user["id"]))


@app.route("/observations/new", methods=("GET", "POST"))
@login_required
def report_observation():
    species = sorted(load_species(), key=lambda item: item["Common_Name"])
    selected_species = request.args.get("species", "")
    if request.method == "POST":
        validate_csrf()
        species_id = request.form.get("species_id", "").strip()
        location = request.form.get("location", "").strip()
        observed_on = request.form.get("observed_on", "").strip()
        notes = request.form.get("notes", "").strip()
        category = request.form.get("category", "Sighting")
        status = "pending" if request.form.get("share") else "private"
        item = find_species(species_id)
        sensitive = not request.form.get("public_location") or bool(item and item.get("IUCN_Status")=="Critically Endangered")
        latitude, longitude = None, None
        error = None
        if not item:
            error = "Choose a species from the database."
        elif category not in features.CATEGORIES:
            error = "Choose a valid observation category."
        elif not location or len(location) > 120:
            error = "Location is required and must be under 120 characters."
        else:
            try:
                observed_date = datetime.strptime(observed_on, "%Y-%m-%d").date()
                if observed_date > date.today():
                    error = "Observation date cannot be in the future."
            except ValueError:
                error = "Enter a valid observation date."
        if not notes or len(notes) > 600:
            error = error or "Notes are required and must be under 600 characters."
        if request.form.get("latitude") or request.form.get("longitude"):
            try:
                lat = float(request.form.get("latitude", ""))
                lng = float(request.form.get("longitude", ""))
                if not (-90<=lat<=90 and -180<=lng<=180):
                    raise ValueError()
                # Store only whole-degree grid centers. Never persist exact coordinates.
                latitude, longitude = round(lat), round(lng)
            except ValueError:
                error = "Enter both valid latitude and longitude, or leave both blank."
        photo = None
        if not error:
            try:
                photo = features.photo_upload(request.files.get("photo"))
            except ValueError as exc:
                error = str(exc)
        if error:
            flash(error, "error")
            selected_species = species_id
        else:
            get_db().execute(
                """INSERT INTO observations (user_id, species_id, location, observed_on, notes, category,status,photo,latitude,longitude,sensitive)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.user["id"], species_id, location, observed_on, notes,category,status,photo,latitude,longitude,int(sensitive)),
            )
            get_db().commit()
            flash("Observation saved. " + ("Submitted for community review." if status=="pending" else "Only you and project administrators can see this entry."), "success")
            return redirect(url_for("dashboard"))
    return render_template("report_observation.html", species=species, selected_species=selected_species, categories=features.CATEGORIES)


@app.post("/observations/<int:observation_id>/delete")
@login_required
def delete_observation(observation_id):
    validate_csrf()
    cursor = get_db().execute(
        "DELETE FROM observations WHERE id = ? AND user_id = ?", (observation_id, g.user["id"])
    )
    get_db().commit()
    flash("Observation removed." if cursor.rowcount else "Observation not found.", "info")
    return redirect(url_for("dashboard"))


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/api/species")
def species_api():
    return jsonify(load_species())


@app.route("/api/species/<species_id>")
def species_api_detail(species_id):
    item = find_species(species_id)
    if not item:
        return jsonify({"error": "Species not found"}), 404
    return jsonify(item)


@app.route("/api/data-status")
def data_status():
    load_species()
    source = _cache.get("source", "")
    return jsonify({
        "connected": source.startswith("Google Sheets"),
        "source": source,
        "sheet": SHEET_NAME,
        "records": len(_cache.get("data", [])),
        "cache_seconds": CACHE_SECONDS,
    })


@app.post("/refresh")
def refresh():
    validate_csrf()
    if not g.user or not g.user["is_admin"]:
        abort(403)
    load_species(force=True)
    flash(f"Species data refreshed from {_cache.get('source')}.", "success")
    return redirect(url_for("hub.admin"))


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(400)
def bad_request(error):
    return render_template("400.html", message=getattr(error, "description", "Invalid request.")), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("hub/error.html", code=403, message="Administrator access is required for this action."), 403


@app.errorhandler(413)
def too_large(_error):
    return render_template("hub/error.html", code=413, message="Upload is too large. Choose a photo under 5 MB."), 413


features.install(app, sys.modules[__name__])
init_db()


if __name__ == "__main__":
    app.run(debug=True)
