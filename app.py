"""
Pilots and Crew Community - deployable version.

Check-in-only location model:
  Location is ONLY the airport code and city a pilot types when checking in.
  The optional "Find me" button takes one tap-initiated GPS fix to suggest the
  nearest airport; nothing is stored or tracked.

Stack: Flask. SQLite locally, Postgres in production (via DATABASE_URL).
Run:  python app.py   (or: flask --app app run)
Production: gunicorn app:app  (see Dockerfile / render.yaml)
"""
import math
import base64
import concurrent.futures
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from flask import Flask, Response, flash as flask_flash, g, get_flashed_messages, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

# The login background photo ships as base64 (snapshot pushes are text-only);
# decode it to the real JPEG on startup.
try:
    _b64 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "login-bg.jpg.b64")
    _jpg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "login-bg.jpg")
    if os.path.exists(_b64):
        with open(_b64, "rb") as _f:
            _raw = base64.b64decode(_f.read())
        with open(_jpg, "wb") as _f:
            _f.write(_raw)
except Exception:
    pass
# Database: Postgres when DATABASE_URL is set (production, e.g. Render),
# otherwise a local SQLite file (development).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pilots.db"))
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import IntegrityError as _PGIntegrityError
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError, _PGIntegrityError)
else:
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

USER_AGENT = "135-121-pilots-deployable/1.0"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Shared pool for racing Overpass against the Nominatim fallback. Module-level
# so request threads are reused instead of spawned (and joined) per search.
_SEARCH_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="osm")

HOTEL_TAGS = [
    "quiet rooms", "blackout curtains", "crew shuttle", "crew rate",
    "good wifi", "free breakfast", "gym", "close to airport",
]

RESTAURANT_TAGS = [
    "open late", "late kitchen", "crew discount", "walking distance",
    "fast service", "healthy options", "good breakfast", "crew friendly",
]

FBO_TAGS = [
    "crew car", "fuel discount", "gpu available", "hangar space",
    "crew lounge", "catering", "quick turn", "free wifi",
]

def _tags_for_kind(kind):
    if kind == "restaurant":
        return RESTAURANT_TAGS
    if kind == "fbo":
        return FBO_TAGS
    return HOTEL_TAGS

def _after_review_url(kind):
    """Where to land after saving a review: FBO reviews live on Home."""
    if kind == "hotel":
        return url_for("hotels")
    if kind == "restaurant":
        return url_for("restaurants")
    return url_for("index")

def _kind_label(kind):
    return {"fbo": "FBO"}.get(kind, kind)

def _author_name(r):
    return "Anonymous" if r["is_anonymous"] else (r["author"] or "")

# Airport coordinates for the "Find me" button (server-side only; OurAirports,
# public domain). Maps CODE -> [lat, lon, city, name].
AIRPORT_COORDS = {}

# US states for the check-in state dropdown.
US_STATES = [("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("DC", "District of Columbia"), ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"),
    ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming")]
try:
    _coords_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "airports_coords.json")
    with open(_coords_path, encoding="utf-8") as _f:
        AIRPORT_COORDS = json.load(_f)
except (OSError, ValueError):
    pass
# City corrections: the bundled OurAirports data lists some airports under the
# wrong city (e.g. KYIP / Willow Run is in Ypsilanti, not Detroit). Fixes here
# override the dataset at load time, for both the server lookup and the
# client-side autofill file (regenerated below).
try:
    _fixes_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "airport_city_fixes.json")
    with open(_fixes_path, encoding="utf-8") as _f:
        for _code, _city in json.load(_f).items():
            if _code in AIRPORT_COORDS:
                AIRPORT_COORDS[_code][2] = _city
except (OSError, ValueError):
    pass

# ---------------------------------------------------------------- database
SQLITE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, airline TEXT, base TEXT, aircraft TEXT,
        email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'pilot',
        layout TEXT NOT NULL DEFAULT 'tabs',
        profile_photo TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        airport_code TEXT NOT NULL, city TEXT NOT NULL, state TEXT, place_name TEXT, kind TEXT,
        hotel_place TEXT, restaurant_place TEXT,
        start_date TEXT, end_date TEXT, checked_out INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS friends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        friend_id INTEGER NOT NULL REFERENCES users(id),
        UNIQUE(user_id, friend_id)
    );
    CREATE TABLE IF NOT EXISTS friend_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user_id INTEGER NOT NULL REFERENCES users(id),
        to_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(from_user_id, to_user_id)
    );
    CREATE TABLE IF NOT EXISTS search_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL,
        airport_code TEXT NOT NULL, city TEXT NOT NULL,
        radius INTEGER NOT NULL,
        centre TEXT, results TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, kind)
    );
    CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id INTEGER NOT NULL REFERENCES reviews(id),
        data TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL CHECK(kind IN ('hotel','restaurant','fbo')),
        place_name TEXT NOT NULL, airport_code TEXT, city TEXT,
        phone TEXT, address TEXT, lat REAL, lon REAL,
        rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        tags TEXT, notes TEXT, is_anonymous INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id INTEGER NOT NULL REFERENCES reviews(id),
        reporter_id INTEGER NOT NULL REFERENCES users(id),
        reason TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(review_id, reporter_id)
    );
    CREATE TABLE IF NOT EXISTS password_resets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        expires_at INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL REFERENCES users(id),
        recipient_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(sender_id, recipient_id);
    CREATE TABLE IF NOT EXISTS login_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        logged_in_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_login_events_user ON login_events(user_id, logged_in_at);
"""

POSTGRES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL, airline TEXT, base TEXT, aircraft TEXT,
        email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'pilot',
        layout TEXT NOT NULL DEFAULT 'tabs',
        profile_photo TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS checkins (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        airport_code TEXT NOT NULL, city TEXT NOT NULL, state TEXT, place_name TEXT, kind TEXT,
        hotel_place TEXT, restaurant_place TEXT,
        start_date TEXT, end_date TEXT, checked_out INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS friends (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        friend_id INTEGER NOT NULL REFERENCES users(id),
        UNIQUE(user_id, friend_id)
    );
    CREATE TABLE IF NOT EXISTS friend_requests (
        id SERIAL PRIMARY KEY,
        from_user_id INTEGER NOT NULL REFERENCES users(id),
        to_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(from_user_id, to_user_id)
    );
    CREATE TABLE IF NOT EXISTS search_cache (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL,
        airport_code TEXT NOT NULL, city TEXT NOT NULL,
        radius INTEGER NOT NULL,
        centre TEXT, results TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, kind)
    );
    CREATE TABLE IF NOT EXISTS photos (
        id SERIAL PRIMARY KEY,
        review_id INTEGER NOT NULL REFERENCES reviews(id),
        data TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL CHECK(kind IN ('hotel','restaurant','fbo')),
        place_name TEXT NOT NULL, airport_code TEXT, city TEXT,
        phone TEXT, address TEXT, lat REAL, lon REAL,
        rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        tags TEXT, notes TEXT, is_anonymous INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS reports (
        id SERIAL PRIMARY KEY,
        review_id INTEGER NOT NULL REFERENCES reviews(id),
        reporter_id INTEGER NOT NULL REFERENCES users(id),
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(review_id, reporter_id)
    );
    CREATE TABLE IF NOT EXISTS password_resets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        expires_at INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        sender_id INTEGER NOT NULL REFERENCES users(id),
        recipient_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(sender_id, recipient_id);
    CREATE TABLE IF NOT EXISTS login_events (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        logged_in_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_login_events_user ON login_events(user_id, logged_in_at);
"""

def _translate(sql):
    # Every query in this file is written with SQLite "?" placeholders.
    # Postgres wants "%s". (No literal "?" appears inside any query string.)
    return sql.replace("?", "%s") if USE_POSTGRES else sql

class _PGConn:
    """Makes a Postgres connection quack like the sqlite3 connections used below
    (db.execute / db.commit / db.rollback / db.close)."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_translate(sql), params)
        return cur
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()

def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db = _PGConn(psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor))
        else:
            g.db = sqlite3.connect(DATABASE)
            g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            # Gunicorn boots 2 workers, each running init_db() concurrently.
            # Serialize the DDL so two workers can't race creating a new
            # table's SERIAL sequence (UniqueViolation on pg_class).
            # xact lock releases automatically on commit.
            cur.execute("SELECT pg_advisory_xact_lock(987654321)")
            cur.execute(POSTGRES_SCHEMA)
            # Login events no longer store IP addresses.
            cur.execute("ALTER TABLE login_events DROP COLUMN IF EXISTS ip")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS place_name TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS kind TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS hotel_place TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS restaurant_place TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS state TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'pilot'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS layout TEXT NOT NULL DEFAULT 'tabs'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo TEXT")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS phone TEXT")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS address TEXT")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS lat REAL")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS lon REAL")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_anonymous INTEGER NOT NULL DEFAULT 0")
            # Widen the kind CHECK to include 'fbo' (auto-named reviews_kind_check).
            cur.execute("ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_kind_check")
            cur.execute("ALTER TABLE reviews ADD CONSTRAINT reviews_kind_check CHECK (kind IN ('hotel','restaurant','fbo'))")
            # Clear FBO search caches poisoned by failed searches during the OSM outage.
            cur.execute("DELETE FROM search_cache WHERE kind = 'fbo'")
            cur.execute("UPDATE checkins SET hotel_place = place_name WHERE kind='hotel' AND place_name IS NOT NULL AND hotel_place IS NULL")
            cur.execute("UPDATE checkins SET restaurant_place = place_name WHERE kind='restaurant' AND place_name IS NOT NULL AND restaurant_place IS NULL")
            conn.commit()
        finally:
            conn.close()
    else:
        db = sqlite3.connect(DATABASE)
        db.executescript(SQLITE_SCHEMA)
        try:
            db.execute("ALTER TABLE checkins ADD COLUMN place_name TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            db.execute("ALTER TABLE checkins ADD COLUMN kind TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        for _col in ("hotel_place", "restaurant_place", "state"):
            try:
                db.execute(f"ALTER TABLE checkins ADD COLUMN {_col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'pilot'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            db.execute("ALTER TABLE users ADD COLUMN layout TEXT NOT NULL DEFAULT 'tabs'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            db.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        for _col in ("phone", "address", "lat", "lon"):
            try:
                db.execute(f"ALTER TABLE reviews ADD COLUMN {_col} {'REAL' if _col in ('lat','lon') else 'TEXT'}")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            db.execute("ALTER TABLE reviews ADD COLUMN is_anonymous INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        db.execute("UPDATE checkins SET hotel_place = place_name WHERE kind='hotel' AND place_name IS NOT NULL AND hotel_place IS NULL")
        db.execute("UPDATE checkins SET restaurant_place = place_name WHERE kind='restaurant' AND place_name IS NOT NULL AND restaurant_place IS NULL")
        # Login events no longer store IP addresses.
        _le_cols = [r[1] for r in db.execute("PRAGMA table_info(login_events)").fetchall()]
        if "ip" in _le_cols:
            db.execute("ALTER TABLE login_events DROP COLUMN ip")
        db.commit()
        db.close()

# ---------------------------------------------------------------- helpers
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return row

def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped

@app.after_request
def _no_private_page_cache(resp):
    # Never let the browser keep app pages in its back-button cache:
    # after logout, Back re-requests the page and login_required sends
    # the user to /login instead of showing the stale logged-in page.
    # Static assets are excluded (the service worker manages those).
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

def is_current(checkin):
    if not checkin:
        return False
    if checkin["checked_out"]:
        return False
    today = date.today().isoformat()
    start = checkin["start_date"]
    if start and start > today:
        return False
    end = checkin["end_date"]
    if end and end < today:
        return False
    return True

def is_upcoming(checkin):
    """A check-in whose start date is in the future (not checked out)."""
    if not checkin or checkin["checked_out"]:
        return False
    start = checkin["start_date"]
    return bool(start) and start > date.today().isoformat()

def upcoming_checkins(user_id):
    rows = get_db().execute(
        "SELECT * FROM checkins WHERE user_id = ? ORDER BY start_date, id", (user_id,)).fetchall()
    return [r for r in rows if is_upcoming(r)]

def current_checkin(user_id):
    rows = get_db().execute(
        "SELECT * FROM checkins WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)
    ).fetchall()
    for row in rows:
        if is_current(row):
            return row
    return None

def area_matches(checkin, airport_code, city):
    if not checkin:
        return False
    if airport_code and checkin["airport_code"].upper() == airport_code.upper():
        return True
    if city and checkin["city"].strip().lower() == city.strip().lower():
        return True
    return False

def checkin_label(c):
    """Display label for a check-in: hotel/restaurant places first, then airport · city."""
    parts = [p for p in [c["hotel_place"], c["restaurant_place"]] if p]
    if not parts and c["place_name"]:
        parts = [c["place_name"]]
    parts += [c["airport_code"], c["city"]]
    return " · ".join(esc(p) for p in parts if p)

def search_saved_reviews(kind, query, scope, user):
    """Every word in the query must match somewhere in the haystack."""
    rows = get_db().execute(
        """SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id
           WHERE r.kind = ? ORDER BY r.created_at DESC, r.id DESC""", (kind,)
    ).fetchall()
    words = [w.lower() for w in (query or "").split() if w.strip()]
    def matches(row):
        if not words:
            return True
        hay = " ".join([
            row["place_name"] or "", row["airport_code"] or "", row["city"] or "",
            row["phone"] or "", row["address"] or "",
            row["notes"] or "", row["tags"] or "", row["author"] or "",
        ]).lower()
        return all(w in hay for w in words)
    all_results = [r for r in rows if matches(r)]
    all_results = visible_reviews(all_results, user)
    checkin = current_checkin(user["id"]) if user else None
    if scope == "in" and checkin:
        in_results = [r for r in all_results
                      if area_matches(checkin, r["airport_code"], r["city"])]
        return in_results, all_results, checkin
    # No check-in, or scope == "all": in-area view is the full list.
    return all_results, all_results, checkin

def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _state_code(state):
    """Full state name (or ISO code) -> 2-letter code. Returns the input unchanged when unknown."""
    s = (state or "").strip()
    if not s:
        return ""
    if len(s) == 2:
        return s.upper()
    iso = s.upper().replace("US-", "")
    if len(iso) == 2:
        return iso
    return {v.upper(): k for k, v in US_STATES}.get(s.upper(), s)


def _short_address(street, city, state):
    """Street, city, state — only the parts that exist, comma-separated."""
    parts = [p.strip() for p in [street, city, state] if p and p.strip()]
    return ", ".join(parts)

def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        import json
        return json.loads(resp.read().decode("utf-8"))

def geocode_place(query):
    """Resolve a typed place (airport code + city, or city) to lat/lon via OpenStreetMap."""
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
    try:
        results = http_get_json(f"{NOMINATIM_URL}?{params}")
    except Exception:
        return None
    if not results:
        return None
    first = results[0]
    return float(first["lat"]), float(first["lon"]), first.get("display_name", query)

def _overpass_one(base, data):
    import json
    req = urllib.request.Request(base, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("elements", [])

def overpass_query(lat, lon, radius_miles, amenity_filter):
    """Race all mirrors in parallel; first successful response wins."""
    import concurrent.futures
    radius_m = int(radius_miles * 1609.34)
    ql = (
        f'[out:json][timeout:25];'
        f'(node[{amenity_filter}](around:{radius_m},{lat},{lon});'
        f'way[{amenity_filter}](around:{radius_m},{lat},{lon}););'
        f'out center tags;'
    )
    data = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(OVERPASS_URLS)) as pool:
        futures = {pool.submit(_overpass_one, base, data): base for base in OVERPASS_URLS}
        for fut in concurrent.futures.as_completed(futures):
            try:
                return fut.result()
            except Exception:
                continue
    raise RuntimeError("listing service unavailable on all mirrors")

def nominatim_fallback(lat, lon, radius_miles, kind, city):
    """Fallback when Overpass is busy: search by place type near the city, then filter by distance.

    The search is geographically bounded to a box around the coordinates, so an
    ambiguous city name (e.g. Bridgeport WV vs Bridgeport CT) can't pull in
    results from the wrong place.
    """
    import math
    term = {"hotel": "hotels", "restaurant": "restaurants", "fbo": "FBO"}.get(kind, "attractions")
    # Bounding box around the search point (a touch wider than the radius; the
    # haversine filter below does the precise cut).
    lat_d = radius_miles * 1.2 / 69.0
    lon_d = radius_miles * 1.2 / (69.0 * max(0.2, math.cos(math.radians(lat))))
    viewbox = f"{lon - lon_d},{lat + lat_d},{lon + lon_d},{lat - lat_d}"
    params = urllib.parse.urlencode({
        "q": term, "format": "json", "limit": 50, "extratags": 1, "addressdetails": 1,
        "viewbox": viewbox, "bounded": 1,
    })
    try:
        results = http_get_json(f"{NOMINATIM_URL}?{params}")
    except Exception:
        return []
    out = []
    for r in results:
        try:
            rlat, rlon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = haversine_miles(lat, lon, rlat, rlon)
        if dist > radius_miles + 0.05:
            continue
        extra = r.get("extratags") or {}
        addr = r.get("address") or {}
        street = " ".join(x for x in [addr.get("house_number"), addr.get("road")] if x)
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("municipality") or "")
        state = _state_code(addr.get("ISO3166-2-lvl4") or addr.get("state_code") or addr.get("state"))
        address = _short_address(street, city, state)
        out.append({
            "name": r.get("name") or address.split(",")[0], "brand": "",
            "lat": rlat, "lon": rlon,
            "address": address,
            "phone": extra.get("phone") or extra.get("contact:phone") or "",
            "website": extra.get("website") or extra.get("contact:website") or "",
            "cuisine": extra.get("cuisine", ""), "hours": extra.get("opening_hours", ""),
            "atype": r.get("type", ""),
            "distance": round(dist, 1),
        })
    out.sort(key=lambda x: x["distance"])
    return out

def _parse_overpass_elements(elements, clat, clon, radius_miles):
    """Turn raw Overpass elements into the standard result dicts, nearest first."""
    seen, results = set(), []
    for el in elements:
        tags = el.get("tags", {}) or {}
        name = tags.get("name")
        if not name:
            continue
        elat = el.get("lat") or (el.get("center") or {}).get("lat")
        elon = el.get("lon") or (el.get("center") or {}).get("lon")
        if elat is None or elon is None:
            continue
        key = (name.lower(), round(float(elat), 4), round(float(elon), 4))
        if key in seen:
            continue
        seen.add(key)
        dist = haversine_miles(clat, clon, float(elat), float(elon))
        if dist > radius_miles + 0.05:
            continue
        street = " ".join(x for x in [tags.get("addr:housenumber"), tags.get("addr:street")] if x)
        address = _short_address(street, tags.get("addr:city"),
                                 _state_code(tags.get("addr:state")))
        results.append({
            "name": name, "brand": tags.get("brand", ""),
            "lat": float(elat), "lon": float(elon),
            "address": address,
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "website": tags.get("website") or tags.get("contact:website") or "",
            "cuisine": tags.get("cuisine", ""),
            "hours": tags.get("opening_hours", ""),
            "atype": tags.get("tourism", ""),
            "distance": round(dist, 1),
        })
    results.sort(key=lambda r: r["distance"])
    return results


def places_at_coords(lat, lon, radius_miles, kind, display=None, city_hint="", tail_timeout=30):
    """Real OpenStreetMap results around a coordinate pair.

    Overpass and the Nominatim fallback run in parallel on a shared pool;
    Overpass gets a short head start for its richer results, then the first
    usable results from either source win. A slow Overpass no longer blocks
    the fallback, and returning never waits for a straggler thread.
    tail_timeout bounds the wait for a slow source (FBOs use a short one —
    the Nominatim fallback can't find FBOs, so a long wait is pure delay).
    """
    if kind == "hotel":
        filt = 'tourism~"hotel|motel|guest_house|hostel"'
    elif kind == "restaurant":
        filt = 'amenity~"restaurant|cafe|fast_food"'
    elif kind == "fbo":
        filt = ('~"^name$"~"(FBO|Fixed Base|Jet Center|Flight Support|Million Air|'
                'Signature Flight|Atlantic Aviation|Sheltair|TAC Air|Jet Aviation)",i')
    else:
        filt = 'tourism~"attraction|museum|gallery|zoo|aquarium|theme_park|viewpoint|artwork"'
    centre = (lat, lon, display or f"{lat:.4f}, {lon:.4f}")

    def _overpass_results():
        try:
            return _parse_overpass_elements(overpass_query(lat, lon, radius_miles, filt), lat, lon, radius_miles)
        except Exception:
            return []

    over_fut = _SEARCH_POOL.submit(_overpass_results)
    nom_fut = _SEARCH_POOL.submit(nominatim_fallback, lat, lon, radius_miles, kind, city_hint)
    try:
        # First usable results win. Overpass is richer when healthy; the
        # fallback is usually faster — either way the pilot isn't left waiting
        # on a degraded source.
        done, _ = concurrent.futures.wait([over_fut, nom_fut], timeout=6,
                                          return_when=concurrent.futures.FIRST_COMPLETED)
        for fut in done:
            try:
                results = fut.result()
            except Exception:
                results = []
            if results:
                for other in (over_fut, nom_fut):
                    if other is not fut:
                        other.cancel()
                return centre, results, None
        # The first finisher came up empty (or nothing finished in 6s): take
        # the first usable results from whichever finishes next.
        try:
            for fut in concurrent.futures.as_completed([over_fut, nom_fut], timeout=tail_timeout):
                try:
                    results = fut.result()
                except Exception:
                    results = []
                if results:
                    return centre, results, None
        except concurrent.futures.TimeoutError:
            pass  # slow source; fall through to the busy message
    finally:
        for fut in (over_fut, nom_fut):
            fut.cancel()
    return centre, [], "Listing service is busy — try again in a moment."


def places_at_checkin(checkin, radius_miles, kind, tail_timeout=30):
    """Real OpenStreetMap results around the check-in place. No device location."""
    centre = None
    # Fast path: the bundled airport dataset (~53k airports) resolves real
    # codes to coordinates instantly — no Nominatim round-trips.
    code = (checkin["airport_code"] or "").strip().upper()
    if code in AIRPORT_COORDS:
        alat, alon, acity, aname = AIRPORT_COORDS[code]
        centre = (alat, alon, f"{aname}, {acity}")
    else:
        for query in (f"{checkin['airport_code']} airport {checkin['city']}",
                      f"{checkin['airport_code']} {checkin['city']}",
                      checkin["city"]):
            centre = geocode_place(query)
            if centre:
                break
    if not centre:
        return None, None, "Could not resolve the check-in place. Check the airport code and city, then try again."
    lat, lon, display = centre
    return places_at_coords(lat, lon, radius_miles, kind, display=display, city_hint=checkin["city"],
                            tail_timeout=tail_timeout)

# ---------------------------------------------------------------- rendering
CSS = """
html{-webkit-text-size-adjust:100%}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f172a;color:#e2e8f0;overflow-x:hidden;-webkit-tap-highlight-color:transparent}
img{max-width:100%;height:auto}
a{color:#7dd3fc}.wrap{max-width:760px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px}
nav{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
nav a,.btn{background:#1e293b;color:#e2e8f0;text-decoration:none;padding:10px 14px;border-radius:10px;border:1px solid #334155;display:inline-block}
nav a.active{background:#0369a1}
.topbar{display:flex;align-items:center;gap:10px;padding:12px 4px}
.brand{font-weight:800;letter-spacing:.5px;font-size:15px}
.appname{text-align:center;font-size:11px;letter-spacing:2px;color:#7dd3fc;font-weight:700;padding:calc(10px + env(safe-area-inset-top)) 4px 0}
.minimap{height:180px;border-radius:10px;margin:10px 0;position:relative;z-index:0}
.resultsmap{height:260px;border-radius:10px;margin:10px 0;position:relative;z-index:0}
.logout_link{margin-left:auto;color:#94a3b8;text-decoration:none;font-size:13px;border:1px solid #334155;border-radius:999px;padding:8px 14px;min-height:44px;display:inline-flex;align-items:center;box-sizing:border-box}
.has_bnav .wrap{padding-bottom:96px}
.bnav{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;background:#0d1626;border-top:1px solid #334155;padding:8px 4px calc(10px + env(safe-area-inset-bottom))}
.bnav a{flex:1;text-align:center;color:#8fa0b8;text-decoration:none;font-size:10px;min-height:48px;display:flex;flex-direction:column;justify-content:center;align-items:center}
.bnav a .e{display:block;font-size:20px}
.bnav a.on{color:#38bdf8;font-weight:700}
.msgdot{display:inline-block;min-width:16px;height:16px;line-height:16px;border-radius:999px;background:#dc2626;color:#fff;font-size:10px;font-weight:700;padding:0 4px;margin-left:2px;vertical-align:middle}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:16px;margin:12px 0}
input,select,textarea{width:100%;padding:10px;margin:6px 0 12px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;box-sizing:border-box;font-size:16px}
input[type="checkbox"],input[type="radio"]{width:auto;min-height:24px;min-width:24px}
button,.btn-primary{background:#0284c7;color:#fff;border:0;padding:12px 16px;border-radius:10px;font-size:16px;cursor:pointer;width:100%;touch-action:manipulation;min-height:44px}
form[style*="inline"] button{min-height:36px}
label{font-weight:600}.muted{color:#94a3b8}.stars{color:#facc15;letter-spacing:2px}
.tag{display:inline-block;background:#0f172a;border:1px solid #475569;border-radius:999px;padding:2px 10px;margin:2px;font-size:13px}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.thumbs img{width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid #475569}
.flash{background:#365314;border:1px solid #65a30d;padding:10px;border-radius:10px;margin:12px 0}
.error{background:#7f1d1d;border:1px solid #ef4444;padding:10px;border-radius:10px;margin:12px 0}
@media(max-width:600px){h1{font-size:22px}.wrap{padding:12px}.card{padding:14px}.minimap{height:160px}.resultsmap{height:220px}}
"""

# Auto-fills the city when an airport code is typed on the Check-In form.
# Data: static/airports.json (OurAirports, public domain; large + medium airports).
AUTOFILL_JS = """<script>
(function(){
  var codeEl=document.getElementById('airport_code'),cityEl=document.getElementById('city');
  var stateEl=document.getElementById('state');
  if(!codeEl||!cityEl)return;
  var db=null,loading=false;
  function fillState(){
    if(!stateEl)return;
    var code=codeEl.value.trim().toUpperCase();
    if(code.length<3)return;
    fetch('/airport-info?code='+encodeURIComponent(code)).then(function(r){return r.json();}).then(function(d){
      if(d.state){
        stateEl.value=d.state;
        // Load this state's cities into the city type-ahead, keeping the
        // auto-filled city in the field.
        if(window.loadCitiesForState){ window.loadCitiesForState(d.city||null); }
      }
      if(d.city){ cityEl.value=d.city; cityEl.dataset.auto='1'; }
    }).catch(function(){});
  }
  function lookup(){
    var code=codeEl.value.trim().toUpperCase();
    if(code.length<3||!db)return;
    var city=db[code];
    if(city&&(!cityEl.value||cityEl.dataset.auto==='1')){cityEl.value=city;cityEl.dataset.auto='1';}
    fillState();
  }
  function ensure(){
    if(db){lookup();return;}
    if(loading)return;loading=true;
    fetch('/static/airports.json?v=2').then(function(r){return r.json();}).then(function(j){db=j;loading=false;lookup();}).catch(function(){loading=false;});
  }
  codeEl.addEventListener('input',ensure);
  codeEl.addEventListener('change',ensure);
  cityEl.addEventListener('input',function(){cityEl.dataset.auto='';});
})();
</script>"""

# "Find me" button: one tap-initiated GPS fix -> nearest airport fills the form.
# Nothing is stored or tracked; coordinates are only used for the lookup.
LOCMAP_JS = """
<script>
(function(){
  var btn=document.getElementById('locmap_btn'),msg=document.getElementById('locmap_msg'),div=document.getElementById('locmap');
  if(!btn)return;
  var map=null,loaded=false,loading=false;
  function say(t){ if(msg)msg.textContent=t; }
  function loadLeaflet(cb){
    if(loaded)return cb();
    if(loading){ var iv=setInterval(function(){ if(loaded){clearInterval(iv);cb();} },200); return; }
    loading=true;
    var l=document.createElement('link'); l.rel='stylesheet';
    l.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(l);
    var s=document.createElement('script');
    s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    s.onload=function(){ loaded=true; cb(); };
    s.onerror=function(){ loading=false; say('Could not load the map. Check your connection and try again.'); };
    document.head.appendChild(s);
  }
  function locate(){
    if(!navigator.geolocation){ say('Location is not available on this device.'); return; }
    say('Getting your location\u2026');
    navigator.geolocation.getCurrentPosition(function(pos){
      loadLeaflet(function(){
        var lat=pos.coords.latitude,lon=pos.coords.longitude;
        div.style.display='block';
        if(!map){
          map=L.map('locmap').setView([lat,lon],14);
          L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
            maxZoom:19,attribution:'\u00a9 <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          }).addTo(map);
        }else{ map.setView([lat,lon],14); }
        map.eachLayer(function(ly){ if(ly instanceof L.Marker||ly instanceof L.Circle)map.removeLayer(ly); });
        L.marker([lat,lon]).addTo(map).bindPopup('You are here').openPopup();
        if(pos.coords.accuracy)L.circle([lat,lon],{radius:pos.coords.accuracy}).addTo(map);
        setTimeout(function(){ map.invalidateSize(); },100);
        say('');
      });
    },function(err){
      say(err.code===1?'Location permission was denied.':'Could not get your location. Try again.');
    },{enableHighAccuracy:true,timeout:15000,maximumAge:0});
  }
  btn.addEventListener('click',locate);
  locate();
})();
</script>
"""

LOCMAP_HTML = """<div class="card"><h2>My location</h2>
      <p class="muted">Show your phone\u2019s current location on a map. One-time fix \u2014 your coordinates stay on your phone and are never stored.</p>
      <p><button type="button" id="locmap_btn">Show my location on a map</button></p>
      <p class="muted" id="locmap_msg"></p>
      <div id="locmap" style="display:none;height:320px;border-radius:12px;overflow:hidden"></div></div>"""

FINDME_JS = """<script>
(function(){
  var btn=document.getElementById('findme'),msg=document.getElementById('findme_msg');
  var codeEl=document.getElementById('airport_code'),cityEl=document.getElementById('city');
  if(!btn||!codeEl||!cityEl)return;
  function say(t){ if(msg)msg.textContent=t; }
  // Container for the airport list (created once).
  var list=document.createElement('div'); list.id='nearby_airports';
  msg.parentNode.insertBefore(list, msg.nextSibling);
  btn.addEventListener('click',function(){
    if(!navigator.geolocation){ say('Location is not supported on this device. Type the airport code instead.'); return; }
    btn.disabled=true; var orig=btn.textContent; btn.textContent='Locating…';
    list.innerHTML='';
    function done(){ btn.disabled=false; btn.textContent=orig; }
    navigator.geolocation.getCurrentPosition(function(pos){
      fetch('/nearby-airports?lat='+pos.coords.latitude+'&lon='+pos.coords.longitude+'&radius=20')
        .then(function(r){ return r.json(); })
        .then(function(d){
          var aps=d.airports||[];
          if(!aps.length){ say('No airports within 50 miles. Type the airport code instead.'); done(); return; }
          say('Airports near you (tap one):');
          var html='<div style="margin-top:8px">';
          aps.forEach(function(a){
            html+='<button type="button" class="ap-pick" data-code="'+a.code+'" data-city="'+a.city.replace(/"/g,'&quot;')+'" style="display:block;width:100%;text-align:left;margin:4px 0;padding:10px 12px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;color:#0f172a;font-size:15px"><strong>'+a.code+'</strong> &middot; '+a.name+' &middot; '+a.distance_mi+' mi</button>';
          });
          html+='</div>';
          list.innerHTML=html;
          list.querySelectorAll('.ap-pick').forEach(function(b){
            b.addEventListener('click',function(){
              codeEl.value=b.dataset.code; cityEl.value=b.dataset.city; cityEl.dataset.auto='1';
              say('Selected '+b.dataset.code+'.');
              list.innerHTML='';
            });
          });
          done();
        })
        .catch(function(){ say('Lookup failed. Type the airport code instead.'); done(); });
    },function(err){
      var why=err.code===1?'permission denied':err.code===2?'unavailable':err.code===3?'timed out':'unavailable';
      say('Could not get your location ('+why+'). Type the airport code instead.');
      done();
    },{enableHighAccuracy:true,timeout:12000,maximumAge:60000});
  });
})();
</script>"""

# City dropdown: picking a state loads that state's cities into the city <select>.
CITY_DROPDOWN_JS = """<script>
(function(){
  var stateEl=document.getElementById('state'),cityEl=document.getElementById('city');
  var suggEl=document.getElementById('city_suggest');
  if(!stateEl||!cityEl)return;
  var allCities=[];
  function showSuggest(){
    if(!suggEl)return;
    var q=cityEl.value.trim().toLowerCase();
    if(!q||!allCities.length){ suggEl.innerHTML=''; return; }
    var matches=[];
    for(var i=0;i<allCities.length&&matches.length<8;i++){
      if(allCities[i].toLowerCase().indexOf(q)===0)matches.push(allCities[i]);
    }
    if(!matches.length){ suggEl.innerHTML=''; return; }
    var html='';
    matches.forEach(function(c){
      html+='<button type="button" class="city-opt" data-city="'+c.replace(/"/g,'&quot;')+'" style="display:block;width:100%;text-align:left;padding:10px 12px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;color:#0f172a;font-size:15px;margin:4px 0">'+c+'</button>';
    });
    suggEl.innerHTML=html;
    suggEl.querySelectorAll('.city-opt').forEach(function(b){
      b.addEventListener('click',function(){
        cityEl.value=b.dataset.city;
        suggEl.innerHTML='';
        cityEl.dispatchEvent(new Event('change',{bubbles:true}));
      });
    });
  }
  function loadCities(keep){
    var state=stateEl.value.trim();
    if(!state){ allCities=[]; if(suggEl)suggEl.innerHTML=''; return; }
    fetch('/cities-in-state?state='+encodeURIComponent(state))
      .then(function(r){ return r.json(); })
      .then(function(d){
        allCities=d.cities||[];
        if(keep){ cityEl.value=keep; }
      })
      .catch(function(){});
  }
  stateEl.addEventListener('change',function(){ cityEl.value=''; allCities=[]; loadCities(null); });
  cityEl.addEventListener('input',showSuggest);
  cityEl.addEventListener('focus',showSuggest);
  document.addEventListener('click',function(e){
    if(suggEl&&!suggEl.contains(e.target)&&e.target!==cityEl)suggEl.innerHTML='';
  });
  // If the state is already set (edit form), load its cities now.
  if(stateEl.value.trim())loadCities(null);
  // Exposed for the airport-code autofill, which sets state + city together.
  window.loadCitiesForState=loadCities;
})();
</script>"""

CITY_AIRPORTS_JS = """<script>
(function(){
  var cityEl=document.getElementById('city'),codeEl=document.getElementById('airport_code');
  var stateEl=document.getElementById('state'),box=document.getElementById('city_airports');
  if(!cityEl||!codeEl||!box)return;
  var timer=null;
  function lookup(){
    var city=cityEl.value.trim(),state=stateEl?stateEl.value.trim():'';
    if(city.length<3||!state){ box.innerHTML=''; return; }
    fetch('/airports-near-city?city='+encodeURIComponent(city)+'&state='+encodeURIComponent(state))
      .then(function(r){ return r.json(); })
      .then(function(d){
        var aps=d.airports||[];
        if(!aps.length){
          if(state && !d.verified){ box.innerHTML='<p class="muted" style="margin:8px 0">Could not find '+city+' in that state — check the spelling.</p>'; }
          else { box.innerHTML=''; }
          return;
        }
        var html='<p class="muted" style="margin:8px 0 4px">Airports near '+city+(state?', '+state:'')+' (tap one):</p>';
        aps.forEach(function(a){
          html+='<button type="button" class="city-ap" data-code="'+a.code+'" data-city="'+a.city.replace(/"/g,'&quot;')+'" style="display:block;width:100%;text-align:left;margin:4px 0;padding:10px 12px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;color:#0f172a;font-size:15px"><strong>'+a.code+'</strong> &middot; '+a.name+' &middot; '+a.distance_mi+' mi</button>';
        });
        box.innerHTML=html;
        box.querySelectorAll('.city-ap').forEach(function(b){
          b.addEventListener('click',function(){
            codeEl.value=b.dataset.code; cityEl.value=b.dataset.city;
            box.innerHTML='';
          });
        });
      })
      .catch(function(){ box.innerHTML=''; });
  }
  cityEl.addEventListener('input',function(){ clearTimeout(timer); timer=setTimeout(lookup,500); });
  cityEl.addEventListener('change',lookup);
  if(stateEl)stateEl.addEventListener('change',lookup);
})();
</script>"""

def page(title, user, body, flash=None, error=None):
    msg = f'<div class="flash">{flash}</div>' if flash else ""
    err = f'<div class="error">{error}</div>' if error else ""
    chrome, after, bcls = "", "", ""
    if user:
        import json as _json
        bcls = "has_bnav"
        first = esc(user["name"].split()[0]) if user["name"] else ""
        first_js = _json.dumps(first)
        logout = f'<a href="{url_for("logout")}" class="logout_link">Log out</a>' if title == "Home" else ""
        chrome = (f'<div class="appname">Pilot &amp; Crew Community</div>'
                  f'<div class="topbar"><span class="brand" id="greet">Hello, {first}</span>{logout}</div>'
                  f'<script>(function(){{var h=new Date().getHours();'
                  f'var g=h<12?"Good morning":h<18?"Good afternoon":"Good evening";'
                  f'document.getElementById("greet").textContent=g+", "+{first_js};}})();</script>')
        items = [("Home", url_for("index"), "🏠"), ("Hotels", url_for("hotels"), "🛏"),
                 ("Restaurants", url_for("restaurants"), "🍽"), ("Attractions", url_for("attractions"), "🎡"),
                 ("Friends", url_for("friends"), "👥"), ("Messages", url_for("messages"), "💬"),
                 ("Profile", url_for("profile"), "👤")]
        try:
            _unread = get_db().execute(
                "SELECT COUNT(*) AS n FROM messages WHERE recipient_id = ? AND is_read = 0",
                (user["id"],)).fetchone()["n"]
        except Exception:
            _unread = 0
        def _label(t, e):
            badge = f' <span class="msgdot">{_unread}</span>' if t == "Messages" and _unread else ""
            return f'<span class="e">{e}</span>{t}{badge}'
        bnav = "".join(
            f'<a href="{u}" class="{"on" if (t == title or (t == "Profile" and title == "My Profile")) else ""}">'
            f'{_label(t, e)}</a>' for t, u, e in items)
        after = (f'<nav class="bnav">{bnav}</nav>'
                 f'{MINIMAP_JS}'
                 '<script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/sw.js");}</script>')
    pwa_head = ('<link rel="manifest" href="/manifest.json">'
                '<meta name="theme-color" content="#0f172a">'
                '<meta name="mobile-web-app-capable" content="yes">'
                '<meta name="apple-mobile-web-app-capable" content="yes">'
                '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">')
    return render_template_string(f"""<!doctype html><html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      {pwa_head}
      <title>{{{{ title }}}} - Pilots and Crew Community</title><style>{CSS}</style></head>
      <body class="{bcls}"><div class="wrap">{chrome}{msg}{err}{body}{after}</div></body></html>""",
      title=title)

def esc(s):
    import html
    return html.escape(str(s or ""))


def state_dropdown(selected="", field_id="state", field_name="state"):
    """State <select> for the check-in form. selected is a 2-letter code."""
    sel = (selected or "").strip().upper()
    opts = ['<option value="">Select state</option>']
    for code, name in US_STATES:
        s = ' selected' if code == sel else ''
        opts.append(f'<option value="{code}"{s}>{name}</option>')
    return (f'<select name="{field_name}" id="{field_id}" style="display:block;width:100%">'
            + "".join(opts) + '</select>')

def stars(n):
    return "★" * int(n) + "☆" * (5 - int(n))

def _save_review_photos(db, review_id, files):
    """Store up to 5 uploaded images for a review, resized to JPEG. Returns count saved."""
    if not _PIL_OK:
        return 0
    saved = 0
    for f in files or []:
        if saved >= 5 or not f or not f.filename:
            continue
        try:
            img = Image.open(f.stream)
            img.verify()
            f.stream.seek(0)
            img = Image.open(f.stream).convert("RGB")
            img.thumbnail((1200, 1200))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72)
            db.execute("INSERT INTO photos (review_id, data) VALUES (?,?)",
                       (review_id, base64.b64encode(buf.getvalue()).decode()))
            saved += 1
        except Exception:
            continue
    return saved

def _resize_photo_b64(f, max_px):
    """Resize one uploaded image to a base64 JPEG, or None if unusable."""
    if not _PIL_OK or not f or not f.filename:
        return None
    try:
        img = Image.open(f.stream)
        img.verify()
        f.stream.seek(0)
        img = Image.open(f.stream).convert("RGB")
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None

def _avatar_html(user_id, size=36):
    """Round profile photo, or a circle with the user's first initial."""
    row = get_db().execute("SELECT name, profile_photo FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return ""
    initial = esc((row["name"] or "?").strip()[:1].upper() or "?")
    st = (f"width:{size}px;height:{size}px;border-radius:50%;object-fit:cover;"
          f"vertical-align:middle;margin-right:6px")
    if row["profile_photo"]:
        return f'<img src="{url_for("profile_photo", user_id=user_id)}" alt="" style="{st}">'
    return (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'{st}background:#334155;color:#e2e8f0;font-weight:700">{initial}</span>')

def review_photos_html(review_id):
    rows = get_db().execute("SELECT id FROM photos WHERE review_id=? ORDER BY id", (review_id,)).fetchall()
    if not rows:
        return ""
    thumbs = "".join(
        f'<a href="{url_for("photo", photo_id=r["id"])}" target="_blank" rel="noopener">'
        f'<img src="{url_for("photo", photo_id=r["id"])}" alt="Review photo" loading="lazy"></a>'
        for r in rows)
    return f'<div class="thumbs">{thumbs}</div>'

MINIMAP_JS = """
<script>
(function(){
  var singles=document.querySelectorAll('.minimap');
  var multis=document.querySelectorAll('.resultsmap');
  if(!singles.length&&!multis.length)return;
  function tiles(m){
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(m);
  }
  function escHtml(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
  function init(){
    singles.forEach(function(d){
      var lat=parseFloat(d.getAttribute('data-lat')),lon=parseFloat(d.getAttribute('data-lon'));
      if(!isFinite(lat)||!isFinite(lon))return;
      var m=L.map(d,{zoomControl:false}).setView([lat,lon],15);
      tiles(m);
      L.marker([lat,lon]).addTo(m);
      m.scrollWheelZoom.disable();
      setTimeout(function(){m.invalidateSize();},150);
    });
    multis.forEach(function(d){
      var markers;
      try{markers=JSON.parse(d.getAttribute('data-markers'));}catch(e){return;}
      if(!markers||!markers.length)return;
      var m=L.map(d,{zoomControl:false}).setView([markers[0].lat,markers[0].lon],12);
      tiles(m);
      var bounds=[];
      markers.forEach(function(p){
        if(!isFinite(p.lat)||!isFinite(p.lon))return;
        L.marker([p.lat,p.lon]).addTo(m).bindPopup(escHtml(p.name));
        bounds.push([p.lat,p.lon]);
      });
      if(bounds.length>1)m.fitBounds(bounds,{padding:[24,24]});
      m.scrollWheelZoom.disable();
      setTimeout(function(){m.invalidateSize();},150);
    });
  }
  if(window.L&&window.L.map){init();return;}
  var l=document.createElement('link');l.rel='stylesheet';l.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.appendChild(l);
  var s=document.createElement('script');s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';s.onload=init;document.head.appendChild(s);
})();
</script>
"""

def hidden_review_ids():
    """Review ids flagged by 3+ pilots -> report count. Cached per request."""
    if not hasattr(g, "_hidden"):
        rows = get_db().execute(
            "SELECT review_id, COUNT(*) AS c FROM reports GROUP BY review_id HAVING COUNT(*) >= 3").fetchall()
        g._hidden = {r["review_id"]: r["c"] for r in rows}
    return g._hidden

def visible_reviews(rows, user):
    hidden = hidden_review_ids()
    uid = user["id"] if user else None
    return [r for r in rows if r["id"] not in hidden or r["user_id"] == uid]

def review_card(r, user):
    tags = "".join(f'<span class="tag">{esc(t.strip())}</span>' for t in (r["tags"] or "").split(",") if t.strip())
    own = ""
    if user and r["user_id"] == user["id"]:
        own = f"""<p style="margin:10px 0 0"><a href="{url_for('edit_review', review_id=r['id'])}">Edit my review</a></p>
          <form method="post" action="{url_for('delete_review', review_id=r['id'])}" style="margin-top:6px">
          <button type="submit" style="background:#7f1d1d;width:auto;padding:8px 12px">Delete my review</button></form>"""
    loc_query = ", ".join(p for p in [r["place_name"] or "", r["city"] or ""] if p)
    loc_url = "https://www.openstreetmap.org/search?" + urllib.parse.urlencode({"query": loc_query})
    photos = review_photos_html(r["id"])
    hidden = hidden_review_ids()
    report = ""
    if user and r["user_id"] != user["id"]:
        report = (f'<p style="margin:8px 0 0"><a class="muted" href="{url_for("report_review", review_id=r["id"])}">'
                  f'Report review</a></p>')
    elif user and r["id"] in hidden:
        report = (f'<p class="muted">Hidden from other pilots — flagged by {hidden[r["id"]]} pilots.</p>')
    contact = ""
    if r["address"]:
        contact += f'<div>{esc(r["address"])}</div>'
    if r["phone"]:
        contact += f'<div><a href="tel:{esc(r["phone"])}">{esc(r["phone"])}</a></div>'
    minimap = ""
    if r["lat"] is not None and r["lon"] is not None:
        try:
            minimap = f'<div class="minimap" data-lat="{float(r["lat"])}" data-lon="{float(r["lon"])}"></div>'
        except (TypeError, ValueError):
            pass
    code = (r["airport_code"] or "").strip()
    code_html = (f'<a href="{url_for("airport_hub", code=code.upper())}">{esc(code)}</a>' if code
                 else '<span class="muted">—</span>')
    byline_avatar = "" if r["is_anonymous"] else _avatar_html(r["user_id"], 28)
    return f"""<div class="card"><a href="{loc_url}" target="_blank" rel="noopener"><strong>{esc(r['place_name'])}</strong></a> <span class="stars">{stars(r['rating'])}</span>
      <div class="muted">{code_html} · {esc(r['city'])} · {byline_avatar}by {esc(_author_name(r))}</div>
      {contact}{minimap}<div>{tags}</div><p>{esc(r['notes'])}</p>{photos}{own}{report}</div>"""

def place_aggregates(rows):
    """Aggregate reviews by place: avg rating + count, best first."""
    agg = {}
    for r in rows:
        key = ((r["place_name"] or "").strip().lower(), (r["airport_code"] or "").strip().upper(),
               (r["city"] or "").strip().lower())
        a = agg.setdefault(key, {"name": (r["place_name"] or "").strip(), "code": r["airport_code"],
                                 "city": r["city"], "total": 0, "n": 0, "lat": None, "lon": None})
        try:
            a["total"] += int(r["rating"] or 0)
        except (TypeError, ValueError):
            pass
        a["n"] += 1
        if a["lat"] is None and r["lat"] is not None:
            a["lat"], a["lon"] = r["lat"], r["lon"]
    out = [{"name": a["name"], "code": a["code"], "city": a["city"], "avg": a["total"] / a["n"],
            "n": a["n"], "lat": a["lat"], "lon": a["lon"]}
           for a in agg.values() if a["n"]]
    out.sort(key=lambda x: (-x["avg"], -x["n"], x["name"].lower()))
    return out

def saved_reviews_section(kind, user, q, scope, sort="new", tag=""):
    in_results, all_results, checkin = search_saved_reviews(kind, q, scope, user)
    label = "hotel" if kind == "hotel" else "restaurant"
    if not checkin:
        return (f'<div class="card"><h2>Saved pilot {label} reviews</h2>'
                f'<p class="muted">Check in on the <a href="{url_for("board")}">Check-In</a> '
                f'page to see saved reviews.</p></div>')
    if sort not in ("new", "top"):
        sort = "new"
    tag = (tag or "").strip()
    if tag:
        tl = tag.lower()
        in_results = [r for r in in_results
                      if any(x.strip().lower() == tl for x in (r["tags"] or "").split(","))]
        all_results = [r for r in all_results
                       if any(x.strip().lower() == tl for x in (r["tags"] or "").split(","))]
    if sort == "top":
        in_results = sorted(in_results, key=lambda r: (-(r["rating"] or 0), -(r["id"] or 0)))
    def _qs(**kw):
        base = {"q": q or "", "scope": scope, "sort": sort, "tag": tag}
        base.update(kw)
        return "?" + urllib.parse.urlencode({k: v for k, v in base.items() if v})
    other_count = 0
    hint = ""
    if scope == "in" and checkin and not in_results and all_results:
        other_count = len(all_results)
        hint = (f'<div class="card">Found in another area — see all areas. '
                f'<a href="{_qs(scope="all")}">See all areas ({other_count})</a></div>')
    scope_links = (f'<p>Scope: <a href="{_qs(scope="in")}">In this area</a> | '
                   f'<a href="{_qs(scope="all")}">All areas</a> '
                   f'(current check-in: {esc(checkin["airport_code"])} · {esc(checkin["city"])})</p>')
    sort_links = (f'<p>Sort: {"<strong>Newest</strong>" if sort == "new" else f"<a href=\"{_qs(sort="new")}\">Newest</a>"} | '
                  f'{"<strong>Top rated</strong>" if sort == "top" else f"<a href=\"{_qs(sort="top")}\">Top rated</a>"}</p>')
    tag_list = RESTAURANT_TAGS if kind == "restaurant" else HOTEL_TAGS
    tag_chips = []
    for t in tag_list:
        if t.lower() == tag.lower() and tag:
            tag_chips.append(f'<a class="tag" href="{_qs(tag="")}" style="background:#0369a1">{esc(t)} ✕</a>')
        else:
            tag_chips.append(f'<a class="tag" href="{_qs(tag=t)}">{esc(t)}</a>')
    tag_html = f'<p style="line-height:2.2">{" ".join(tag_chips)}</p>' if tag_chips else ""
    base_rows = in_results if scope == "in" else all_results
    aggs = place_aggregates(base_rows)[:6]
    top_html = ""
    if aggs:
        chips = "".join(
            f'<a class="tag" href="{_qs(q=a["name"], tag="")}">★{a["avg"]:.1f} ({a["n"]}) · {esc(a["name"])}</a> '
            for a in aggs)
        top_html = f'<div class="card"><h2>Top rated {label}s</h2><p style="line-height:2.2">{chips}</p></div>'
    cards = "".join(review_card(r, user) for r in in_results) or '<p class="muted">No saved reviews match yet.</p>'
    return f"""
      <div class="card"><h2>Search saved pilot {label} reviews</h2>
      <form method="get"><label>Search saved pilot {label} reviews</label>
      <input type="text" name="q" value="{esc(q)}" placeholder="Place, airport code, city, notes, or tag">
      <input type="hidden" name="scope" value="{esc(scope)}">
      <input type="hidden" name="sort" value="{esc(sort)}">
      <input type="hidden" name="tag" value="{esc(tag)}">
      <button type="submit">Search</button></form>
      <p class="muted">Searches pilot reviews saved in this app — place name, airport code, city, phone, address, notes, and tags. Every word must match somewhere.</p>
      {scope_links}{sort_links}{tag_html}</div>{top_html}{hint}{cards}"""

# ---------------------------------------------------------------- auth
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or not password:
            return page("Register", None, register_form(), error="Name, email, and password are required.")
        db = get_db()
        role = request.form.get("role", "pilot")
        if role not in ("pilot", "crew"):
            role = "pilot"
        params = (name, request.form.get("airline", "").strip(), request.form.get("base", "").strip(),
                  request.form.get("aircraft", "").strip(), email, generate_password_hash(password), role)
        try:
            if USE_POSTGRES:
                cur = db.execute(
                    "INSERT INTO users (name, airline, base, aircraft, email, password_hash, role) "
                    "VALUES (?,?,?,?,?,?,?) RETURNING id", params)
                new_id = cur.fetchone()["id"]
            else:
                cur = db.execute(
                    "INSERT INTO users (name, airline, base, aircraft, email, password_hash, role) VALUES (?,?,?,?,?,?,?)",
                    params)
                new_id = cur.lastrowid
            db.commit()
        except _INTEGRITY_ERRORS:
            db.rollback()
            return page("Register", None, register_form(), error="That email is already registered. Try logging in.")
        session["user_id"] = new_id
        return redirect(url_for("index"))
    return page("Register", None, register_form())

def register_form():
    return """<div class="card"><h2>Create an account</h2><p class="muted">Open to all pilots and crew.</p>
    <form method="post"><label>Name</label><input name="name" required>
    <label>Role</label><select name="role"><option value="pilot">Pilot</option><option value="crew">Crew</option></select>
    <label>Airline</label><input name="airline"><label>Base</label><input name="base">
    <label>Aircraft</label><input name="aircraft"><label>Email</label><input type="email" name="email" required>
    <label>Password</label><input type="password" name="password" required>
    <button type="submit">Register</button></form>
    <p>Already have an account? <a href="/login">Log in</a></p></div>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            import time
            db = get_db()
            # One tap can submit the form twice (double-tap before the redirect).
            # Record the login only if this user has no event in the last minute.
            now = int(time.time())
            last = db.execute("SELECT MAX(logged_in_at) AS t FROM login_events WHERE user_id = ?",
                              (row["id"],)).fetchone()["t"]
            if not last or now - last >= 60:
                db.execute("INSERT INTO login_events (user_id, logged_in_at) VALUES (?,?)",
                           (row["id"], now))
                db.commit()
            session["user_id"] = row["id"]
            return redirect(url_for("index"))
        return login_page(error="Email or password did not match.")
    return login_page()

def _fmt_login_ts(ts):
    """Epoch seconds -> 'YYYY-MM-DD HH:MM UTC'; None -> 'never'."""
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _hash_reset_token(token):
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Email a one-hour, single-use password reset link."""
    if request.method == "POST":
        import secrets, time
        email = request.form.get("email", "").strip().lower()
        db = get_db()
        db.execute("DELETE FROM password_resets WHERE expires_at <= ?", (int(time.time()),))
        db.commit()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            db.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?,?,?)",
                       (user["id"], _hash_reset_token(token), int(time.time()) + 3600))
            db.commit()
            link = request.host_url.rstrip("/") + url_for("reset_password", token=token)
            sent = send_email(email, "Reset your Pilots and Crew Community password",
                              f"Hi {user['name']},\n\nSomeone asked to reset the password for this account.\n\n"
                              f"Reset it here (valid 1 hour, one-time use):\n{link}\n\n"
                              "If that wasn't you, just ignore this email.")
            if not sent:
                return page("Forgot password", None,
                            '<div class="card"><h2>Forgot password</h2>'
                            '<p class="error">Could not send the reset email right now. Try again later.</p>'
                            f'<p><a href="{url_for("login")}">Back to log in</a></p></div>')
        # Same message whether or not the email is registered.
        return page("Forgot password", None,
                    '<div class="card"><h2>Check your email</h2>'
                    '<p>If that email is registered, a reset link is on its way (valid 1 hour).</p>'
                    f'<p><a href="{url_for("login")}">Back to log in</a></p></div>')
    return page("Forgot password", None,
                f"""<div class="card"><h2>Forgot password</h2>
                <p class="muted">Enter your account email and we'll send you a reset link.</p>
                <form method="post"><label>Email</label>
                <input type="email" name="email" required autocomplete="email">
                <button type="submit">Send reset link</button></form>
                <p><a href="{url_for("login")}">Back to log in</a></p></div>""")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Consume a reset token and set a new password."""
    import time
    db = get_db()
    db.execute("DELETE FROM password_resets WHERE expires_at <= ?", (int(time.time()),))
    db.commit()
    row = db.execute("SELECT * FROM password_resets WHERE token_hash = ?",
                     (_hash_reset_token(token),)).fetchone()
    if not row:
        return page("Reset password", None,
                    '<div class="card"><h2>Link expired</h2>'
                    '<p class="muted">That reset link is invalid or expired. Request a new one.</p>'
                    f'<p><a href="{url_for("forgot_password")}">Forgot password</a></p></div>')
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < 8:
            return page("Reset password", None,
                        '<div class="card"><h2>Choose a new password</h2>'
                        '<p class="error">Password must be at least 8 characters.</p>'
                        '<form method="post"><label>New password</label>'
                        '<input type="password" name="new_password" required autocomplete="new-password">'
                        '<button type="submit">Set new password</button></form></div>')
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new), row["user_id"]))
        db.execute("DELETE FROM password_resets WHERE id=?", (row["id"],))
        db.commit()
        return page("Reset password", None,
                    '<div class="card"><h2>Password updated</h2>'
                    f'<p><a href="{url_for("login")}">Log in with your new password</a></p></div>')
    return page("Reset password", None,
                """<div class="card"><h2>Choose a new password</h2>
                <form method="post"><label>New password (at least 8 characters)</label>
                <input type="password" name="new_password" required autocomplete="new-password">
                <button type="submit">Set new password</button></form></div>""")

ATC_AUDIO_JS = """
(function(){
  var KEY='atc_audio';
  var enabled=false;
  try{ enabled=(localStorage.getItem(KEY)||'on')==='on'; }catch(_){ enabled=true; }
  var HAS_TTS=('speechSynthesis' in window);
  var btn=document.getElementById('atc_toggle');
  var hint=document.getElementById('atc_hint');
  function paint(){ if(btn) btn.textContent=enabled?'\\uD83D\\uDCFB ATC on':'\\uD83D\\uDCFB ATC off'; }
  function showHint(s){ if(hint) hint.style.display=s?'block':'none'; }
  if(btn) btn.addEventListener('click',function(e){
    e.stopPropagation();
    enabled=!enabled;
    try{ localStorage.setItem(KEY,enabled?'on':'off'); }catch(_){}
    paint();
    if(enabled){ boot(); } else { stop(); showHint(false); }
  });
  paint();

  var PHRASES=[
    "American twelve thirty four, D F W tower, runway one seven center, cleared for takeoff.",
    "Cleared for takeoff, runway one seven center, American twelve thirty four.",
    "Delta four fifty six, contact departure, one two four point three.",
    "One two four point three, Delta four fifty six.",
    "Southwest seven eighty nine, taxi to runway two six via alpha, hold short of bravo.",
    "Taxi to two six via alpha, hold short of bravo, Southwest seven eighty nine.",
    "United two twenty, D F W approach, descend and maintain three thousand.",
    "Down to three thousand, United two twenty.",
    "FedEx nine oh four heavy, turn left heading two seven zero, cleared I L S runway one eight left.",
    "Left two seven zero, cleared I L S one eight left, FedEx nine oh four heavy.",
    "American sixty seven, wind two four zero at one two, runway one seven right, cleared to land.",
    "Cleared to land, one seven right, American sixty seven.",
    "JetBlue eleven twelve, traffic, Boeing seven thirty seven, two o'clock, five miles, same altitude.",
    "Looking for traffic, JetBlue eleven twelve.",
    "Alaska three forty one, climb and maintain flight level two three zero.",
    "Up to two three zero, Alaska three forty one.",
    "Spirit nine fifteen, D F W ground, runway one seven left at kilo, line up and wait.",
    "Line up and wait, one seven left at kilo, Spirit nine fifteen.",
    "Endeavor fifty five twenty three, contact tower one two six point five five.",
    "One two six point five five, Endeavor fifty five twenty three.",
    "Delta one ninety eight, reduce speed to one eight zero knots.",
    "One eight zero knots, Delta one ninety eight.",
    "American fourteen oh five, taxi to the gate via mike, monitor ground point niner.",
    "To the gate via mike, American fourteen oh five.",
    "Southwest thirty two, wind calm, runway two six, cleared for takeoff.",
    "Cleared for takeoff, two six, Southwest thirty two."
  ];

  var AC=null, started=false, timer=null, speaking=false, voice=null;

  function pickVoice(){
    try{
      var vs=speechSynthesis.getVoices().filter(function(v){return v.lang&&v.lang.toLowerCase().indexOf('en')===0;});
      if(vs.length) voice=vs[Math.floor(Math.random()*vs.length)];
    }catch(_){}
  }
  if(HAS_TTS){ pickVoice(); speechSynthesis.onvoiceschanged=pickVoice; }

  function noiseBuffer(dur){
    var b=AC.createBuffer(1,Math.max(1,Math.floor(AC.sampleRate*dur)),AC.sampleRate);
    var d=b.getChannelData(0);
    for(var i=0;i<d.length;i++) d[i]=Math.random()*2-1;
    return b;
  }
  function squelch(){
    if(!AC) return;
    try{
      var src=AC.createBufferSource(); src.buffer=noiseBuffer(0.12);
      var f=AC.createBiquadFilter(); f.type='bandpass'; f.frequency.value=2400; f.Q.value=1.2;
      var g=AC.createGain(); g.gain.value=0.22;
      src.connect(f); f.connect(g); g.connect(AC.destination); src.start();
    }catch(_){}
  }
  function staticBed(dur){
    if(!AC) return;
    try{
      var src=AC.createBufferSource(); src.buffer=noiseBuffer(dur); src.loop=true;
      var f=AC.createBiquadFilter(); f.type='bandpass'; f.frequency.value=1800; f.Q.value=0.6;
      var g=AC.createGain(); g.gain.value=0.014;
      src.connect(f); f.connect(g); g.connect(AC.destination);
      src.start(); src.stop(AC.currentTime+dur);
    }catch(_){}
  }

  function say(text){
    return new Promise(function(res){
      var done=false;
      function fin(){ if(!done){ done=true; res(); } }
      if(!HAS_TTS){ fin(); return; }
      try{
        speechSynthesis.cancel();
        var u=new SpeechSynthesisUtterance(text);
        if(voice) u.voice=voice;
        u.rate=1.02+Math.random()*0.12;
        u.pitch=0.72+Math.random()*0.28;
        u.volume=0.9;
        u.onend=fin; u.onerror=fin;
        setTimeout(fin,15000);
        speaking=true;
        squelch();
        setTimeout(function(){ staticBed(Math.min(20,2+text.length*0.09)); speechSynthesis.speak(u); },140);
      }catch(_){ fin(); }
    });
  }

  function loop(){
    timer=null;
    if(!enabled||!started||document.hidden){ return; }
    var text=PHRASES[Math.floor(Math.random()*PHRASES.length)];
    say(text).then(function(){
      speaking=false;
      if(!enabled||!started) return;
      timer=setTimeout(loop,2000);
    });
  }

  function boot(){
    if(!HAS_TTS||!enabled){ showHint(false); return; }
    if(!AC){
      try{ AC=new (window.AudioContext||window.webkitAudioContext)(); }catch(_){ AC=null; }
    }
    if(AC&&AC.state==='suspended'){ AC.resume().catch(function(){}); }
    showHint(false);
    if(!started){ started=true; }
    if(!speaking&&!timer){ loop(); }
  }
  function stop(){
    if(timer){ clearTimeout(timer); timer=null; }
    try{ if(HAS_TTS) speechSynthesis.cancel(); }catch(_){}
    speaking=false;
  }
  document.addEventListener('visibilitychange',function(){
    if(document.hidden){ stop(); }
    else if(enabled&&started&&!speaking&&!timer){ loop(); }
  });
  function arm(e){
    if(e&&e.target&&e.target.closest&&e.target.closest('#atc_toggle')) return;
    document.removeEventListener('pointerdown',arm);
    document.removeEventListener('keydown',arm);
    if(enabled) boot(); else showHint(false);
  }
  document.addEventListener('pointerdown',arm);
  document.addEventListener('keydown',arm);
  if(enabled&&HAS_TTS){ showHint(true); } else { showHint(false); }
})();

"""

ATC_AUDIO_HTML = """<button type="button" id="atc_toggle" class="atc_toggle">📻 ATC on</button><div id="atc_hint" class="atc_hint">Tap anywhere for ATC radio audio</div>"""

def login_page(error=None):
    """Full-bleed photo login page. Standalone document, not wrapped in page()."""
    err = f'<div class="error">{error}</div>' if error else ""
    return render_template_string(f"""<!doctype html><html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Log in - Pilots and Crew Community</title><style>
      body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#e2e8f0;
        min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}}
      .bg{{position:fixed;inset:0;background:url('{url_for("static", filename="login-bg.jpg")}') center/cover no-repeat}}
      .scrim{{position:fixed;inset:0;background:linear-gradient(180deg,rgba(5,9,22,.45),rgba(5,9,22,.72))}}
      .card{{position:relative;width:100%;max-width:380px;background:rgba(13,20,38,.84);
        border:1px solid rgba(148,163,184,.28);border-radius:18px;padding:30px 26px;box-sizing:border-box;
        box-shadow:0 18px 50px rgba(0,0,0,.45)}}
      h1{{margin:0 0 4px;font-size:28px;letter-spacing:.5px}}
      .tag{{color:#bae6fd;font-size:14px;margin:0 0 22px}}
      label{{font-weight:600;font-size:14px}}
      input{{width:100%;padding:11px;margin:6px 0 14px;border-radius:10px;border:1px solid #475569;
        background:#0b1322;color:#e2e8f0;box-sizing:border-box;font-size:16px}}
      button{{width:100%;background:linear-gradient(180deg,#38bdf8,#0284c7);color:#fff;border:0;padding:13px;
        border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}}
      .reg{{text-align:center;margin:14px 0 0;font-size:14px;color:#cbd5e1}}
      .reg a{{color:#7dd3fc}}
      .error{{background:#7f1d1d;border:1px solid #ef4444;padding:10px;border-radius:10px;margin:0 0 14px}}
      .atc_toggle{{position:fixed;top:12px;right:12px;z-index:5;width:auto;background:rgba(13,20,38,.7);
        border:1px solid rgba(148,163,184,.35);color:#bae6fd;border-radius:999px;padding:8px 14px;
        font-size:13px;font-weight:600;cursor:pointer}}
      .atc_hint{{position:fixed;bottom:14px;left:0;right:0;z-index:5;text-align:center;color:#cbd5e1;
        font-size:13px;opacity:.85;pointer-events:none;text-shadow:0 1px 6px #000;display:none}}
      </style></head><body><div class="bg"></div><div class="scrim"></div>
      <div class="card"><h1>Pilots and Crew Community</h1>
      <p class="tag">The Pilots and Crew Community App is where pilots share real layover intel. Check in at your overnight, leave your honest opinion of the hotels and restaurants nearby, and read what other pilots thought before you book, eat, or sleep.</p>
      {err}<form method="post" id="login_form">
      <label>Email</label><input type="email" name="email" required autocomplete="email">
      <label>Password</label><input type="password" name="password" required autocomplete="current-password">
      <button type="button" id="login_btn">Log in</button></form>
      <p class="reg"><a href="/forgot-password">Forgot password?</a></p>
      <p class="reg">No account yet? <a href="/register">Register</a></p></div>
      {ATC_AUDIO_HTML}<script>{ATC_AUDIO_JS}</script>
      <script>var _lf=document.getElementById('login_form');var _lb=document.getElementById('login_btn');_lf.addEventListener('keydown',function(e){{if(e.key==='Enter'){{e.preventDefault();}}}});_lb.addEventListener('click',function(){{if(_lf.checkValidity()){{_lb.disabled=true;_lf.submit();}}else{{_lf.reportValidity();}}}});</script>
      </body></html>""")

@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        # Wipe the user's cached hotel/restaurant/attraction search results.
        # Reviews, check-ins, and friends are their data and stay.
        db = get_db()
        db.execute("DELETE FROM search_cache WHERE user_id = ?", (uid,))
        db.commit()
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    db = get_db()
    if request.method == "POST" and request.form.get("form") == "password":
        new = request.form.get("new_password", "")
        row = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not check_password_hash(row["password_hash"], request.form.get("current_password", "")):
            return page("My Profile", user, profile_form(user), error="Current password did not match.")
        if len(new) < 8:
            return page("My Profile", user, profile_form(user), error="New password must be at least 8 characters.")
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new), user["id"]))
        db.commit()
        return page("My Profile", user, profile_form(user), flash="Password changed.")
    if request.method == "POST" and request.form.get("form") == "photo":
        if request.form.get("remove") == "1":
            db.execute("UPDATE users SET profile_photo=NULL WHERE id=?", (user["id"],))
            db.commit()
            return page("My Profile", user, profile_form(current_user()), flash="Profile photo removed.")
        data = _resize_photo_b64(request.files.get("photo"), 512)
        if not data:
            return page("My Profile", user, profile_form(user),
                        error="That photo could not be used — try a JPG or PNG.")
        db.execute("UPDATE users SET profile_photo=? WHERE id=?", (data, user["id"]))
        db.commit()
        return page("My Profile", user, profile_form(current_user()), flash="Profile photo updated.")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        if not name or not email:
            return page("My Profile", user, profile_form(user), error="Name and email are required.")
        clash = db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email, user["id"])).fetchone()
        if clash:
            return page("My Profile", user, profile_form(user), error="That email is already used by another account.")
        role = request.form.get("role", "pilot")
        if role not in ("pilot", "crew"):
            role = "pilot"
        db.execute("UPDATE users SET name=?, airline=?, base=?, aircraft=?, email=?, role=? WHERE id=?",
                   (name, request.form.get("airline", "").strip(), request.form.get("base", "").strip(),
                    request.form.get("aircraft", "").strip(), email, role, user["id"]))
        db.commit()
        user = current_user()
        return page("My Profile", user, profile_form(user), flash="Profile updated.")
    admin_card = (f'<div class="card"><h2>Admin</h2><p><a href="{url_for("admin")}">View app stats</a> — '
                  'registered users, check-ins, reviews.</p></div>') if _is_admin(user) else ""
    return page("My Profile", user, profile_form(user) + admin_card)

def _is_admin(user):
    """The app owner, set via the ADMIN_EMAIL env var (kept out of the repo)."""
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    return bool(admin_email) and (user["email"] or "").strip().lower() == admin_email

@app.route("/admin")
@login_required
def admin():
    """Owner-only stats: registered users, check-ins, reviews, logins."""
    user = current_user()
    if not _is_admin(user):
        return page("Admin", user, '<div class="card"><p class="muted">Not available.</p></div>')
    db = get_db()
    users = db.execute(
        "SELECT id, name, email, role, airline, base, aircraft, created_at, "
        "profile_photo IS NOT NULL AS has_photo FROM users ORDER BY created_at").fetchall()
    n_checkins = db.execute("SELECT count(*) AS n FROM checkins").fetchone()["n"]
    n_reviews = db.execute("SELECT count(*) AS n FROM reviews").fetchone()["n"]
    n_logins = db.execute("SELECT count(*) AS n FROM login_events").fetchone()["n"]
    # Current check-in per user (newest current row wins).
    current_by_user = {}
    for c in db.execute("SELECT * FROM checkins ORDER BY created_at DESC, id DESC").fetchall():
        if c["user_id"] not in current_by_user and is_current(c):
            current_by_user[c["user_id"]] = c
    def _checkin_text(uid):
        c = current_by_user.get(uid)
        if not c:
            return "no current check-in"
        label = f"{c['airport_code']} · {c['city']}"
        if c["state"]:
            label += f", {c['state']}"
        return f"checked in at {label}"
    def _remove_photo_form(uid):
        return (f' <form method="post" action="{url_for("admin_remove_photo", user_id=uid)}" '
                f'style="display:inline;margin-left:8px">'
                f'<button type="submit" style="background:#7f1d1d;width:auto;padding:4px 10px">'
                f'Remove photo</button></form>')
    rows = "".join(
        f"<li>{_avatar_html(u['id'], 28)}{esc(u['name'])} — {esc(u['email'])} "
        f"({esc(u['role'] or 'pilot').title()} · {esc(u['airline'])} · {esc(u['base'])} · {esc(u['aircraft'])}) "
        f"<span class='muted'>joined {esc(str(u['created_at'] or ''))}</span>"
        f"{_remove_photo_form(u['id']) if u['has_photo'] else ''}</li>"
        for u in users)
    # One row per user: their latest login only.
    recent = db.execute(
        "SELECT u.id AS uid, u.name, u.email, MAX(le.logged_in_at) AS last_login "
        "FROM login_events le JOIN users u ON u.id = le.user_id "
        "GROUP BY u.id ORDER BY last_login DESC LIMIT 100").fetchall()
    login_rows = "".join(
        f"<li>{_fmt_login_ts(r['last_login'])} — {esc(r['name'])} ({esc(r['email'])}) · "
        f"{esc(_checkin_text(r['uid']))}</li>"
        for r in recent)
    body = (f'<div class="card"><h2>App stats</h2>'
            f'<p><strong>{len(users)}</strong> registered users · '
            f'<strong>{n_checkins}</strong> check-ins · <strong>{n_reviews}</strong> reviews · '
            f'<strong>{n_logins}</strong> logins recorded</p>'
            f'<ul>{rows}</ul></div>'
            f'<div class="card"><h2>Recent logins</h2>'
            f'<p class="muted">One row per user — their latest login. Logins are recorded from the Nov 8 update onward; logins before it were not tracked.</p>'
            f'<ul>{login_rows or "<li class=\"muted\">No logins recorded yet.</li>"}</ul></div>')
    return page("Admin", user, body)

@app.route("/admin/remove-photo/<int:user_id>", methods=["POST"])
@login_required
def admin_remove_photo(user_id):
    """Owner removes a pilot's profile photo."""
    user = current_user()
    if not _is_admin(user):
        return page("Admin", user, '<div class="card"><p class="muted">Not available.</p></div>')
    db = get_db()
    db.execute("UPDATE users SET profile_photo=NULL WHERE id=?", (user_id,))
    db.commit()
    return redirect(url_for("admin"))

def _role_options(selected):
    return "".join(
        f'<option value="{v}"{" selected" if v == selected else ""}>{v.title()}</option>'
        for v in ("pilot", "crew"))

def profile_form(user):
    role = user["role"] if user["role"] in ("pilot", "crew") else "pilot"
    try:
        _has_photo = bool(user["profile_photo"])
    except (KeyError, IndexError):
        _has_photo = False
    remove_form = ('<form method="post" style="margin-top:8px"><input type="hidden" name="form" value="photo">'
                   '<input type="hidden" name="remove" value="1">'
                   '<button type="submit" style="background:#7f1d1d;width:auto;padding:8px 12px">Remove photo</button></form>') if _has_photo else ""
    photo_card = (f'<div class="card"><h2>Profile photo</h2>'
                  f'<p>{_avatar_html(user["id"], 72)}</p>'
                  f'<form method="post" enctype="multipart/form-data"><input type="hidden" name="form" value="photo">'
                  f'<label>Photo</label><input type="file" name="photo" accept="image/*">'
                  f'<button type="submit">Upload photo</button></form>{remove_form}</div>')
    return photo_card + f"""<div class="card"><h2>My Profile</h2>
    <form method="post"><label>Name</label><input name="name" value="{esc(user['name'])}" required>
    <label>Role</label><select name="role">{_role_options(role)}</select>
    <label>Airline</label><input name="airline" value="{esc(user['airline'])}">
    <label>Base</label><input name="base" value="{esc(user['base'])}">
    <label>Aircraft</label><input name="aircraft" value="{esc(user['aircraft'])}">
    <label>Email</label><input type="email" name="email" value="{esc(user['email'])}" required>
    <button type="submit">Save</button></form></div>
    <div class="card"><h2>Change password</h2>
    <form method="post"><input type="hidden" name="form" value="password">
    <label>Current password</label><input type="password" name="current_password" required autocomplete="current-password">
    <label>New password (at least 8 characters)</label><input type="password" name="new_password" required autocomplete="new-password">
    <button type="submit">Change password</button></form></div>"""

@app.route("/")
@login_required
def index():
    user = current_user()
    db = get_db()
    checkin = current_checkin(user["id"])
    notes = get_flashed_messages()
    body = (current_checkin_html(checkin)
            + upcoming_checkins_html(user)
            + LOCMAP_HTML + LOCMAP_JS
            + friends_nearby_html(db, user, checkin)
            + fbo_area_html(db, user, checkin)
            + area_reviews_html(db, user, checkin))
    return page("Home", user, body, flash=" ".join(notes) if notes else None)

# ---------------------------------------------------------------- board
def airports_within(lat, lon, radius_miles=20):
    """Airports within radius_miles, sorted by distance. Each is (code, city, name, distance_mi).

    Dedupes by coordinates — the dataset lists the same airport under both its
    ICAO (KCKB) and IATA (CKB) codes. Prefers the ICAO (K-prefixed) code.
    """
    if not AIRPORT_COORDS:
        return []
    by_coord = {}
    for code, (alat, alon, city, name) in AIRPORT_COORDS.items():
        p1, p2 = math.radians(lat), math.radians(alat)
        dphi = math.radians(alat - lat)
        dlmb = math.radians(alon - lon)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        d = 2 * 3958.8 * math.asin(math.sqrt(a))
        if d <= radius_miles:
            key = (round(alat, 4), round(alon, 4))
            existing = by_coord.get(key)
            # Prefer ICAO (K-prefixed, 4-letter) over IATA (3-letter).
            if existing is None or (len(code) == 4 and code.startswith("K") and len(existing[0]) != 4):
                by_coord[key] = (code, city, name, round(d, 1))
    out = list(by_coord.values())
    out.sort(key=lambda x: x[3])
    return out


def airports_near_city(city, state=None, radius_miles=20):
    """Airports within radius_miles of a city (and state, when given).

    Verifies the city + state against the bundled US cities dataset (local,
    no network — Nominatim rate-limits the server's shared IP): the city must
    appear in that state's list. The city's coordinates from the same dataset
    are the search centre.

    Returns ([(code, city, name, dist)], matched_city, verified_bool).
    verified_bool is True only when the city was confirmed in the state.
    """
    city = (city or "").strip()
    state = (state or "").strip().upper()
    if not city or not state or not AIRPORT_COORDS:
        return [], None, False
    cities = load_us_cities().get(state, {})
    # Case-insensitive city lookup.
    match = None
    for name, coords in cities.items():
        if name.lower() == city.lower():
            match = (name, coords)
            break
    if match is None:
        return [], None, False
    matched, (lat, lon) = match
    return airports_within(lat, lon, radius_miles), matched, True


def nearest_airport_for(lat, lon):
    """(code, city, name, distance_mi) of the nearest bundled airport, or Nones."""
    if not AIRPORT_COORDS:
        return None, None, None, None
    best, best_d = None, None
    for code, (alat, alon, city, name) in AIRPORT_COORDS.items():
        # Haversine distance in miles.
        p1, p2 = math.radians(lat), math.radians(alat)
        dphi = math.radians(alat - lat)
        dlmb = math.radians(alon - lon)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        d = 2 * 3958.8 * math.asin(math.sqrt(a))
        if best_d is None or d < best_d:
            best, best_d = (code, city, name), d
    if best is None:
        return None, None, None, None
    code, city, name = best
    return code, city, name, round(best_d, 1)


@app.route("/nearby-airports")
@login_required
def nearby_airports():
    """Airports within 20 miles of a tap-initiated GPS fix. Nothing is stored."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400
    radius = float(request.args.get("radius", "20"))
    found = airports_within(lat, lon, radius)
    return jsonify({"airports": [{"code": c, "city": ci, "name": n, "distance_mi": d}
                                 for c, ci, n, d in found]})


# In-memory cache: airport code -> state code (from Nominatim reverse geocode).
_AIRPORT_STATE_CACHE = {}


def airport_state(code):
    """State code for an airport code (reverse geocode, cached). Returns "" when unknown."""
    code = (code or "").strip().upper()
    if not code or code not in AIRPORT_COORDS:
        return ""
    state = _AIRPORT_STATE_CACHE.get(code)
    if state is None:
        lat, lon, _city, _name = AIRPORT_COORDS[code]
        params = urllib.parse.urlencode({
            "lat": lat, "lon": lon, "format": "json", "addressdetails": 1})
        try:
            r = http_get_json(f"https://nominatim.openstreetmap.org/reverse?{params}")
            addr = (r or {}).get("address", {})
            state = (addr.get("ISO3166-2-lvl4") or "").replace("US-", "").upper()
            if not state:
                state = (addr.get("state_code") or addr.get("state") or "").strip().upper()
                if len(state) > 2:
                    state = {v.upper(): k for k, v in US_STATES}.get(state, "")
        except Exception:
            state = ""
        _AIRPORT_STATE_CACHE[code] = state or ""
    return state


def verify_checkin(code, city, state):
    """Verify an airport code, city, and state agree. Returns (ok, error).

    - Unknown code -> not ok.
    - City must match the dataset city for that code.
    - State, when given, must match the airport's state (reverse geocode).
      If the state lookup fails (network), the state check is skipped.
    """
    code = (code or "").strip().upper()
    city = (city or "").strip()
    state = (state or "").strip().upper()
    if not code or code not in AIRPORT_COORDS:
        return False, f"Unknown airport code {code}."
    _lat, _lon, db_city, _name = AIRPORT_COORDS[code]
    if city.lower() != db_city.strip().lower():
        return False, f"{code} is in {db_city}, not {city}."
    if state:
        real_state = airport_state(code)
        if real_state and state != real_state:
            return False, f"{code} is in {real_state}, not {state}."
    return True, ""


@app.route("/airport-info")
@login_required
def airport_info():
    """City + state for an airport code. State comes from a reverse geocode
    of the airport's coordinates (cached in-memory after the first lookup)."""
    code = request.args.get("code", "").strip().upper()
    if not code or code not in AIRPORT_COORDS:
        return jsonify({"error": "unknown code"}), 404
    _lat, _lon, city, name = AIRPORT_COORDS[code]
    return jsonify({"code": code, "city": city, "state": airport_state(code), "name": name})


# US cities by state (bundled dataset, public domain — kelvins/US-Cities-Database).
# Format: {STATE: {CITY: [lat, lon]}}.
_US_CITIES = None


def load_us_cities():
    """Load the bundled cities dataset once (cached in memory)."""
    global _US_CITIES
    if _US_CITIES is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "static", "us_cities.json"), encoding="utf-8") as f:
                _US_CITIES = json.load(f)
        except Exception:
            _US_CITIES = {}
    return _US_CITIES


@app.route("/cities-in-state")
@login_required
def cities_in_state():
    """All cities in a state, sorted. Used to populate the city dropdown."""
    state = request.args.get("state", "").strip().upper()
    if not state:
        return jsonify({"cities": []})
    cities = load_us_cities().get(state, {})
    return jsonify({"cities": sorted(cities.keys())})


@app.route("/airports-near-city")
@login_required
def airports_near_city_route():
    """Airports within 20 miles of a typed city + state.

    Both city and state are required before the search begins. The city and
    state are verified against Nominatim first — if the city isn't in that
    state, no airports are returned (verified: false).
    """
    city = request.args.get("city", "").strip()
    state = request.args.get("state", "").strip()
    if not city or not state:
        return jsonify({"airports": [], "matched_city": None, "verified": False})
    found, matched, verified = airports_near_city(city, state)
    return jsonify({"airports": [{"code": c, "city": ci, "name": n, "distance_mi": d}
                                 for c, ci, n, d in found],
                    "matched_city": matched, "verified": verified})


@app.route("/nearest_airport")
@login_required
def nearest_airport():
    """Nearest airport to a tap-initiated GPS fix. Nothing is stored."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400
    code, city, name, dist = nearest_airport_for(lat, lon)
    if code is None:
        return jsonify({"error": "airport data unavailable"}), 503
    return jsonify({"code": code, "city": city, "name": name, "distance_mi": dist})

def _slot_checkout_form(slot, label):
    """Small form that checks the user out of one place (hotel or restaurant)."""
    return (f'<form method="post" action="{url_for("checkout_place")}" style="display:block;margin:2px 0 6px" '
            f'onsubmit="return confirm(\'Check out of this {label}?\')">'
            f'<input type="hidden" name="slot" value="{slot}">'
            f'<button type="submit" style="width:auto;padding:4px 10px">Checked out</button></form>')


def upcoming_checkins_html(user):
    """Card listing the user's future-dated check-ins, with edit/cancel."""
    rows = upcoming_checkins(user["id"])
    if not rows:
        return ""
    items = "".join(
        f"<li>{checkin_label(r)} <span class='muted'>— {esc(r['start_date'])} to {esc(r['end_date'] or 'open')}</span> "
        f"<a href=\"{url_for('edit_checkin', id=r['id'])}\">Edit</a> "
        f"<form method=\"post\" action=\"{url_for('cancel_checkin')}\" style=\"display:inline\" "
        f"onsubmit=\"return confirm('Cancel this upcoming check-in?')\">"
        f"<input type=\"hidden\" name=\"id\" value=\"{r['id']}\">"
        f"<button type=\"submit\" style=\"width:auto;padding:4px 10px;background:#7f1d1d\">Cancel</button></form></li>"
        for r in rows)
    return f'<div class="card"><h2>Upcoming check-ins</h2><ul>{items}</ul></div>'


def current_checkin_html(checkin):
    """Card showing the user's active check-in (hotel/restaurant places link to the review form)."""
    checkin_btn = (f'<a href="{url_for("board")}" class="btn" style="width:auto;white-space:nowrap">'
                   f'Locate</a>')
    head = (f'<div style="display:flex;justify-content:space-between;align-items:center;gap:12px">'
            f'<h2 style="margin:0">Current Location</h2>{checkin_btn}</div>')
    if not checkin:
        return f'<div class="card">{head}<p class="muted">No active check-in.</p></div>'
    def _place_link(place, kind):
        url = (url_for("new_review") + "?" + urllib.parse.urlencode(
            {"kind": kind, "place": place,
             "airport": checkin["airport_code"], "city": checkin["city"]}))
        return f'<a href="{url}">{esc(place)}</a>'
    _REVIEW_HINT = ' <span class="muted" style="font-weight:400">(Hit to add review)</span>'
    status_lines = []
    if checkin["hotel_place"]:
        status_lines.append(f"Hotel: {_place_link(checkin['hotel_place'], 'hotel')}{_REVIEW_HINT}")
        status_lines.append(_slot_checkout_form('hotel', 'hotel'))
    if checkin["restaurant_place"]:
        status_lines.append(f"Restaurant: {_place_link(checkin['restaurant_place'], 'restaurant')}{_REVIEW_HINT}")
        status_lines.append(_slot_checkout_form('restaurant', 'restaurant'))
    if not status_lines and checkin["place_name"]:
        # Legacy rows (pre hotel/restaurant split).
        kind = checkin["kind"] or "hotel"
        status_lines.append(f"{_place_link(checkin['place_name'], kind)}{_REVIEW_HINT}")
        status_lines.append(_slot_checkout_form(kind, kind))
    loc = f'{esc(checkin["airport_code"])} · {esc(checkin["city"])}'
    try:
        _st = checkin["state"]
    except (KeyError, IndexError):
        _st = None
    if _st:
        loc += f', {esc(_st)}'
    status_lines.append(loc)
    status_html = "<br>".join(status_lines)
    return f"""<div class="card">{head}
      <p><strong>{status_html}</strong></p>
      <p class="muted">{esc(checkin['start_date'])} to {esc(checkin['end_date'] or 'open')}</p>
      <p><a href="{url_for('edit_checkin')}">Edit check-in</a></p>
      <form method="post" action="{url_for('board')}" onsubmit="return confirm('Check out of this location?')"><input type="hidden" name="action" value="checkout">
      <button type="submit">Leaving the area</button></form></div>"""


def friends_nearby_html(db, user, checkin):
    """Friends whose current check-in matches the user's check-in area."""
    friend_rows = db.execute(
        "SELECT u.* FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = ?", (user["id"],)
    ).fetchall()
    nearby = []
    for fr in friend_rows:
        fc = current_checkin(fr["id"])
        if checkin and fc and (fc["airport_code"].upper() == checkin["airport_code"].upper()
                               or fc["city"].strip().lower() == checkin["city"].strip().lower()):
            nearby.append(f"{_avatar_html(fr['id'], 28)}{esc(fr['name'])} — {checkin_label(fc)}")
    nearby_html = "".join(f"<li>{n}</li>" for n in nearby) or "<li class='muted'>No friends checked in to the same area.</li>"
    return (f'<div class="card"><h2>Friends in the same area</h2><ul>{nearby_html}</ul>'
            f'<p><a href="{url_for("friends")}">Manage friends</a></p></div>')


def area_reviews_html(db, user, checkin):
    """Newest pilot reviews in the user's check-in area."""
    if not checkin:
        return ""
    rev_rows = db.execute(
        """SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id
           WHERE upper(r.airport_code) = ? OR lower(r.city) = ?
           ORDER BY r.created_at DESC, r.id DESC LIMIT 20""",
        ((checkin["airport_code"] or "").upper(), (checkin["city"] or "").strip().lower())).fetchall()
    rev_rows = visible_reviews(rev_rows, user)
    html = '<div class="card"><h2>Pilot reviews in this area</h2>'
    if rev_rows:
        html += (f'<p class="muted">{len(rev_rows)} reviews at '
                 f'{esc(checkin["airport_code"])} · {esc(checkin["city"])}</p></div>')
        html += "".join(review_card(r, user) for r in rev_rows)
    else:
        html += '<p class="muted">No pilot reviews here yet — be the first.</p></div>'
    return html


def fbo_area_html(db, user, checkin):
    """FBOs in the area, server-rendered from the check-in airport code.

    Uses AirNav (fast, ~2s) with per-check-in caching. No JavaScript —
    the list renders with the page so there's nothing to get stuck.
    """
    if not checkin:
        return ""
    radius = 3
    manual = (url_for("new_review") + "?" + urllib.parse.urlencode(
        {"kind": "fbo", "airport": checkin["airport_code"] or "", "city": checkin["city"] or ""}))
    # Cached?
    cached = load_search_cache(user["id"], "fbo", checkin)
    if cached and cached[0] == radius:
        _r, centre, results = cached
        error = None
    else:
        fbos, _err = airnav_fbos(checkin["airport_code"])
        if fbos is None:
            # AirNav errored — fall back to OSM (slow, but better than nothing).
            centre, results, error = places_at_checkin(checkin, radius, "fbo")
            if results is not None and not error:
                save_search_cache(user["id"], "fbo", checkin, radius, centre, results)
        else:
            results = [{"name": f["name"], "brand": "", "lat": None, "lon": None,
                        "address": "", "phone": f["phone"], "website": "", "cuisine": "",
                        "hours": "", "atype": "", "distance": 0.0, "logo": f.get("logo", "")} for f in fbos]
            centre = None
            code = (checkin["airport_code"] or "").strip().upper()
            if code in AIRPORT_COORDS:
                _lat, _lon, _city, _name = AIRPORT_COORDS[code]
                centre = (_lat, _lon, f"{_name}, {_city}")
            error = None
            save_search_cache(user["id"], "fbo", checkin, radius, centre, results)
    html = f'<div class="card"><h2>FBOs in the area</h2>'
    if results is None:
        html += '<p class="muted">Could not resolve the check-in location.</p>'
    else:
        html += external_results_html("fbo", (centre, results, error, radius, checkin), user, with_map=False)
    html += (f'<p><a href="{manual}">Add FBO review</a> '
             f'<span class="muted">— don\u2019t see yours? add it by name.</span></p></div>')
    fbo_rows = db.execute(
        """SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id
           WHERE r.kind = 'fbo' AND (upper(r.airport_code) = ? OR lower(r.city) = ?)
           ORDER BY r.created_at DESC, r.id DESC LIMIT 5""",
        ((checkin["airport_code"] or "").upper(), (checkin["city"] or "").strip().lower())).fetchall()
    fbo_rows = visible_reviews(fbo_rows, user)
    if fbo_rows:
        html += "".join(review_card(r, user) for r in fbo_rows)
    return html


def airnav_fbos(airport_code):
    """FBOs at a US airport from AirNav (fast, reliable; OSM has sparse FBO data).

    Returns (list of {name, phone}, error). Empty list (no error) means the
    airport page loaded but listed no FBOs.
    """
    import re
    code = (airport_code or "").strip().upper()
    if not code:
        return [], "no airport code"
    url = f"https://www.airnav.com/airport/{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "PilotCrewCommunity/1.0 (pilot FBO directory)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            h = r.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)[:100]
    start = h.find("FBO, Fuel Providers")
    if start == -1:
        return [], "no FBO section"
    section = h[start:start + 40000]
    seen = {}
    # FBO links use the canonical ICAO code (e.g. KTEB) even when the page
    # was fetched via the IATA code (TEB) — match any airport code here.
    for m in re.finditer(r"/airport/[A-Z0-9]{3,4}/([A-Z0-9_]{2,})", section):
        fcode = m.group(1)
        if fcode in seen:
            continue
        ahead = section[m.end():m.end() + 600]
        # Name is either IMG alt text or the link text itself.
        name = None
        alt_m = re.search(r'alt="([^"]+)"', ahead)
        if alt_m:
            name = alt_m.group(1).strip()
        if not name:
            txt_m = re.search(r">([^<]{2,80})</A>", ahead)
            if txt_m:
                name = txt_m.group(1).strip()
        if not name or "Add a business" in name or "://" in name:
            continue
        phone_m = re.search(r"(\d{3}-\d{3}-\d{4})", ahead)
        phone = phone_m.group(1) if phone_m else ""
        # Logo: IMG src (make absolute). Not all FBOs have one.
        logo = ""
        img_m = re.search(r'<IMG[^>]*src="([^"]+)"', ahead)
        if img_m:
            logo = img_m.group(1)
            if logo.startswith("//"):
                logo = "https:" + logo
            # Skip spacer/pixel images
            if "1dot.gif" in logo or "width=1" in ahead[ahead.find(logo)-50:ahead.find(logo)]:
                logo = ""
        seen[fcode] = {"name": name, "phone": phone, "logo": logo}
    return list(seen.values()), None


@app.route("/fbos")
@login_required
def fbo_list():
    """FBO search fragment for the Home page (lazy-loaded; cached per check-in).

    Primary source is AirNav (real FBO directory for US airports, fast).
    Falls back to the OSM name search when AirNav has nothing.
    Anchored tightly to the check-in airport (3 mi) — FBOs are on the field.
    ?lat=&lon= searches around a one-time GPS fix instead (never stored);
    the nearest airport supplies the Add-review context.
    Failed searches are NOT cached, so the list retries on the next Home
    visit and fills in on its own once the listing service recovers.
    """
    user = current_user()
    checkin = current_checkin(user["id"])
    radius = 3
    lat_arg, lon_arg = request.args.get("lat"), request.args.get("lon")
    if lat_arg and lon_arg:
        try:
            glat, glon = float(lat_arg), float(lon_arg)
            if not (-90 <= glat <= 90 and -180 <= glon <= 180):
                raise ValueError
        except (TypeError, ValueError):
            return '<p class="muted">Could not read your location.</p>'
        code, city, _name, _dist = nearest_airport_for(glat, glon)
        # GPS "near me": AirNav for the nearest airport, else OSM.
        # Works without a check-in — uses the current GPS fix.
        fbos, _err = airnav_fbos(code) if code else (None, "no airport")
        if fbos is not None:
            results = [{"name": f["name"], "brand": "", "lat": None, "lon": None,
                        "address": "", "phone": f["phone"], "website": "", "cuisine": "",
                        "hours": "", "atype": "", "distance": 0.0, "logo": f.get("logo", "")} for f in fbos]
            place = {"airport_code": code or "", "city": city or ""}
            centre = (glat, glon, "your current location")
            return external_results_html("fbo", (centre, results, None, radius, place), user, with_map=False)
        centre, results, error = places_at_coords(
            glat, glon, radius, "fbo", display="your current location", city_hint=city or "")
        place = {"airport_code": code or "", "city": city or ""}
        if error and not results:
            return ('<p class="muted">The FBO directory is temporarily unavailable '
                    '(map service is slow right now) — try again in a moment.</p>')
        return external_results_html("fbo", (centre, results, error, radius, place), user, with_map=False)
    if not checkin:
        return ""
    cached = load_search_cache(user["id"], "fbo", checkin)
    if cached and cached[0] == radius:
        _r, centre, results = cached
        return external_results_html("fbo", (centre, results, None, radius, checkin), user, with_map=False)
    # Check-in airport: AirNav first (real FBO data), OSM fallback.
    fbos, _err = airnav_fbos(checkin["airport_code"])
    if fbos is not None:
        # AirNav answered (even if zero FBOs) — authoritative for US airports.
        # Don't fall back to the slow OSM search when AirNav says none.
        results = [{"name": f["name"], "brand": "", "lat": None, "lon": None,
                    "address": "", "phone": f["phone"], "website": "", "cuisine": "",
                    "hours": "", "atype": "", "distance": 0.0, "logo": f.get("logo", "")} for f in fbos]
        centre = None
        code = (checkin["airport_code"] or "").strip().upper()
        if code in AIRPORT_COORDS:
            _lat, _lon, _city, _name = AIRPORT_COORDS[code]
            centre = (_lat, _lon, f"{_name}, {_city}")
        save_search_cache(user["id"], "fbo", checkin, radius, centre, results)
        return external_results_html("fbo", (centre, results, None, radius, checkin), user, with_map=False)
    # AirNav errored — fall back to OSM.
    centre, results, error = places_at_checkin(checkin, radius, "fbo")
    if results is not None and not error:
        save_search_cache(user["id"], "fbo", checkin, radius, centre, results)
    if results is None:
        return '<p class="muted">Could not resolve the check-in location.</p>'
    if error and not results:
        return ('<p class="muted">The FBO directory is temporarily unavailable '
                '(map service is slow right now) — it will appear on its own. '
                'You can still <a href="' +
                esc(url_for("new_review") + "?" + urllib.parse.urlencode(
                    {"kind": "fbo", "airport": checkin["airport_code"] or "",
                     "city": checkin["city"] or ""})) +
                '">add an FBO review by name</a>.</p>')
    return external_results_html("fbo", (centre, results, error, radius, checkin), user, with_map=False)


@app.route("/board", methods=["GET", "POST"])
@login_required
def board():
    user = current_user()
    db = get_db()
    flash = None
    if request.method == "POST" and request.form.get("action") == "checkin":
        place = request.form.get("place_name", "").strip() or None
        code = request.form.get("airport_code", "").strip().upper()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper() or None
        kind = request.form.get("kind", "").strip()
        kind = kind if kind in ("hotel", "restaurant") else None
        start = request.form.get("start_date") or date.today().isoformat()
        end = request.form.get("end_date") or None
        # The airport code, city, and state must verify against each other
        # before a check-in is saved.
        verified = True
        if not place:
            verified, err = verify_checkin(code, city, state)
            if not verified:
                flash = err
        if not verified:
            pass  # flash already set; fall through to re-render the form.
        elif place and kind:
            # From a search result: attach the hotel/restaurant to the current
            # check-in at this place instead of replacing the whole check-in.
            col = "hotel_place" if kind == "hotel" else "restaurant_place"
            cur = current_checkin(user["id"])
            if cur and (cur["airport_code"] or "").upper() == code and \
                    (cur["city"] or "").strip().lower() == city.strip().lower():
                db.execute(f"UPDATE checkins SET {col}=? WHERE id=?", (place, cur["id"]))
            else:
                db.execute(
                    f"INSERT INTO checkins (user_id, airport_code, city, state, {col}, start_date, end_date) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user["id"], code, city, state, place, start, end))
            db.execute("DELETE FROM search_cache WHERE user_id=? AND (upper(airport_code) != ? OR lower(city) != ?)",
                       (user["id"], code, city.strip().lower()))
            db.commit()
            return redirect(url_for("index"))
        else:
            # Main check-in form: a new check-in at a new place.
            db.execute(
                "INSERT INTO checkins (user_id, airport_code, city, state, start_date, end_date) VALUES (?,?,?,?,?,?)",
                (user["id"], code, city, state, start, end))
            # A new place means old search results no longer apply.
            db.execute("DELETE FROM search_cache WHERE user_id=? AND (upper(airport_code) != ? OR lower(city) != ?)",
                       (user["id"], code, city.strip().lower()))
            db.commit()
            # Friend nearby alerts go out only for a check-in that starts today —
            # a future-dated check-in stays silent until its date arrives.
            if start <= date.today().isoformat():
                buds = []
                for fr in db.execute(
                        "SELECT u.* FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = ?",
                        (user["id"],)).fetchall():
                    fc = current_checkin(fr["id"])
                    if fc and (fc["airport_code"].upper() == code
                               or fc["city"].strip().lower() == city.strip().lower()):
                        buds.append(fr)
                if buds:
                    names = ", ".join(fr["name"] for fr in buds)
                    flask_flash(f"👥 {names} {'is' if len(buds) == 1 else 'are'} also checked in at {code} / {city}.")
                    for fr in buds:
                        send_email(fr["email"], f"{user['name']} checked in at {code} / {city}",
                                   f"{user['name']} just checked in at {code} / {city}, where you are checked in too.\n\n"
                                   "Open Pilot & Crew Community to say hi.")
            else:
                flask_flash(f"Check-in saved for {start} — it will become your current check-in on that date.")
            return redirect(url_for("index"))
    if request.method == "POST" and request.form.get("action") == "checkout":
        cur = current_checkin(user["id"])
        if cur:
            db.execute("UPDATE checkins SET checked_out = 1 WHERE id = ?", (cur["id"],))
        db.commit()
        flash = "Left the area."
    checkin = current_checkin(user["id"])
    # Check-in history: past check-ins only (upcoming ones get their own card).
    all_rows = db.execute(
        "SELECT * FROM checkins WHERE user_id=? ORDER BY created_at DESC, id DESC", (user["id"],)).fetchall()
    upcoming_ids = {r["id"] for r in all_rows if is_upcoming(r)}
    past = [r for r in all_rows
            if r["id"] not in upcoming_ids and (not checkin or r["id"] != checkin["id"])]
    if past:
        items = "".join(
            f"<li>{checkin_label(r)} <span class='muted'>— {esc(r['start_date'])} to {esc(r['end_date'] or 'open')}</span></li>"
            for r in past)
        history_html = f'<div class="card"><h2>Check-in history</h2><ul>{items}</ul></div>'
    else:
        history_html = ""
    _state_dd = state_dropdown()
    body = f"""
      <div class="card"><h2>Check in</h2><p class="muted">Type the airport code — the city fills in automatically — or tap Find me for a one-time location fix. This is the only location this app uses.</p>
      <p><button type="button" id="findme">Find me</button></p><p class="muted" id="findme_msg"></p>
      <form method="post" onsubmit="return confirm('Check in at ' + document.getElementById('airport_code').value.toUpperCase() + ' / ' + document.getElementById('city').value + ' on ' + document.getElementsByName('start_date')[0].value + '?')"><input type="hidden" name="action" value="checkin">
      <label>Airport code</label><input id="airport_code" name="airport_code" placeholder="KTUL" autocomplete="off" required>
      <label>State</label>{_state_dd}
      <label>City</label><input name="city" id="city" placeholder="Start typing a city" autocomplete="off" required><div id="city_suggest"></div>
      <div id="city_airports"></div>
      <label>Start date</label><input type="date" name="start_date" value="{date.today().isoformat()}">
      <label>End date (optional)</label><input type="date" name="end_date">
      <button type="submit">Check in</button></form></div>{AUTOFILL_JS}{FINDME_JS}{CITY_DROPDOWN_JS}{CITY_AIRPORTS_JS}
      {upcoming_checkins_html(user)}
      {friends_nearby_html(db, user, checkin)}{area_reviews_html(db, user, checkin)}{history_html}"""
    return page("Check-In", user, body, flash=flash)


@app.route("/checkout/place", methods=["POST"])
@login_required
def checkout_place():
    """Check out of one place (the hotel slot or the restaurant slot) without ending the check-in."""
    user = current_user()
    db = get_db()
    slot = request.form.get("slot")
    checkin = current_checkin(user["id"])
    if checkin and slot in ("hotel", "restaurant"):
        col = "hotel_place" if slot == "hotel" else "restaurant_place"
        db.execute(f"UPDATE checkins SET {col}=NULL WHERE id=? AND user_id=? AND checked_out=0",
                   (checkin["id"], user["id"]))
        # Legacy rows stored the place in place_name/kind instead of the slots.
        if checkin["place_name"] and (checkin["kind"] or "hotel") == slot:
            db.execute("UPDATE checkins SET place_name=NULL, kind=NULL "
                       "WHERE id=? AND user_id=? AND checked_out=0",
                       (checkin["id"], user["id"]))
        db.commit()
        flask_flash(f"Checked out of the {slot}.")
    return redirect(url_for("index"))


@app.route("/checkin/edit", methods=["GET", "POST"])
@login_required
def edit_checkin():
    """Edit the current check-in, or an upcoming one via ?id=."""
    user = current_user()
    db = get_db()
    cid = request.values.get("id", type=int)
    if cid:
        checkin = db.execute(
            "SELECT * FROM checkins WHERE id=? AND user_id=?", (cid, user["id"])).fetchone()
        if not checkin or checkin["checked_out"] or not (is_current(checkin) or is_upcoming(checkin)):
            return redirect(url_for("board"))
    else:
        checkin = current_checkin(user["id"])
    if not checkin:
        return redirect(url_for("board"))
    if request.method == "POST":
        code = request.form.get("airport_code", "").strip().upper()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper() or None
        hotel = request.form.get("hotel_place", "").strip() or None
        restaurant = request.form.get("restaurant_place", "").strip() or None
        start = request.form.get("start_date") or checkin["start_date"]
        end = request.form.get("end_date") or None
        ok, err = verify_checkin(code, city, state)
        if not ok:
            flask_flash(err)
            return redirect(url_for("edit_checkin", id=checkin["id"]))
        db.execute(
            "UPDATE checkins SET airport_code=?, city=?, state=?, hotel_place=?, restaurant_place=?, "
            "start_date=?, end_date=? WHERE id=? AND user_id=? AND checked_out=0",
            (code, city, state, hotel, restaurant, start, end, checkin["id"], user["id"]))
        # A new place means old cached search results no longer apply.
        db.execute("DELETE FROM search_cache WHERE user_id=? AND (upper(airport_code) != ? OR lower(city) != ?)",
                   (user["id"], code, city.strip().lower()))
        db.commit()
        flask_flash("Check-in updated.")
        return redirect(url_for("index"))
    try:
        _cur_state = checkin["state"]
    except (KeyError, IndexError):
        _cur_state = ""
    _edit_state_dd = state_dropdown(_cur_state)
    # Pre-populate the city type-ahead with the current state's cities.
    _cur_city = checkin["city"] or ""
    _edit_city_dd = (f'<input name="city" id="city" value="{esc(_cur_city)}" '
                     f'placeholder="Start typing a city" autocomplete="off" required>'
                     f'<div id="city_suggest"></div>')
    body = f"""
      <div class="card"><h2>Edit check-in</h2>
      <form method="post">
      <input type="hidden" name="id" value="{checkin['id']}">
      <label>Airport code</label><input name="airport_code" value="{esc(checkin['airport_code'] or '')}" required>
      <label>State</label>{_edit_state_dd}
      <label>City</label>{_edit_city_dd}
      <label>Hotel</label><input name="hotel_place" value="{esc(checkin['hotel_place'] or '')}" placeholder="Hotel (optional)">
      <label>Restaurant</label><input name="restaurant_place" value="{esc(checkin['restaurant_place'] or '')}" placeholder="Restaurant (optional)">
      <label>Start date</label><input type="date" name="start_date" value="{esc(checkin['start_date'] or '')}">
      <label>End date (optional)</label><input type="date" name="end_date" value="{esc(checkin['end_date'] or '')}">
      <button type="submit">Save changes</button></form>
      <p><a href="{url_for('index')}">Cancel</a></p></div>{CITY_DROPDOWN_JS}"""
    return page("Edit check-in", user, body)


@app.route("/checkin/cancel", methods=["POST"])
@login_required
def cancel_checkin():
    """Cancel an upcoming (future-dated) check-in. Ownership and upcoming status enforced."""
    user = current_user()
    db = get_db()
    cid = request.form.get("id", type=int)
    if cid:
        checkin = db.execute(
            "SELECT * FROM checkins WHERE id=? AND user_id=?", (cid, user["id"])).fetchone()
        if checkin and is_upcoming(checkin):
            db.execute("DELETE FROM checkins WHERE id=?", (cid,))
            db.commit()
            flask_flash("Upcoming check-in cancelled.")
    return redirect(url_for("index"))

# ---------------------------------------------------------------- reviews
def _parse_coord(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if abs(f) <= 180 else None
@app.route("/reviews/new", methods=["GET", "POST"])
@login_required
def new_review():
    user = current_user()
    checkin = current_checkin(user["id"])
    if request.method == "POST":
        kind = request.form.get("kind", "hotel")
        if kind not in ("hotel", "restaurant", "fbo"):
            kind = "hotel"
        chosen = request.form.getlist("tag")
        custom = request.form.get("custom_tags", "").strip()
        tags = ", ".join([t for t in chosen] + ([custom] if custom else []))
        anon = 1 if request.form.get("anonymous") else 0
        db = get_db()
        params = (user["id"], kind, request.form.get("place_name", "").strip(),
                  request.form.get("airport_code", "").strip().upper(), request.form.get("city", "").strip(),
                  request.form.get("phone", "").strip(), request.form.get("address", "").strip(),
                  _parse_coord(request.form.get("lat")), _parse_coord(request.form.get("lon")),
                  int(request.form.get("rating", 5)), tags, request.form.get("notes", "").strip(), anon)
        if USE_POSTGRES:
            cur = db.execute(
                "INSERT INTO reviews (user_id, kind, place_name, airport_code, city, phone, address, lat, lon, rating, tags, notes, is_anonymous) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id", params)
            review_id = cur.fetchone()["id"]
        else:
            cur = db.execute(
                "INSERT INTO reviews (user_id, kind, place_name, airport_code, city, phone, address, lat, lon, rating, tags, notes, is_anonymous) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", params)
            review_id = cur.lastrowid
        _save_review_photos(db, review_id, request.files.getlist("photos"))
        db.commit()
        return redirect(_after_review_url(kind))
    kind = request.args.get("kind", "hotel")
    place = request.args.get("place", "")
    airport = request.args.get("airport", checkin["airport_code"] if checkin else "")
    city = request.args.get("city", checkin["city"] if checkin else "")
    phone = request.args.get("phone", "")
    address = request.args.get("address", "")
    lat = request.args.get("lat", "")
    lon = request.args.get("lon", "")
    tag_list = _tags_for_kind(kind) if kind in ("hotel", "restaurant", "fbo") else HOTEL_TAGS
    boxes = "".join(
        f'<label><input type="checkbox" name="tag" value="{esc(t)}" style="width:auto"> {esc(t)}</label><br>' for t in tag_list)
    body = f"""<div class="card"><h2>Add a {_kind_label(kind)} review</h2>
      <form method="post" enctype="multipart/form-data"><input type="hidden" name="kind" value="{esc(kind)}">
      <label>Place name</label><input name="place_name" value="{esc(place)}" required>
      <label>Airport code</label><input name="airport_code" value="{esc(airport)}">
      <label>City</label><input name="city" value="{esc(city)}">
      <label>Phone</label><input name="phone" value="{esc(phone)}" inputmode="tel">
      <label>Address</label><input name="address" value="{esc(address)}">
      <input type="hidden" name="lat" value="{esc(lat)}"><input type="hidden" name="lon" value="{esc(lon)}">
      <label>Star rating</label><select name="rating">
      <option value="5">5 stars</option><option value="4">4 stars</option><option value="3">3 stars</option>
      <option value="2">2 stars</option><option value="1">1 star</option></select>
      <label>Pilot tags</label><div>{boxes}</div>
      <label>Other tags (comma-separated)</label><input name="custom_tags">
      <label>Notes</label><textarea name="notes" rows="4"></textarea>
      <label>Photos (up to 5)</label><input type="file" name="photos" accept="image/*" multiple>
      <label><input type="checkbox" name="anonymous" value="1" style="width:auto"> Post anonymously</label>
      <button type="submit">Save review</button></form></div>"""
    return page("Add review", user, body)

def _edit_review_form(vals, kind, tag_list, photo_rows):
    existing_tags = [t.strip() for t in (vals.get("tags") or "").split(",") if t.strip()]
    boxes = "".join(
        f'<label><input type="checkbox" name="tag" value="{esc(t)}" style="width:auto"'
        f'{" checked" if t in existing_tags else ""}> {esc(t)}</label><br>'
        for t in tag_list)
    custom = ", ".join(t for t in existing_tags if t not in tag_list)
    try:
        rating = int(vals.get("rating", 5))
    except (TypeError, ValueError):
        rating = 5
    opts = "".join(
        f'<option value="{n}"{" selected" if rating == n else ""}>{n} star{"s" if n > 1 else ""}</option>'
        for n in (5, 4, 3, 2, 1))
    thumbs = ""
    if photo_rows:
        thumbs = "".join(
            f'<label style="display:inline-block;margin:4px;text-align:center;vertical-align:top">'
            f'<img src="{url_for("photo", photo_id=p["id"])}" alt="Review photo" '
            f'style="width:90px;height:90px;object-fit:cover;border-radius:8px;display:block">'
            f'<input type="checkbox" name="remove_photo" value="{p["id"]}" style="width:auto"> remove</label>'
            for p in photo_rows)
        thumbs = f"<div>{thumbs}</div>"
    remaining = 5 - len(photo_rows)
    return f"""<div class="card"><h2>Edit {_kind_label(kind)} review</h2>
      <form method="post" enctype="multipart/form-data">
      <label>Place name</label><input name="place_name" value="{esc(vals.get('place_name', ''))}" required>
      <label>Airport code</label><input name="airport_code" value="{esc(vals.get('airport_code', ''))}">
      <label>City</label><input name="city" value="{esc(vals.get('city', ''))}">
      <label>Phone</label><input name="phone" value="{esc(vals.get('phone', ''))}" inputmode="tel">
      <label>Address</label><input name="address" value="{esc(vals.get('address', ''))}">
      <input type="hidden" name="lat" value="{esc(str(vals.get('lat') or ''))}">
      <input type="hidden" name="lon" value="{esc(str(vals.get('lon') or ''))}">
      <label>Star rating</label><select name="rating">{opts}</select>
      <label>Pilot tags</label><div>{boxes}</div>
      <label>Other tags (comma-separated)</label><input name="custom_tags" value="{esc(custom)}">
      <label>Notes</label><textarea name="notes" rows="4">{esc(vals.get('notes', ''))}</textarea>
      <label>Photos (up to 5 total{f' — {remaining} more allowed' if remaining else ''})</label>{thumbs}
      <input type="file" name="photos" accept="image/*" multiple>
      <label><input type="checkbox" name="anonymous" value="1" style="width:auto"{' checked' if vals.get('is_anonymous') else ''}> Post anonymously</label>
      <button type="submit">Save changes</button></form></div>"""


@app.route("/reviews/<int:review_id>/edit", methods=["GET", "POST"])
@login_required
def edit_review(review_id):
    user = current_user()
    db = get_db()
    r = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not r or r["user_id"] != user["id"]:
        return "Not found", 404
    kind = r["kind"] if r["kind"] in ("hotel", "restaurant", "fbo") else "hotel"
    tag_list = _tags_for_kind(kind)
    photo_rows = db.execute("SELECT id FROM photos WHERE review_id=? ORDER BY id", (review_id,)).fetchall()
    if request.method == "POST":
        vals = {
            "place_name": request.form.get("place_name", "").strip(),
            "airport_code": request.form.get("airport_code", "").strip().upper(),
            "city": request.form.get("city", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "address": request.form.get("address", "").strip(),
            "lat": _parse_coord(request.form.get("lat")),
            "lon": _parse_coord(request.form.get("lon")),
            "rating": request.form.get("rating", 5),
            "notes": request.form.get("notes", "").strip(),
        }
        chosen = request.form.getlist("tag")
        custom = request.form.get("custom_tags", "").strip()
        vals["tags"] = ", ".join([t for t in chosen] + ([custom] if custom else []))
        vals["is_anonymous"] = 1 if request.form.get("anonymous") else 0
        try:
            rating = int(vals["rating"])
        except (TypeError, ValueError):
            rating = 5
        rating = max(1, min(5, rating))
        vals["rating"] = rating
        # Photo budget: removals first, then new uploads, max 5 total.
        remove_ids = {int(x) for x in request.form.getlist("remove_photo") if x.isdigit()}
        kept = [p for p in photo_rows if p["id"] not in remove_ids]
        new_files = [f for f in request.files.getlist("photos") if f and f.filename]
        if len(kept) + len(new_files) > 5:
            return page("Edit review", user,
                        _edit_review_form(vals, kind, tag_list, photo_rows),
                        error=f"That would be {len(kept) + len(new_files)} photos — the limit is 5 per review.")
        db.execute(
            "UPDATE reviews SET place_name=?, airport_code=?, city=?, phone=?, address=?, lat=?, lon=?, rating=?, tags=?, notes=?, is_anonymous=? "
            "WHERE id=? AND user_id=?",
            (vals["place_name"], vals["airport_code"], vals["city"], vals["phone"], vals["address"],
             vals["lat"], vals["lon"], rating, vals["tags"], vals["notes"], vals["is_anonymous"], review_id, user["id"]))
        for pid in remove_ids:
            db.execute("DELETE FROM photos WHERE id=? AND review_id=?", (pid, review_id))
        _save_review_photos(db, review_id, new_files)
        db.commit()
        return redirect(_after_review_url(kind))
    vals = {"place_name": r["place_name"] or "", "airport_code": r["airport_code"] or "",
            "city": r["city"] or "", "phone": r["phone"] or "", "address": r["address"] or "",
            "lat": r["lat"], "lon": r["lon"], "is_anonymous": r["is_anonymous"],
            "rating": r["rating"], "tags": r["tags"] or "", "notes": r["notes"] or ""}
    return page("Edit review", user, _edit_review_form(vals, kind, tag_list, photo_rows))


@app.route("/reviews/<int:review_id>/report", methods=["GET", "POST"])
@login_required
def report_review(review_id):
    user = current_user()
    db = get_db()
    r = db.execute("SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id WHERE r.id=?",
                   (review_id,)).fetchone()
    if not r:
        return "Not found", 404
    back = _after_review_url(r["kind"])
    if r["user_id"] == user["id"]:
        return redirect(back)
    if request.method == "POST":
        reason = request.form.get("reason", "").strip() or "Other"
        try:
            db.execute("INSERT INTO reports (review_id, reporter_id, reason) VALUES (?,?,?)",
                       (review_id, user["id"], reason))
            db.commit()
        except _INTEGRITY_ERRORS:
            db.rollback()
        if hasattr(g, "_hidden"):
            delattr(g, "_hidden")
        return redirect(back)
    body = f"""<div class="card"><h2>Report review</h2>
      <p class="muted">Flag <strong>{esc(r['place_name'])}</strong> ({r['rating']}★ by {esc(_author_name(r))}). Reviews flagged by 3 pilots are hidden.</p>
      <form method="post"><label>Reason</label><select name="reason">
      <option>Spam</option><option>Inappropriate</option><option>Inaccurate</option><option>Other</option>
      </select><button type="submit">Send report</button></form></div>"""
    return page("Report review", user, body)


@app.route("/photo/<int:photo_id>")
@login_required
def photo(photo_id):
    row = get_db().execute("SELECT data FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        return "Not found", 404
    return Response(base64.b64decode(row["data"]), mimetype="image/jpeg")

@app.route("/profile-photo/<int:user_id>")
@login_required
def profile_photo(user_id):
    row = get_db().execute("SELECT profile_photo FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["profile_photo"]:
        return "Not found", 404
    return Response(base64.b64decode(row["profile_photo"]), mimetype="image/jpeg")

@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@login_required
def delete_review(review_id):
    user = current_user()
    db = get_db()
    db.execute("DELETE FROM photos WHERE review_id IN (SELECT id FROM reviews WHERE id=? AND user_id=?)",
               (review_id, user["id"]))
    db.execute("DELETE FROM reports WHERE review_id IN (SELECT id FROM reviews WHERE id=? AND user_id=?)",
               (review_id, user["id"]))
    db.execute("DELETE FROM reviews WHERE id = ? AND user_id = ?", (review_id, user["id"]))
    db.commit()
    return redirect(url_for("board"))

# ---------------------------------------------------------------- hotels / restaurants
def save_search_cache(user_id, kind, checkin, radius, centre, results):
    """Keep the last at-my-location search so it survives page switches."""
    db = get_db()
    centre_json = json.dumps(list(centre)) if centre else None
    results_json = json.dumps(results or [])
    if USE_POSTGRES:
        db.execute(
            """INSERT INTO search_cache (user_id, kind, airport_code, city, radius, centre, results)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (user_id, kind) DO UPDATE SET
                 airport_code=EXCLUDED.airport_code, city=EXCLUDED.city, radius=EXCLUDED.radius,
                 centre=EXCLUDED.centre, results=EXCLUDED.results, created_at=now()""",
            (user_id, kind, checkin["airport_code"], checkin["city"], radius, centre_json, results_json))
    else:
        db.execute(
            """INSERT OR REPLACE INTO search_cache (user_id, kind, airport_code, city, radius, centre, results)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, kind, checkin["airport_code"], checkin["city"], radius, centre_json, results_json))
    db.commit()

def load_search_cache(user_id, kind, checkin):
    """(radius, centre, results) from the last search, if it was for this check-in place."""
    row = get_db().execute(
        "SELECT radius, centre, results, airport_code, city FROM search_cache WHERE user_id=? AND kind=?",
        (user_id, kind)).fetchone()
    if not row:
        return None
    if (row["airport_code"] or "").upper() != (checkin["airport_code"] or "").upper() or \
       (row["city"] or "").strip().lower() != (checkin["city"] or "").strip().lower():
        return None
    centre = tuple(json.loads(row["centre"])) if row["centre"] else None
    return row["radius"], centre, json.loads(row["results"] or "[]")

def at_location_section(kind, user, radius_options, default_radius):
    """The 'at my location' card: check-in location and/or one-tap GPS.
    GPS coordinates are used for that one search only — never stored, no tracking.
    Search results are kept until the pilot checks in somewhere else."""
    checkin = current_checkin(user["id"])
    radius_raw = (request.form.get("radius") if request.method == "POST"
                  else request.args.get("radius")) or str(default_radius)
    try:
        radius = int(radius_raw)
    except ValueError:
        radius = default_radius
    # Hotels, restaurants, attractions: 5/10/20 miles, capped at 20.
    if kind in ("restaurant", "attraction"):
        radius_options = [5, 10, 20]
        radius = max(1, min(radius, 20))
    else:
        radius_options = [5, 10, 20]
        radius = max(1, min(radius, 20))
    data = None
    gps_error = None
    if request.method == "POST" and request.form.get("gps") == "1":
        try:
            lat = float(request.form.get("lat", ""))
            lon = float(request.form.get("lon", ""))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (TypeError, ValueError):
            lat = lon = None
            gps_error = "Could not read your location. Try again."
        if lat is not None:
            code, city, _name, _dist = nearest_airport_for(lat, lon)
            centre, results, error = places_at_coords(
                lat, lon, radius, kind, display="your current location", city_hint=city or "")
            # Nearest airport gives the Add review / Check in buttons an airport + city.
            data = (centre, results, error, radius,
                    {"airport_code": code or "", "city": city or ""})
    elif checkin and request.args.get("at_checkin") == "1":
        centre, results, error = places_at_checkin(checkin, radius, kind)
        if results is not None:
            save_search_cache(user["id"], kind, checkin, radius, centre, results)
        data = (centre, results, error, radius, checkin)
    elif checkin:
        cached = load_search_cache(user["id"], kind, checkin)
        if cached:
            radius, centre, results = cached
            data = (centre, results, None, radius, checkin)
        else:
            radius = default_radius
    radios = "".join(
        f'<label><input type="radio" name="radius" value="{r}" style="width:auto" {"checked" if r == radius else ""}> {r} miles</label> '
        for r in radius_options)
    gps_radios = "".join(
        f'<label><input type="radio" name="gps_radius" value="{r}" style="width:auto" {"checked" if r == radius else ""}> {r} miles</label> '
        for r in radius_options)
    if checkin:
        checkin_part = (f'<p class="muted">Uses your check-in location: {esc(checkin["airport_code"])} · '
                        f'{esc(checkin["city"])}.</p>'
                        f'<form method="get"><input type="hidden" name="at_checkin" value="1">{radios}'
                        f'<button type="submit">Search {kind}s at airport location</button></form>')
    else:
        checkin_part = ('<p class="muted">Check in on the <a href="' + url_for("board") + '">Check-In</a> '
                        'page to also search around your check-in location.</p>')
    card = f"""<div class="card"><h2>Search {kind}s at my location</h2>{checkin_part}
      <p class="muted">Or use your current GPS location — one tap, used for this search only, never stored.</p>
      <div>{gps_radios}</div>
      <p><button type="button" id="gps_btn_{kind}">Use my current location</button>
      <span class="muted" id="gps_msg_{kind}"></span></p>
      <form id="gps_form_{kind}" method="post" style="display:none">
      <input type="hidden" name="gps" value="1">
      <input type="hidden" name="radius" id="gps_radius_val_{kind}" value="{radius}">
      <input type="hidden" name="lat" id="gps_lat_{kind}">
      <input type="hidden" name="lon" id="gps_lon_{kind}"></form></div>
      <script>
      (function(){{
        var btn = document.getElementById('gps_btn_{kind}');
        var msg = document.getElementById('gps_msg_{kind}');
        btn.addEventListener('click', function(){{
          if (!navigator.geolocation) {{ msg.textContent = 'Location is not available on this device.'; return; }}
          msg.textContent = 'Getting your location…';
          navigator.geolocation.getCurrentPosition(function(pos){{
            var sel = document.querySelector('input[name="gps_radius"]:checked');
            document.getElementById('gps_radius_val_{kind}').value = sel ? sel.value : '{radius}';
            document.getElementById('gps_lat_{kind}').value = pos.coords.latitude;
            document.getElementById('gps_lon_{kind}').value = pos.coords.longitude;
            document.getElementById('gps_form_{kind}').submit();
          }}, function(){{
            msg.textContent = 'Location unavailable — allow location access for this site and try again.';
          }}, {{timeout: 15000, maximumAge: 60000}});
        }});
      }})();
      </script>"""
    if gps_error:
        card += f'<div class="error">{esc(gps_error)}</div>'
    return card, data, checkin

def external_results_html(kind, data, user, with_map=True):
    centre, results, error, radius, place = data
    kind_label = _kind_label(kind)
    if error and not results:
        return f'<div class="error">{esc(error)}</div>'
    if centre:
        lat, lon, display = centre
        header = f"<h2>{len(results)} {kind_label}s within {radius} mi of {esc(display)}</h2>"
    else:
        header = f"<h2>{len(results)} {kind_label}s within {radius} mi</h2>"
    if not results:
        return header + '<p class="muted">No places found in that radius.</p>'
    markers = [ {"lat": r["lat"], "lon": r["lon"], "name": r["name"]} for r in results
               if r.get("lat") is not None and r.get("lon") is not None]
    map_html = (f'<div class="resultsmap" data-markers="{esc(json.dumps(markers))}"></div>'
                if markers and with_map else "")
    cards = ""
    for r in results:
        detail = ""
        if kind == "hotel":
            if r.get("brand") and r["brand"] != r["name"]:
                detail = f"<div>Brand: {esc(r['brand'])}</div>"
        elif kind == "restaurant":
            if r.get("cuisine"):
                detail += f"<div>Cuisine: {esc(r['cuisine'])}</div>"
            if r.get("hours"):
                detail += f"<div>Hours: {esc(r['hours'])}</div>"
        elif kind == "fbo" and r.get("brand"):
            detail = f"<div>Brand: {esc(r['brand'])}</div>"
        elif r.get("atype"):
            detail = f"<div>Type: {esc(r['atype'])}</div>"
        web = f'<div><a href="{esc(r["website"])}" target="_blank" rel="noopener">Website</a></div>' if r["website"] else ""
        addr = f"<div>Address: {esc(r['address'])}</div>" if r.get("address") else ""
        phone = (f"<div>Phone: <a href=\"tel:{esc(r['phone'])}\">{esc(r['phone'])}</a></div>"
                 if r.get("phone") else "")
        if kind == "attraction":
            # No reviews or check-ins for attractions — link the name to an OSM search.
            loc_url = ("https://www.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"query": f"{r['name']}, {place['city']}"}))
            name_html = (f'<a href="{loc_url}" target="_blank" rel="noopener">'
                         f'<strong>{esc(r["name"])}</strong></a>')
            action_html = ""
        else:
            add = (url_for("new_review") + "?" + urllib.parse.urlencode(
                {"kind": kind, "place": r["name"], "airport": place["airport_code"], "city": place["city"],
                 "phone": r["phone"] or "", "address": r["address"] or "",
                 "lat": r.get("lat") or "", "lon": r.get("lon") or ""}))
            checkin_btn = ""
            if kind in ("hotel", "restaurant") and place["airport_code"]:
                _label = "Staying Here" if kind == "hotel" else "Eating here"
                checkin_btn = (f'<form method="post" action="{url_for("board")}" style="margin-top:8px" onsubmit="return confirm(\'{_label}?\')">'
                    f'<input type="hidden" name="action" value="checkin">'
                    f'<input type="hidden" name="airport_code" value="{esc(place["airport_code"])}">'
                    f'<input type="hidden" name="city" value="{esc(place["city"])}">'
                    f'<input type="hidden" name="place_name" value="{esc(r["name"])}">'
                    f'<input type="hidden" name="kind" value="{kind}">'
                    f'<button type="submit" style="width:auto;padding:6px 12px">{_label}</button></form>')
            name_html = f'<a href="{add}"><strong>{esc(r["name"])}</strong></a>'
            action_html = f'<p><a href="{add}">Add review</a></p>{checkin_btn}'
            # Tapping a result name shows the place on the map (new tab), like saved
            # reviews and attractions — the Add review link is the path to the review form.
            loc_url = ("https://www.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"query": f"{r['name']}, {place['city']}"}))
            name_html = (f'<a href="{loc_url}" target="_blank" rel="noopener">'
                         f'<strong>{esc(r["name"])}</strong></a>')
        cards += f"""<div class="card">{name_html} <span class="muted">{r['distance']} mi</span>
          {f'<div><img src="{esc(r["logo"])}" alt="{esc(r["name"])} logo" style="max-width:200px;max-height:60px;margin:8px 0"></div>' if r.get("logo") else ""}
          {detail}{addr}{phone}{web}
          {action_html}</div>"""
    return header + map_html + cards

@app.route("/attractions", methods=["GET", "POST"])
@login_required
def attractions():
    user = current_user()
    card, data, _ = at_location_section("attraction", user, [5, 10, 20], 10)
    ext = external_results_html("attraction", data, user) if data else ""
    body = card + ext
    return page("Attractions", user, body)


@app.route("/airport/<code>")
@login_required
def airport_hub(code):
    """One page per airport: top hotels/restaurants, recent reviews, friends here."""
    user = current_user()
    code = (code or "").strip().upper()
    db = get_db()
    city, aname = "", ""
    if code in AIRPORT_COORDS:
        _lat, _lon, city, aname = AIRPORT_COORDS[code]
    rows = db.execute(
        """SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id
           WHERE upper(r.airport_code) = ? OR lower(r.city) = ?
           ORDER BY r.created_at DESC, r.id DESC""",
        (code, (city or "").strip().lower())).fetchall()
    if not city:
        for r in rows:
            if (r["airport_code"] or "").upper() == code and r["city"]:
                city = r["city"]
                break
        if not city:
            c = db.execute("SELECT city FROM checkins WHERE upper(airport_code)=? LIMIT 1", (code,)).fetchone()
            if c:
                city = c["city"]
    rows = visible_reviews(rows, user)
    hotels_agg = place_aggregates([r for r in rows if r["kind"] == "hotel"])[:5]
    rests_agg = place_aggregates([r for r in rows if r["kind"] == "restaurant"])[:5]
    def agg_chips(aggs, kind):
        if not aggs:
            return '<p class="muted">No reviews yet.</p>'
        dest = url_for("hotels") if kind == "hotel" else url_for("restaurants")
        return ("<p style='line-height:2.2'>" + "".join(
            f'<a class="tag" href="{dest}?q={urllib.parse.quote(a["name"])}&scope=all">'
            f'★{a["avg"]:.1f} ({a["n"]}) · {esc(a["name"])}</a> ' for a in aggs) + "</p>")
    friend_rows = db.execute(
        "SELECT u.* FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = ?",
        (user["id"],)).fetchall()
    buds = []
    for fr in friend_rows:
        fc = current_checkin(fr["id"])
        if fc and (fc["airport_code"].upper() == code
                   or (city and fc["city"].strip().lower() == city.strip().lower())):
            buds.append(f"<li>{esc(fr['name'])} — {checkin_label(fc)}</li>")
    buds_html = "".join(buds) or "<li class='muted'>No friends checked in here.</li>"
    cards = "".join(review_card(r, user) for r in rows[:10]) or '<p class="muted">No reviews here yet — be the first.</p>'
    title = f"{code} · {city}" if city else code
    body = f"""<div class="card"><h2>{esc(title)}</h2>
      {f'<p class="muted">{esc(aname)}</p>' if aname else ''}
      <p class="muted">{len(rows)} pilot reviews</p></div>
      <div class="card"><h2>Top hotels</h2>{agg_chips(hotels_agg, "hotel")}</div>
      <div class="card"><h2>Top restaurants</h2>{agg_chips(rests_agg, "restaurant")}</div>
      <div class="card"><h2>Friends here</h2><ul>{buds_html}</ul></div>
      <div class="card"><h2>Recent reviews</h2></div>{cards}"""
    return page(title, user, body)


@app.route("/hotels", methods=["GET", "POST"])
@login_required
def hotels():
    user = current_user()
    q = request.args.get("q", "")
    scope = request.args.get("scope", "in")
    sort = request.args.get("sort", "new")
    tag = request.args.get("tag", "")
    card, data, _ = at_location_section("hotel", user, [5, 10, 20], 5)
    ext = external_results_html("hotel", data, user) if data else ""
    body = card + ext + saved_reviews_section("hotel", user, q, scope, sort, tag)
    return page("Hotels", user, body)

@app.route("/restaurants", methods=["GET", "POST"])
@login_required
def restaurants():
    user = current_user()
    q = request.args.get("q", "")
    scope = request.args.get("scope", "in")
    sort = request.args.get("sort", "new")
    tag = request.args.get("tag", "")
    card, data, _ = at_location_section("restaurant", user, [5, 10, 20], 5)
    ext = external_results_html("restaurant", data, user) if data else ""
    body = card + ext + saved_reviews_section("restaurant", user, q, scope, sort, tag)
    return page("Restaurants", user, body)

# ---------------------------------------------------------------- friends
def _add_friendship(db, a, b):
    # Mutual friendship rows; safe to call twice.
    if USE_POSTGRES:
        db.execute("INSERT INTO friends (user_id, friend_id) VALUES (?,?) "
                   "ON CONFLICT (user_id, friend_id) DO NOTHING", (a, b))
        db.execute("INSERT INTO friends (user_id, friend_id) VALUES (?,?) "
                   "ON CONFLICT (user_id, friend_id) DO NOTHING", (b, a))
    else:
        db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (a, b))
        db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (b, a))
def send_email(to_addr, subject, body):
    """Send a plain-text email via SMTP if configured. Never raises.
    Configure with env vars: SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME,
    SMTP_PASSWORD, SMTP_FROM."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host or not to_addr:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
        username = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        from_addr = os.environ.get("SMTP_FROM", "") or username
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            if username:
                s.login(username, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
        return True
    except Exception:
        return False


def _request_friend(db, user, fid, friend_ids):
    """Send a friend request to fid (or approve the reverse one). Returns (flash, error)."""
    target = db.execute("SELECT id, name FROM users WHERE id=?", (fid,)).fetchone()
    if not target or fid == user["id"]:
        return None, "Pilot not found."
    if fid in friend_ids:
        return None, f"'{esc(target['name'])}' is already your friend."
    reverse = db.execute("SELECT id FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                         (fid, user["id"])).fetchone()
    outgoing = db.execute("SELECT id FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                          (user["id"], fid)).fetchone()
    if reverse:
        # They already asked you — adding them back approves it.
        _add_friendship(db, user["id"], fid)
        db.execute("DELETE FROM friend_requests WHERE id=?", (reverse["id"],))
        db.commit()
        friend_ids.add(fid)
        return f"You are now friends with {esc(target['name'])}.", None
    if outgoing:
        return None, f"You already sent {esc(target['name'])} a request."
    db.execute("INSERT INTO friend_requests (from_user_id, to_user_id) VALUES (?,?)",
               (user["id"], fid))
    db.commit()
    to_user = db.execute("SELECT email, name FROM users WHERE id=?", (fid,)).fetchone()
    if to_user:
        login_url = request.host_url.rstrip("/") + url_for("friends")
        send_email(to_user["email"],
                   "New friend request on Pilots and Crew Community",
                   f"Hi {to_user['name']},\n\n{user['name']} sent you a friend request "
                   f"on Pilots and Crew Community.\n\nLog in at {login_url} "
                   f"to approve or decline it.\n")
    return f"Friend request sent to {esc(target['name'])}. They need to approve it.", None


@app.route("/friends", methods=["GET", "POST"])
@login_required
def friends():
    user = current_user()
    db = get_db()
    flash = None
    error = None
    duplicates = []
    # Search box filters the friends list by name, company (airline), or plane (aircraft).
    # request.values covers the GET search form and POST actions that keep ?q= in the URL.
    fq = (request.values.get("q") or "").strip()
    friend_ids = {r["friend_id"] for r in db.execute(
        "SELECT friend_id FROM friends WHERE user_id=?", (user["id"],)).fetchall()}
    if request.method == "POST":
        remove_id = request.form.get("remove_id")
        if remove_id:
            rid = int(remove_id)
            db.execute("DELETE FROM friends WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)",
                       (user["id"], rid, rid, user["id"]))
            db.commit()
            friend_ids.discard(rid)
            flash = "Friend removed."
        pilot_id = request.form.get("pilot_id", "").strip()
        pilot_name = request.form.get("pilot_name", "").strip()
        if pilot_id.isdigit():
            # Chosen from the duplicate-name list.
            flash, error = _request_friend(db, user, int(pilot_id), friend_ids)
        elif pilot_name:
            want = pilot_name.lower()
            named = [u for u in db.execute("SELECT * FROM users WHERE id != ?", (user["id"],)).fetchall()
                     if (u["name"] or "").strip().lower() == want]
            new = [u for u in named if u["id"] not in friend_ids]
            if len(new) == 1:
                flash, error = _request_friend(db, user, new[0]["id"], friend_ids)
            elif len(new) > 1:
                duplicates = new
            elif named:
                error = f"'{esc(pilot_name)}' is already your friend."
            else:
                error = f"No pilot named '{esc(pilot_name)}' found. Use the name on their account."
        req_action = request.form.get("req_action")
        req_id = request.form.get("request_id")
        if req_action in ("approve", "decline", "cancel") and req_id:
            fr = db.execute("SELECT * FROM friend_requests WHERE id=?", (int(req_id),)).fetchone()
            if fr and req_action in ("approve", "decline") and fr["to_user_id"] == user["id"]:
                if req_action == "approve":
                    _add_friendship(db, user["id"], fr["from_user_id"])
                    friend_ids.add(fr["from_user_id"])
                    other = db.execute("SELECT name FROM users WHERE id=?", (fr["from_user_id"],)).fetchone()
                    flash = f"You are now friends with {esc(other['name']) if other else 'them'}."
                else:
                    flash = "Request declined."
                db.execute("DELETE FROM friend_requests WHERE id=?", (fr["id"],))
                db.commit()
            elif fr and req_action == "cancel" and fr["from_user_id"] == user["id"]:
                db.execute("DELETE FROM friend_requests WHERE id=?", (fr["id"],))
                db.commit()
                flash = "Request cancelled."
    rows = db.execute(
        "SELECT u.* FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = ?", (user["id"],)
    ).fetchall()
    if fq:
        fl = fq.lower()
        rows = [fr for fr in rows
                if fl in (fr["name"] or "").lower()
                or fl in (fr["airline"] or "").lower()
                or fl in (fr["aircraft"] or "").lower()]
    checkin = current_checkin(user["id"])
    items = ""
    for fr in rows:
        fc = current_checkin(fr["id"])
        nearby = ""
        if checkin and fc and area_matches(checkin, fc["airport_code"], fc["city"]):
            nearby = f' — <strong>nearby: {checkin_label(fc)}</strong>'
        elif fc:
            nearby = f' — checked in: {checkin_label(fc)}'
        else:
            nearby = ' — no active check-in'
        _search = esc(" ".join(x for x in [fr["name"], fr["airline"], fr["aircraft"]] if x).lower())
        items += (f"<li data-search=\"{_search}\">{_avatar_html(fr['id'])}{esc(fr['name'])} ({esc(fr['role'] or 'pilot').title()} · {esc(fr['airline'])} · {esc(fr['base'])} · {esc(fr['aircraft'])}){nearby}"
                  f' <form method="post" style="display:inline;margin-left:8px">'
                  f'<input type="hidden" name="remove_id" value="{fr["id"]}">'
                  f'<button type="submit" style="background:#7f1d1d;width:auto;padding:4px 10px">Remove</button>'
                  f"</form></li>")
    others = db.execute(
        "SELECT * FROM users WHERE id != ? AND id NOT IN (SELECT friend_id FROM friends WHERE user_id = ?)",
        (user["id"], user["id"])).fetchall()
    if others:
        add_card = (f'<div class="card"><h2>Add a friend</h2><form method="post">'
                    f"<label>Pilot's name (as on their account)</label>"
                    f'<input id="add_friend_input" name="pilot_name" placeholder="Jane Doe" autocomplete="off" required>'
                    f'<button type="submit">Add friend</button></form>')
        add_items = "".join(
            f'<li data-search="{esc(((o["name"] or "").split() or [""])[0])}" '
            f'data-name="{esc(o["name"])}" style="cursor:pointer">'
            f'{_avatar_html(o["id"])}{esc(o["name"])} '
            f'<span class="muted">· {esc(o["airline"] or "")} {esc(o["base"] or "")} {esc(o["aircraft"] or "")}</span></li>'
            for o in others)
        add_card += (f'<p class="muted" id="add_friend_count"></p>'
                     f'<ul id="add_friend_list">{add_items}</ul>'
                     f'<script>(function(){{var input=document.getElementById("add_friend_input"),'
                     f'items=document.querySelectorAll("#add_friend_list li"),'
                     f'count=document.getElementById("add_friend_count");'
                     f'function filter(){{var q=input.value.trim().toLowerCase(),n=0;'
                     f'items.forEach(function(li){{var hit=q&&li.getAttribute("data-search").toLowerCase().indexOf(q)===0;'
                     f'li.style.display=hit?"":"none";if(hit)n++;}});'
                     f'count.textContent=q?(n+(n===1?" match":" matches")):"Type a name to find pilots.";}}'
                     f'items.forEach(function(li){{li.addEventListener("click",function(){{'
                     f'input.value=li.getAttribute("data-name");'
                     f'items.forEach(function(x){{x.style.display="none";}});'
                     f'count.textContent="";input.focus();}});}});'
                     f'input.addEventListener("input",filter);filter();}})();</script>')
        if duplicates:
            dup_items = "".join(
                f"<li>{_avatar_html(m['id'])}{esc(m['name'])} ({esc(m['role'] or 'pilot').title()} · {esc(m['airline'])} · {esc(m['base'])} · {esc(m['aircraft'])})"
                f' <form method="post" style="display:inline;margin-left:8px">'
                f'<input type="hidden" name="pilot_id" value="{m["id"]}">'
                f'<button type="submit" style="width:auto;padding:4px 10px">Send request</button>'
                f"</form></li>" for m in duplicates)
            add_card += (f'<p class="muted">More than one pilot has that name — pick the right one:</p>'
                         f'<ul>{dup_items}</ul>')
        add_card += '</div>'
    else:
        add_card = ('<div class="card"><h2>Add a friend</h2>'
                    '<p class="muted">No more pilots to add — everyone here is already your friend.</p></div>')
    req_items = ""
    for q in db.execute(
            "SELECT r.id, r.from_user_id, u.name, u.airline, u.base, u.aircraft FROM friend_requests r "
            "JOIN users u ON u.id = r.from_user_id WHERE r.to_user_id=?", (user["id"],)).fetchall():
        req_items += (f"<li>{_avatar_html(q['from_user_id'])}{esc(q['name'])} ({esc(q['airline'])} · {esc(q['base'])} · {esc(q['aircraft'])})"
                      f' <form method="post" style="display:inline;margin-left:8px">'
                      f'<input type="hidden" name="request_id" value="{q["id"]}">'
                      f'<button type="submit" name="req_action" value="approve" style="width:auto;padding:4px 10px">Approve</button> '
                      f'<button type="submit" name="req_action" value="decline" style="background:#7f1d1d;width:auto;padding:4px 10px">Decline</button>'
                      f"</form></li>")
    for q in db.execute(
            "SELECT r.id, r.to_user_id, u.name, u.airline, u.base, u.aircraft FROM friend_requests r "
            "JOIN users u ON u.id = r.to_user_id WHERE r.from_user_id=?", (user["id"],)).fetchall():
        req_items += (f'<li class="muted">Request sent to {esc(q["name"])} — waiting for approval'
                      f' <form method="post" style="display:inline;margin-left:8px">'
                      f'<input type="hidden" name="request_id" value="{q["id"]}">'
                      f'<button type="submit" name="req_action" value="cancel" style="background:#7f1d1d;width:auto;padding:4px 10px">Cancel</button>'
                      f"</form></li>")
    req_card = f'<div class="card"><h2>Friend requests</h2><ul>{req_items}</ul></div>' if req_items else ""
    search_html = (f'<form method="get" style="margin-bottom:12px">'
                   f'<input id="friend_search" name="q" placeholder="Search by name, company, or plane" '
                   f'value="{esc(fq)}" autocomplete="off"></form>'
                   f'<p class="muted" id="friend_count"></p>')
    if fq:
        search_html += (f'<p class="muted"><a href="{url_for("friends")}">Clear search</a></p>')
    list_html = items or ('<li class="muted">No friends match.</li>' if fq
                          else '<li class="muted">No friends yet.</li>')
    no_match = ('<li class="muted" id="friend_no_match" style="display:none">No friends match.</li>'
                if items else "")
    filter_js = (f'<script>(function(){{var input=document.getElementById("friend_search"),'
                 f'items=document.querySelectorAll("#friend_list li[data-search]"),'
                 f'count=document.getElementById("friend_count"),'
                 f'none=document.getElementById("friend_no_match");'
                 f'function filter(){{var q=input.value.trim().toLowerCase(),n=0;'
                 f'items.forEach(function(li){{var hit=!q||li.getAttribute("data-search").indexOf(q)!==-1;'
                 f'li.style.display=hit?"":"none";if(hit)n++;}});'
                 f'if(none)none.style.display=(q&&n===0)?"":"none";'
                 f'if(count)count.textContent=q?(n+(n===1?" match":" matches")):"";}}'
                 f'input.addEventListener("input",filter);filter();}})();</script>')
    body = f"""<div class="card"><h2>Friends</h2>
      <p class="muted">Friends show as nearby only when their check-in matches yours.</p>
      {search_html}
      <ul id="friend_list">{list_html}{no_match}</ul></div>
      {req_card}
      {add_card}{filter_js}"""
    return page("Friends", user, body, flash=flash, error=error)

# ---------------------------------------------------------------- messages
def _conversation_rows(user_id):
    """All messages involving the user, newest first, with the other party's name."""
    return get_db().execute(
        "SELECT m.*, u.name AS other_name FROM messages m "
        "JOIN users u ON u.id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END "
        "WHERE m.sender_id = ? OR m.recipient_id = ? "
        "ORDER BY m.created_at DESC, m.id DESC",
        (user_id, user_id, user_id)).fetchall()


def _notify_message(sender, to_id, body):
    """Email the recipient about a new message (silent no-op if email is off)."""
    to_user = get_db().execute("SELECT email, name FROM users WHERE id=?", (to_id,)).fetchone()
    if not to_user:
        return
    thread_url = request.host_url.rstrip("/") + url_for("conversation", user_id=sender["id"])
    send_email(to_user["email"],
               f"New message from {sender['name']} on Pilots and Crew Community",
               f"Hi {to_user['name']},\n\n{sender['name']} sent you a message "
               f"on Pilots and Crew Community:\n\n\"{body[:500]}\"\n\n"
               f"Log in at {thread_url} to reply.\n")


@app.route("/messages", methods=["GET", "POST"])
@login_required
def messages():
    """Inbox: one row per conversation, plus every registered pilot to message."""
    user = current_user()
    db = get_db()
    if request.method == "POST":
        to_id = request.form.get("to_id", type=int)
        body = (request.form.get("body") or "").strip()
        error = None
        if not to_id or to_id == user["id"]:
            error = "Pick someone to message."
        elif not body:
            error = "Write something first."
        elif not db.execute("SELECT id FROM users WHERE id = ?", (to_id,)).fetchone():
            error = "That pilot no longer exists."
        if error:
            return page("Messages", user, _messages_html(user, db), error=error)
        db.execute("INSERT INTO messages (sender_id, recipient_id, body) VALUES (?,?,?)",
                   (user["id"], to_id, body[:2000]))
        db.commit()
        _notify_message(user, to_id, body)
        return redirect(url_for("conversation", user_id=to_id))
    return page("Messages", user, _messages_html(user, db))


def _messages_html(user, db):
    rows = _conversation_rows(user["id"])
    convos, order = {}, []
    for r in rows:
        oid = r["recipient_id"] if r["sender_id"] == user["id"] else r["sender_id"]
        if oid not in convos:
            convos[oid] = {"name": r["other_name"], "latest": r, "unread": 0}
            order.append(oid)
        if r["recipient_id"] == user["id"] and not r["is_read"]:
            convos[oid]["unread"] += 1
    if order:
        items = "".join(
            f'<li>{_avatar_html(oid)}<a href="{url_for("conversation", user_id=oid)}">'
            f'<strong>{esc(convos[oid]["name"])}</strong></a>'
            f'{" <span class=\"msgdot\">" + str(convos[oid]["unread"]) + "</span>" if convos[oid]["unread"] else ""}'
            f'<br><span class="muted">{esc((convos[oid]["latest"]["body"] or "")[:80])}</span></li>'
            for oid in order)
        inbox = f'<div class="card"><h2>Conversations</h2><ul>{items}</ul></div>'
    else:
        inbox = '<div class="card"><h2>Conversations</h2><p class="muted">No messages yet.</p></div>'
    others = db.execute(
        "SELECT id, name, airline, base FROM users WHERE id != ? ORDER BY name", (user["id"],)).fetchall()
    fresh = [o for o in others if o["id"] not in convos]
    if fresh:
        new_items = "".join(
            f'<li data-search="{esc(((o["name"] or "").split() or [""])[0])}">'
            f'{_avatar_html(o["id"])}<a href="{url_for("conversation", user_id=o["id"])}">{esc(o["name"])}</a> '
            f'<span class="muted">· {esc(o["airline"] or "")} {esc(o["base"] or "")}</span></li>'
            for o in fresh)
        new_card = (f'<div class="card"><h2>Message someone new</h2>'
                    f'<p class="muted">Anyone registered — no need to be friends first.</p>'
                    f'<input id="pilot_search" placeholder="Type a connection\u2019s name\u2026" autocomplete="off">'
                    f'<p class="muted" id="pilot_count"></p>'
                    f'<ul id="pilot_list">{new_items}</ul></div>'
                    f'<script>(function(){{var input=document.getElementById("pilot_search"),'
                    f'items=document.querySelectorAll("#pilot_list li"),'
                    f'count=document.getElementById("pilot_count");'
                    f'function filter(){{var q=input.value.trim().toLowerCase(),n=0;'
                    f'items.forEach(function(li){{var hit=q&&li.getAttribute("data-search").toLowerCase().indexOf(q)===0;'
                    f'li.style.display=hit?"":"none";if(hit)n++;}});'
                    f'count.textContent=q?(n+(n===1?" match":" matches")):"Type a name to find connections.";}}'
                    f'input.addEventListener("input",filter);filter();}})();</script>')
    else:
        new_card = ""
    return inbox + new_card


@app.route("/messages/<int:user_id>", methods=["GET", "POST"])
@login_required
def conversation(user_id):
    """Thread with one pilot; viewing marks their messages read."""
    user = current_user()
    db = get_db()
    other = db.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not other or other["id"] == user["id"]:
        return redirect(url_for("messages"))
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if body:
            db.execute("INSERT INTO messages (sender_id, recipient_id, body) VALUES (?,?,?)",
                       (user["id"], other["id"], body[:2000]))
            db.commit()
            _notify_message(user, other["id"], body)
        return redirect(url_for("conversation", user_id=other["id"]))
    db.execute("UPDATE messages SET is_read = 1 WHERE sender_id = ? AND recipient_id = ? AND is_read = 0",
               (other["id"], user["id"]))
    db.commit()
    rows = db.execute(
        "SELECT * FROM messages WHERE (sender_id = ? AND recipient_id = ?) "
        "OR (sender_id = ? AND recipient_id = ?) ORDER BY created_at, id",
        (user["id"], other["id"], other["id"], user["id"])).fetchall()
    msg_html = []
    for r in rows:
        mine = r["sender_id"] == user["id"]
        style = ("margin-left:24px;background:#0284c7;border-color:#38bdf8" if mine
                 else "margin-right:24px")
        ts_style = ' style="color:#e0f2fe"' if mine else ""
        who = "You" if mine else other["name"]
        msg_html.append(
            f'<div class="card" style="{style}">'
            f'<p style="margin:0 0 4px"><strong>{esc(who)}</strong> '
            f'<span class="muted"{ts_style}>{esc(str(r["created_at"])[:16])}</span></p>'
            f'<p style="margin:0;white-space:pre-wrap">{esc(r["body"])}</p></div>')
    msgs = "".join(msg_html) or '<p class="muted">No messages yet — say hello.</p>'
    body = (f'<div class="card"><h2>{_avatar_html(other["id"], 40)}{esc(other["name"])}</h2>'
            f'<form method="post"><label>Message</label>'
            f'<textarea name="body" rows="3" required placeholder="Write a message…"></textarea>'
            f'<button type="submit">Send</button></form>'
            f'<p><a href="{url_for("messages")}">Back to messages</a></p></div>'
            f'{msgs}')
    return page("Messages", user, body)

# ---------------------------------------------------------------- PWA
PWA_ICON_SVG = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'%3E"
    "%3Crect width='512' height='512' rx='112' fill='%230f172a'/%3E"
    "%3Ctext x='256' y='352' font-size='300' text-anchor='middle'%3E%E2%9C%88%EF%B8%8F%3C/text%3E%3C/svg%3E"
)

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Pilot & Crew Community",
        "short_name": "PilotCrew",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [{"src": PWA_ICON_SVG, "sizes": "any", "type": "image/svg+xml"}],
    })

SW_JS = """const CACHE = "pcc-v1";
const CORE = ["/static/airports.json", "/offline", "/manifest.json"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin || e.request.method !== "GET") return;
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/offline")));
    return;
  }
  e.respondWith(
    caches.match(e.request).then((hit) => {
      const net = fetch(e.request).then((res) => {
        if (res.ok) {
          const cp = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, cp));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
"""

@app.route("/sw.js")
def service_worker():
    return Response(SW_JS, mimetype="application/javascript")

@app.route("/offline")
def offline():
    return page("Offline", None,
                '<div class="card"><h2>You are offline</h2>'
                '<p class="muted">Check your connection and try again.</p>'
                '<p><a href="/">Retry</a></p></div>')

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
