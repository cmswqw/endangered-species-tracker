"""Conservation tools, moderation, learning, and persistent member features."""
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import time
import warnings
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from urllib.parse import urlparse
from xml.etree import ElementTree

import click
import requests
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from content import ACTIONS, LESSONS, NE, ORGANIZATIONS, QUIZ, SOURCES

bp = Blueprint("hub", __name__)
core = None
NEWS_URL = "https://news.mongabay.com/feed/"
news_cache = {"time": 0, "items": [], "error": False}
CATEGORIES = ("Sighting", "Tracks", "Habitat note", "Other")
FIELDS = ("Common_Name", "Scientific_Name", "Class", "Order", "Family", "IUCN_Status", "Population_Trend", "Native_Region", "Countries", "Habitat", "Diet", "Primary_Threats", "Conservation_Actions", "Key_Fact", "Nepal_Relevance", "Map_Latitude", "Map_Longitude", "IUCN_Source", "Wikipedia_Source", "Last_Reviewed")


def migrate(connection):
    """Additive migration: existing accounts, watchlists, and journal entries survive."""
    columns = {
        "users": {"is_admin": "INTEGER NOT NULL DEFAULT 0", "disabled": "INTEGER NOT NULL DEFAULT 0", "notify_species": "INTEGER NOT NULL DEFAULT 1", "notify_observations": "INTEGER NOT NULL DEFAULT 1", "notify_events": "INTEGER NOT NULL DEFAULT 1"},
        "observations": {"status": "TEXT NOT NULL DEFAULT 'private'", "category": "TEXT NOT NULL DEFAULT 'Sighting'", "photo": "TEXT", "latitude": "REAL", "longitude": "REAL", "sensitive": "INTEGER NOT NULL DEFAULT 1", "review_note": "TEXT NOT NULL DEFAULT ''"},
    }
    for table, additions in columns.items():
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            questions TEXT NOT NULL, answers TEXT, score INTEGER, total INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pledges (
            user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
            PRIMARY KEY(user_id, action)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            event_key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, event_key)
        );
        CREATE TABLE IF NOT EXISTS species_snapshots (
            species_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY, species_id TEXT NOT NULL, status TEXT NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS species_edits (
            species_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            author_id INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, event_date TEXT NOT NULL,
            location TEXT NOT NULL, description TEXT NOT NULL, url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS event_follows (
            user_id INTEGER NOT NULL REFERENCES users(id), event_id INTEGER NOT NULL REFERENCES events(id),
            PRIMARY KEY(user_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS observation_status ON observations(status, observed_on);
        CREATE INDEX IF NOT EXISTS notification_user ON notifications(user_id, is_read);
    """)
    connection.commit()


def tr(text):
    return NE.get(text, text) if session.get("lang") == "ne" else text


def bi(english, nepali):
    return nepali if session.get("lang") == "ne" else english


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def member_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def notify(user_id, key, title, body, link):
    core.get_db().execute(
        "INSERT OR IGNORE INTO notifications (user_id,event_key,title,body,link) VALUES (?,?,?,?,?)",
        (user_id, key, title, body, link),
    )


def apply_edits(rows):
    data = {s["Species_ID"]: dict(s) for s in rows}
    for edit in core.get_db().execute("SELECT species_id,payload FROM species_edits"):
        data[edit["species_id"]] = core._normalise(json.loads(edit["payload"]))
        data[edit["species_id"]]["Data_Origin"] = "Project amendment"
    return list(data.values())


def sync_changes(rows):
    """Compare successful Sheet reads with a persistent baseline; never compare fallback data."""
    db = core.get_db()
    for species in rows:
        sid = species["Species_ID"]
        payload = json.dumps({key: species.get(key, "") for key in FIELDS}, sort_keys=True)
        previous = db.execute("SELECT payload FROM species_snapshots WHERE species_id=?", (sid,)).fetchone()
        if previous and previous["payload"] == payload:
            continue
        old = json.loads(previous["payload"]) if previous else None
        status = species.get("IUCN_Status", "")
        if not old or old.get("IUCN_Status") != status:
            db.execute("INSERT INTO status_history(species_id,status) VALUES (?,?)", (sid, status))
        if old:
            changed = [key.replace("_", " ") for key in FIELDS if old.get(key) != species.get(key, "")]
            revision = db.execute("SELECT COUNT(*) FROM status_history WHERE species_id=?", (sid,)).fetchone()[0]
            digest = hashlib.sha256((previous["payload"] + payload + str(revision)).encode()).hexdigest()
            for user in db.execute("SELECT u.id FROM users u JOIN favorites f ON f.user_id=u.id WHERE f.species_id=? AND u.notify_species=1 AND u.disabled=0", (sid,)).fetchall():
                notify(user["id"], f"species:{sid}:{digest}", f"{species['Common_Name']} updated", "Changed: " + ", ".join(changed) + ". Check the source assessment for details.", f"/species/{sid}")
        db.execute("INSERT INTO species_snapshots(species_id,payload) VALUES (?,?) ON CONFLICT(species_id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP", (sid, payload))
    db.commit()


def featured_species(rows):
    candidates = sorted([s for s in rows if s.get("Featured") == "Yes"] or rows, key=lambda s: s["Species_ID"])
    return candidates[(date.today().toordinal() // 7) % len(candidates)] if candidates else None


def photo_upload(upload):
    if not upload or not upload.filename:
        return None
    raw = upload.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Photo must be 5 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > 20_000_000:
                    raise ValueError("Use a JPEG, PNG, or WebP image under 20 megapixels.")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1600, 1600))
                # A new pixel-only image excludes EXIF, GPS, and other supplied metadata.
                clean = Image.new("RGB", image.size)
                clean.paste(image)
                name = secrets.token_hex(20) + ".jpg"
                os.makedirs(current_app.config["UPLOAD_FOLDER"], exist_ok=True)
                clean.save(os.path.join(current_app.config["UPLOAD_FOLDER"], name), "JPEG", quality=85)
                return name
    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError("That file could not be read as a safe image.")


def public_observation(row, species):
    item = dict(row)
    sensitive = item["sensitive"] or not species or species.get("IUCN_Status") == "Critically Endangered"
    item.update(species=species, sensitive=sensitive)
    if sensitive:
        item.update(location="Sensitive location hidden", latitude=None, longitude=None)
    return item


def progress(user_id):
    db = core.get_db()
    followed = {row[0] for row in db.execute("SELECT species_id FROM favorites WHERE user_id=?", (user_id,))}
    nepal = sum(s["Species_ID"] in followed and s.get("Nepal_Relevance") == "Yes" for s in core.load_species())
    observations = db.execute("SELECT COUNT(*),COALESCE(SUM(status='approved'),0) FROM observations WHERE user_id=?", (user_id,)).fetchone()
    best = db.execute("SELECT COALESCE(MAX(100.0*score/total),0) FROM quiz_attempts WHERE user_id=? AND completed_at IS NOT NULL", (user_id,)).fetchone()[0]
    pledges = db.execute("SELECT COUNT(*) FROM pledges WHERE user_id=?", (user_id,)).fetchone()[0]
    badges = [
        ("First Observation", observations[0], 1, "Save your first field note.", "पहिलो अवलोकन सुरक्षित गर्नुहोस्।"),
        ("Ten Species Followed", len(followed), 10, "Add ten species to your watchlist.", "दस प्रजाति निगरानी सूचीमा थप्नुहोस्।"),
        ("Nepal Wildlife Explorer", nepal, 5, "Follow five species relevant to Nepal.", "नेपालसँग सम्बन्धित पाँच प्रजाति पछ्याउनुहोस्।"),
        ("Conservation Quiz Champion", int(best), 80, "Score at least 80% on a quiz.", "प्रश्नोत्तरीमा कम्तीमा ८० प्रतिशत प्राप्त गर्नुहोस्।"),
        ("Responsible Observer", observations[1], 3, "Have three community observations approved.", "तीन सामुदायिक अवलोकन स्वीकृत गराउनुहोस्।"),
        ("Learning Advocate", pledges, 3, "Choose three actions on Learn & Protect.", "सिकौँ र जोगाऔँमा तीन प्रतिबद्धता छान्नुहोस्।"),
    ]
    return {"badges": badges, "earned": sum(value >= goal for _, value, goal, _, _ in badges), "points": len(followed)*5 + observations[1]*20 + int(best) + pledges*10, "best": round(best)}


def generate_questions(mode):
    if mode == "weekly":
        s = featured_species(core.load_species())
        if not s:
            abort(404)
        return [dict(prompt=f"What is the recorded status of {s['Common_Name']}?", prompt_ne=f"{s['Common_Name']} को अभिलेखित अवस्था के हो?", options=list(core.RISK), options_ne=[NE.get(x,x) for x in core.RISK], answer=list(core.RISK).index(s["IUCN_Status"]) if s["IUCN_Status"] in core.RISK else 0, explanation=f"The current directory records {s['IUCN_Status']}. Verify the linked IUCN assessment.", explanation_ne=f"हालको सूचीमा {tr(s['IUCN_Status'])} अभिलेख छ। मूल आईयूसीएन मूल्याङ्कन जाँच्नुहोस्।")]
    return [dict(prompt=q[0], prompt_ne=q[1], options=q[2], options_ne=q[3], answer=q[4], explanation=q[5], explanation_ne=q[6]) for q in QUIZ]


def fetch_news():
    now = time.time()
    if now - news_cache["time"] < 900:
        return news_cache
    try:
        response = requests.get(NEWS_URL, headers={"User-Agent": "WildTrack/2.0 RSS reader"}, timeout=8)
        response.raise_for_status()
        raw = response.content
        if len(raw) > 2_000_000 or b"<!ENTITY" in raw.upper() or b"<!DOCTYPE" in raw.upper():
            raise ValueError("Unsupported feed")
        root = ElementTree.fromstring(raw)
        items = []
        for item in root.findall("./channel/item")[:60]:
            link, title = item.findtext("link", ""), item.findtext("title", "")
            if urlparse(link).scheme != "https" or not title:
                continue
            tags = [tag.text or "" for tag in item.findall("category")]
            published = item.findtext("pubDate", "")
            try:
                dt = parsedate_to_datetime(published)
                stamp = dt.timestamp()
                display_date = dt.strftime("%d %b %Y")
            except (ValueError, TypeError, OverflowError):
                stamp, display_date = 0, "Date unavailable"
            items.append(dict(title=title, url=link, tags=tags, date=display_date, stamp=stamp, source="Mongabay"))
        if not items:
            raise ValueError("Empty feed")
        news_cache.update(time=now, items=sorted(items, key=lambda x:x["stamp"], reverse=True), error=False)
    except (requests.RequestException, ValueError, ElementTree.ParseError):
        news_cache.update(time=now, error=True)
    return news_cache


@bp.get("/language/<language>")
def language(language):
    if language not in {"en", "ne"}:
        abort(404)
    session["lang"] = language
    return redirect(core.safe_next_url(request.args.get("next")) or url_for("index"))


@bp.get("/species/<species_id>/image")
def species_image(species_id):
    species = core.find_species(species_id)
    if not species:
        abort(404)
    return redirect(core.wikipedia_image(species.get("Scientific_Name", "")))


@bp.get("/learn")
def learn():
    chosen = {r[0] for r in core.get_db().execute("SELECT action FROM pledges WHERE user_id=?", (g.user["id"],))} if g.user else set()
    return render_template("hub/learn.html", lessons=LESSONS, actions=ACTIONS, chosen=chosen, sources=SOURCES)


@bp.post("/learn/pledge")
@member_required
def pledge():
    core.validate_csrf()
    action = request.form.get("action")
    if action not in {a[0] for a in ACTIONS}:
        abort(400)
    db = core.get_db()
    if request.form.get("remove"):
        db.execute("DELETE FROM pledges WHERE user_id=? AND action=?", (g.user["id"], action))
    else:
        db.execute("INSERT OR IGNORE INTO pledges(user_id,action) VALUES (?,?)", (g.user["id"], action))
    db.commit()
    return redirect(url_for("hub.learn", _anchor="actions"))


@bp.get("/compare")
def compare():
    rows = core.load_species()
    ids = list(dict.fromkeys(x for x in request.args.getlist("species") if x))
    error = ""
    if len(ids) > 3:
        error = bi("Choose up to three species.", "बढीमा तीन प्रजाति छान्नुहोस्।")
    selected = [next((s for s in rows if s["Species_ID"] == sid), None) for sid in ids[:3]]
    selected = [s for s in selected if s]
    if ids and len(selected) < 2:
        error = bi("Choose at least two different species.", "कम्तीमा दुई फरक प्रजाति छान्नुहोस्।")
    fields = [("Scientific name","Scientific_Name"),("Status","IUCN_Status"),("Population trend","Population_Trend"),("Habitat","Habitat"),("Region","Native_Region"),("Diet","Diet"),("Threats","Primary_Threats"),("Conservation actions","Conservation_Actions")]
    return render_template("hub/compare.html", species=sorted(rows,key=lambda s:s["Common_Name"]), selected=selected, ids=ids, fields=fields, error=error)


@bp.get("/quiz")
def quiz():
    attempts = core.get_db().execute("SELECT * FROM quiz_attempts WHERE user_id=? AND completed_at IS NOT NULL ORDER BY id DESC LIMIT 10", (g.user["id"],)).fetchall() if g.user else []
    return render_template("hub/quiz.html", attempts=attempts, attempt=None, questions=None)


@bp.post("/quiz/start")
@member_required
def quiz_start():
    core.validate_csrf()
    questions = generate_questions(request.form.get("mode", "full"))
    db = core.get_db()
    cursor = db.execute("INSERT INTO quiz_attempts(user_id,questions,total) VALUES (?,?,?)", (g.user["id"], json.dumps(questions), len(questions)))
    db.commit()
    return redirect(url_for("hub.quiz_attempt", attempt_id=cursor.lastrowid))


@bp.route("/quiz/<int:attempt_id>", methods=["GET","POST"])
@member_required
def quiz_attempt(attempt_id):
    db = core.get_db()
    attempt = db.execute("SELECT * FROM quiz_attempts WHERE id=? AND user_id=?", (attempt_id,g.user["id"])).fetchone()
    if not attempt:
        abort(404)
    questions = json.loads(attempt["questions"])
    if request.method == "POST":
        core.validate_csrf()
        if attempt["completed_at"]:
            return redirect(url_for("hub.quiz_attempt", attempt_id=attempt_id))
        try:
            answers = [int(request.form[f"answer_{i}"]) for i in range(len(questions))]
            if any(a < 0 or a >= len(q["options"]) for a,q in zip(answers,questions)):
                raise ValueError()
        except (KeyError, ValueError):
            flash(bi("Answer every question before submitting.", "बुझाउनुअघि सबै प्रश्नको उत्तर दिनुहोस्।"), "error")
            return redirect(url_for("hub.quiz_attempt", attempt_id=attempt_id))
        score = sum(a == q["answer"] for a,q in zip(answers,questions))
        db.execute("UPDATE quiz_attempts SET answers=?,score=?,completed_at=CURRENT_TIMESTAMP WHERE id=? AND completed_at IS NULL", (json.dumps(answers),score,attempt_id))
        db.commit()
        return redirect(url_for("hub.quiz_attempt", attempt_id=attempt_id))
    return render_template("hub/quiz.html", attempt=attempt, questions=questions, answers=json.loads(attempt["answers"] or "[]"), attempts=[])


@bp.get("/quiz/<int:attempt_id>/certificate")
@member_required
def certificate(attempt_id):
    attempt = core.get_db().execute("SELECT * FROM quiz_attempts WHERE id=? AND user_id=? AND completed_at IS NOT NULL", (attempt_id,g.user["id"])).fetchone()
    if not attempt or attempt["score"] / attempt["total"] < .8:
        abort(404)
    return render_template("hub/certificate.html", attempt=attempt)


@bp.get("/achievements")
@member_required
def achievements():
    return render_template("hub/achievements.html", progress=progress(g.user["id"]))


@bp.get("/news")
def news():
    feed = fetch_news()
    topic = request.args.get("topic", "All")
    filters = {"All": [], "Nepal": ["nepal"], "Asia": ["asia","nepal","india","china","indonesia","borneo","malaysia","sri lanka"], "Climate change": ["climate","warming","carbon"], "Wildlife crime": ["poaching","wildlife trade","trafficking","illegal"], "Habitat restoration": ["restoration","reforest","habitat","forest"]}
    if topic not in filters:
        topic = "All"
    items = [i for i in feed["items"] if not filters[topic] or any(word in (i["title"] + " " + " ".join(i["tags"])).lower() for word in filters[topic])]
    return render_template("hub/news.html", items=items, topics=filters, topic=topic, stale=feed["error"])


@bp.get("/organizations")
def organizations():
    q = request.args.get("q", "").strip().lower()
    organizations = [o for o in ORGANIZATIONS if q in " ".join(str(v) for v in o.values()).lower()]
    return render_template("hub/organizations.html", organizations=organizations)


@bp.get("/community")
def community():
    species = core.load_species()
    lookup = {s["Species_ID"]:s for s in species}
    selected = request.args.get("species", "")
    category = request.args.get("category", "")
    rows = core.get_db().execute("SELECT o.*,u.username FROM observations o JOIN users u ON u.id=o.user_id WHERE o.status='approved' AND u.disabled=0 ORDER BY o.observed_on DESC,o.id DESC LIMIT 200").fetchall()
    observations = [public_observation(r,lookup.get(r["species_id"])) for r in rows if (not selected or r["species_id"]==selected) and (not category or r["category"]==category)]
    pins = [dict(lat=o["latitude"],lng=o["longitude"],name=o["species"]["Common_Name"] if o["species"] else "Species",location=o["location"]) for o in observations if o["latitude"] is not None]
    return render_template("hub/community.html", observations=observations, species=species, categories=CATEGORIES, pins=pins)


@bp.get("/observations/<int:observation_id>/photo")
def observation_photo(observation_id):
    row = core.get_db().execute("SELECT o.*,u.disabled FROM observations o JOIN users u ON u.id=o.user_id WHERE o.id=?", (observation_id,)).fetchone()
    if not row or not row["photo"]:
        abort(404)
    privileged = g.user and (g.user["id"] == row["user_id"] or g.user["is_admin"])
    if not privileged and (row["status"] != "approved" or row["disabled"]):
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(row["photo"]))
    if not os.path.isfile(path):
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/analytics")
def analytics():
    scope = request.args.get("scope", "global")
    species = [s for s in core.load_species() if scope != "nepal" or s.get("Nepal_Relevance") == "Yes"]
    threats = Counter()
    for s in species:
        text = s.get("Primary_Threats", "").lower()
        groups = {"Habitat loss & fragmentation": ["habitat","fragment","forest","land","plantation","road"], "Hunting & wildlife trade": ["poach","hunt","trade","snare","ivory"], "Pollution & poisoning": ["pollution","poison","drug"], "Fishing & bycatch": ["fishing","bycatch","gillnet","nets","entanglement"], "Climate change": ["climate"], "Human–wildlife conflict": ["conflict","retaliat"], "Disease & small populations": ["disease","tiny","small","isolated","breeding"]}
        for group,terms in groups.items():
            if any(term in text for term in terms):
                threats[group] += 1
    series = [
        ("Threats", threats.most_common()),
        ("Region", Counter(s.get("Native_Region","Unknown") for s in species).most_common()),
        ("Population trend", Counter(s.get("Population_Trend","Unknown") for s in species).most_common()),
        ("Class", Counter(s.get("Class","Unknown") for s in species).most_common()),
        ("Habitat", Counter(h.strip() for s in species for h in s.get("Habitat", "").split(";") if h.strip()).most_common(10)),
    ]
    class_risk = {c:Counter(s["IUCN_Status"] for s in species if s["Class"]==c) for c in sorted({s["Class"] for s in species})}
    history = core.get_db().execute("SELECT species_id,status,recorded_at FROM status_history ORDER BY id DESC LIMIT 30").fetchall()
    lookup = {s["Species_ID"]:s for s in species}
    return render_template("hub/analytics.html", series=series, total=len(species), scope=scope, class_risk=class_risk, statuses=list(core.RISK), history=[r for r in history if r["species_id"] in lookup], lookup=lookup)


def event_reminders():
    if not g.user["notify_events"]:
        return
    db = core.get_db()
    today, upcoming = date.today().isoformat(), (date.today()+timedelta(days=7)).isoformat()
    for event in db.execute("SELECT e.* FROM events e JOIN event_follows f ON f.event_id=e.id WHERE f.user_id=? AND e.event_date BETWEEN ? AND ?", (g.user["id"],today,upcoming)).fetchall():
        notify(g.user["id"],f"event:{event['id']}:{event['event_date']}","Upcoming: " + event["title"],f"{event['event_date']} · {event['location']}","/events")
    db.commit()


@bp.route("/alerts", methods=["GET","POST"])
@member_required
def alerts():
    db = core.get_db()
    if request.method == "POST":
        core.validate_csrf()
        if request.form.get("action") == "read":
            db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
        else:
            db.execute("UPDATE users SET notify_species=?,notify_observations=?,notify_events=? WHERE id=?", tuple(int(key in request.form) for key in ("notify_species","notify_observations","notify_events")) + (g.user["id"],))
            flash(bi("Alert preferences saved.", "सूचना प्राथमिकता सुरक्षित भयो।"),"success")
        db.commit()
        return redirect(url_for("hub.alerts"))
    core.load_species()
    event_reminders()
    return render_template("hub/alerts.html", notifications=db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (g.user["id"],)).fetchall())


@bp.route("/events", methods=["GET","POST"])
def events():
    db = core.get_db()
    if request.method == "POST":
        if not g.user:
            return redirect(url_for("login", next="/events"))
        core.validate_csrf()
        event = db.execute("SELECT id FROM events WHERE id=?", (request.form.get("event_id"),)).fetchone()
        if not event:
            abort(404)
        if request.form.get("remove"):
            db.execute("DELETE FROM event_follows WHERE user_id=? AND event_id=?", (g.user["id"],event["id"]))
        else:
            db.execute("INSERT OR IGNORE INTO event_follows(user_id,event_id) VALUES (?,?)", (g.user["id"],event["id"]))
        db.commit()
        return redirect(url_for("hub.events"))
    followed = {r[0] for r in db.execute("SELECT event_id FROM event_follows WHERE user_id=?", (g.user["id"],))} if g.user else set()
    return render_template("hub/events.html", events=db.execute("SELECT * FROM events WHERE event_date>=? ORDER BY event_date", (date.today().isoformat(),)).fetchall(), followed=followed)


@bp.get("/admin")
@admin_required
def admin():
    db = core.get_db()
    species = core.load_species()
    status = request.args.get("status", "pending")
    if status not in {"pending","approved","rejected","hidden"}:
        status = "pending"
    observations = db.execute("SELECT o.*,u.username FROM observations o JOIN users u ON u.id=o.user_id WHERE status=? ORDER BY o.id DESC", (status,)).fetchall()
    return render_template("hub/admin.html", observations=observations, status=status, users=db.execute("SELECT id,username,is_admin,disabled,created_at FROM users ORDER BY id").fetchall(), species=species, edits={r[0] for r in db.execute("SELECT species_id FROM species_edits")}, counts={"users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0], "pending": db.execute("SELECT COUNT(*) FROM observations WHERE status='pending'").fetchone()[0], "observations": db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]}, data_source=core._cache.get("source"), sheet_url=core.SHEET_URL)


@bp.post("/admin/observations/<int:observation_id>")
@admin_required
def moderate(observation_id):
    core.validate_csrf()
    db = core.get_db()
    row = db.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
    if not row or row["status"] == "private":
        abort(404)
    status = request.form.get("status")
    if status not in {"approved","rejected","hidden"}:
        abort(400)
    note = request.form.get("review_note", "").strip()[:500]
    db.execute("UPDATE observations SET status=?,review_note=? WHERE id=?", (status,note,observation_id))
    notify(row["user_id"],f"review:{observation_id}:{status}","Observation review",f"Your observation is now {status}. {note}","/dashboard")
    if status == "approved" and row["status"] != "approved":
        species = core.find_species(row["species_id"])
        for user in db.execute("SELECT u.id FROM users u JOIN favorites f ON f.user_id=u.id WHERE f.species_id=? AND u.notify_observations=1 AND u.disabled=0 AND u.id!=?", (row["species_id"],row["user_id"])).fetchall():
            notify(user["id"],f"observation:{observation_id}","New reviewed observation", f"A community observation was approved for {species['Common_Name'] if species else row['species_id']}.",f"/species/{row['species_id']}")
    db.commit()
    flash("Observation review saved.","success")
    return redirect(url_for("hub.admin"))


@bp.post("/admin/users/<int:user_id>")
@admin_required
def manage_user(user_id):
    core.validate_csrf()
    db = core.get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if user["is_admin"]:
        flash("Administrator accounts cannot be disabled from this screen.","error")
    else:
        db.execute("UPDATE users SET disabled=? WHERE id=?", (not user["disabled"],user_id))
        db.commit()
        flash("Account access updated.","success")
    return redirect(url_for("hub.admin", _anchor="users"))


@bp.route("/admin/species/<species_id>/edit", methods=["GET","POST"])
@admin_required
def edit_species(species_id):
    item = core.find_species(species_id) if species_id != "new" else {}
    if item is None:
        abort(404)
    if request.method == "POST":
        core.validate_csrf()
        sid = request.form.get("Species_ID", "").strip() if species_id == "new" else species_id
        values = {key:request.form.get(key, "").strip() for key in FIELDS}
        error = None
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", sid or ""):
            error = "Use a 2–40 character species ID with letters, numbers, underscores or hyphens."
        elif species_id == "new" and core.find_species(sid):
            error = "This species ID already exists."
        elif any(not values[key] for key in ("Common_Name","Scientific_Name","Class","IUCN_Status")):
            error = "Name, scientific name, class and status are required."
        elif values["IUCN_Status"] not in core.RISK:
            error = "Choose a valid conservation status."
        elif any(len(v)>2000 for v in values.values()):
            error = "Keep each field under 2,000 characters."
        for key,limit in (("Map_Latitude",90),("Map_Longitude",180)):
            try:
                value = float(values[key] or 0)
                if not math.isfinite(value) or abs(value)>limit:
                    raise ValueError()
            except ValueError:
                error = "Enter valid representative map coordinates."
        for key in ("IUCN_Source","Wikipedia_Source"):
            if values[key] and urlparse(values[key]).scheme != "https":
                error = "Source links must use https."
        values.update(Species_ID=sid, Risk_Score=core.RISK.get(values["IUCN_Status"],0), Featured=item.get("Featured","No"))
        if error:
            flash(error,"error")
            item = values
        else:
            db = core.get_db()
            db.execute("INSERT INTO species_edits(species_id,payload,author_id) VALUES (?,?,?) ON CONFLICT(species_id) DO UPDATE SET payload=excluded.payload,author_id=excluded.author_id,updated_at=CURRENT_TIMESTAMP", (sid,json.dumps(values),g.user["id"]))
            db.commit()
            flash("Project amendment saved. The original Google Sheet has not been changed.","success")
            return redirect(url_for("hub.admin", _anchor="species"))
    return render_template("hub/edit_species.html", item=item, fields=FIELDS, statuses=list(core.RISK), new=species_id=="new")


@bp.post("/admin/species/<species_id>/reset")
@admin_required
def reset_species(species_id):
    core.validate_csrf()
    core.get_db().execute("DELETE FROM species_edits WHERE species_id=?", (species_id,))
    core.get_db().commit()
    flash("Project amendment removed; the Google Sheet value is used again where available.","success")
    return redirect(url_for("hub.admin", _anchor="species"))


@bp.post("/admin/events")
@admin_required
def create_event():
    core.validate_csrf()
    data = {key:request.form.get(key, "").strip() for key in ("title","event_date","location","description","url")}
    try:
        valid_date = date.fromisoformat(data["event_date"]) >= date.today()
    except ValueError:
        valid_date = False
    if not valid_date or not data["title"] or not data["location"] or not data["description"] or urlparse(data["url"]).scheme != "https" or any(len(v)>1000 for v in data.values()):
        flash("Complete every event field with a future date and an https source link (maximum 1,000 characters each).","error")
    else:
        core.get_db().execute("INSERT INTO events(title,event_date,location,description,url) VALUES (?,?,?,?,?)", tuple(data.values()))
        core.get_db().commit()
        flash("Event published. Members can follow it for in-app reminders.","success")
    return redirect(url_for("hub.admin", _anchor="events"))


def csv_safe(value):
    text = str(value if value is not None else "")
    return "'"+text if text.lstrip().startswith(("=","+","-","@")) else text


@bp.get("/observations/export")
@member_required
def export_observations():
    admin_export = request.args.get("all") == "1" and g.user["is_admin"]
    query = "SELECT id,species_id,location,observed_on,notes,status,category FROM observations"
    rows = core.get_db().execute(query + (" ORDER BY id" if admin_export else " WHERE user_id=? ORDER BY id"), () if admin_export else (g.user["id"],)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Species ID","General location","Date","Notes","Review status","Category"])
    writer.writerows([[csv_safe(value) for value in row] for row in rows])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv", as_attachment=True, download_name="wildtrack-observations.csv")


def install(app, services):
    global core
    core = services
    app.register_blueprint(bp)

    @app.context_processor
    def extras():
        unread = core.get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)).fetchone()[0] if g.get("user") else 0
        return dict(tr=tr, bi=bi, lang=session.get("lang", "en"), unread_alerts=unread)

    @app.cli.command("promote-admin")
    @click.argument("username")
    def promote_admin(username):
        """Grant administrator access to an existing local account."""
        cursor = core.get_db().execute("UPDATE users SET is_admin=1,disabled=0 WHERE username=? COLLATE NOCASE", (username,))
        core.get_db().commit()
        if not cursor.rowcount:
            raise click.ClickException("Create that account in the app first.")
        click.echo(f"Administrator access enabled for {username}.")

    @app.cli.command("sync-species")
    def sync_species():
        """Refresh the source and generate watched-species alerts."""
        rows = core.load_species(force=True)
        click.echo(f"{core._cache.get('source')}: {len(rows)} species")
