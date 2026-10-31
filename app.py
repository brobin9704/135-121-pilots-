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
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, session, url_for
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

HOTEL_TAGS = [
    "quiet rooms", "blackout curtains", "crew shuttle", "crew rate",
    "good wifi", "free breakfast", "gym", "close to airport",
]

RESTAURANT_TAGS = [
    "open late", "late kitchen", "crew discount", "walking distance",
    "fast service", "healthy options", "good breakfast", "crew friendly",
]

# Airport coordinates for the "Find me" button (server-side only; OurAirports,
# public domain). Maps CODE -> [lat, lon, city, name].
AIRPORT_COORDS = {}
try:
    _coords_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "airports_coords.json")
    with open(_coords_path, encoding="utf-8") as _f:
        AIRPORT_COORDS = json.load(_f)
except (OSError, ValueError):
    pass

# ---------------------------------------------------------------- database
SQLITE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, airline TEXT, base TEXT, aircraft TEXT,
        email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        airport_code TEXT NOT NULL, city TEXT NOT NULL, place_name TEXT, kind TEXT,
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
        kind TEXT NOT NULL CHECK(kind IN ('hotel','restaurant')),
        place_name TEXT NOT NULL, airport_code TEXT, city TEXT,
        rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        tags TEXT, notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

POSTGRES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL, airline TEXT, base TEXT, aircraft TEXT,
        email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS checkins (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        airport_code TEXT NOT NULL, city TEXT NOT NULL, place_name TEXT, kind TEXT,
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
        kind TEXT NOT NULL CHECK(kind IN ('hotel','restaurant')),
        place_name TEXT NOT NULL, airport_code TEXT, city TEXT,
        rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        tags TEXT, notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
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
            cur.execute(POSTGRES_SCHEMA)
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS place_name TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS kind TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS hotel_place TEXT")
            cur.execute("ALTER TABLE checkins ADD COLUMN IF NOT EXISTS restaurant_place TEXT")
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
        for _col in ("hotel_place", "restaurant_place"):
            try:
                db.execute(f"ALTER TABLE checkins ADD COLUMN {_col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        db.execute("UPDATE checkins SET hotel_place = place_name WHERE kind='hotel' AND place_name IS NOT NULL AND hotel_place IS NULL")
        db.execute("UPDATE checkins SET restaurant_place = place_name WHERE kind='restaurant' AND place_name IS NOT NULL AND restaurant_place IS NULL")
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

def is_current(checkin):
    if not checkin:
        return False
    if checkin["checked_out"]:
        return False
    end = checkin["end_date"]
    if end and end < date.today().isoformat():
        return False
    return True

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
            row["notes"] or "", row["tags"] or "", row["author"] or "",
        ]).lower()
        return all(w in hay for w in words)
    all_results = [r for r in rows if matches(r)]
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
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8")).get("elements", [])

def overpass_query(lat, lon, radius_miles, amenity_filter):
    """Race all mirrors in parallel; first successful response wins."""
    import concurrent.futures
    radius_m = int(radius_miles * 1609.34)
    ql = (
        f'[out:json][timeout:30];'
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
    """Fallback when Overpass is busy: search by place type near the city, then filter by distance."""
    term = "hotels" if kind == "hotel" else "restaurants"
    params = urllib.parse.urlencode({
        "q": f"{term} near {city}", "format": "json", "limit": 50, "extratags": 1, "addressdetails": 1,
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
        address = r.get("display_name", "")
        out.append({
            "name": r.get("name") or address.split(",")[0], "brand": "",
            "address": address,
            "phone": extra.get("phone") or extra.get("contact:phone") or "",
            "website": extra.get("website") or extra.get("contact:website") or "",
            "cuisine": extra.get("cuisine", ""), "hours": extra.get("opening_hours", ""),
            "distance": round(dist, 1),
        })
    out.sort(key=lambda x: x["distance"])
    return out

def places_at_checkin(checkin, radius_miles, kind):
    """Real OpenStreetMap results around the check-in place. No device location."""
    if kind == "hotel":
        filt = 'tourism~"hotel|motel|guest_house|hostel"'
    else:
        filt = 'amenity~"restaurant|cafe|fast_food"'
    centre = None
    for query in (f"{checkin['airport_code']} airport {checkin['city']}",
                  f"{checkin['airport_code']} {checkin['city']}",
                  checkin["city"]):
        centre = geocode_place(query)
        if centre:
            break
    if not centre:
        return None, None, "Could not resolve the check-in place. Check the airport code and city, then try again."
    lat, lon, display = centre
    try:
        elements = overpass_query(lat, lon, radius_miles, filt)
    except Exception:
        # Overpass mirrors are busy: fall back to a place-type search, filtered by distance.
        fallback = nominatim_fallback(lat, lon, radius_miles, kind, checkin["city"])
        if fallback:
            return (lat, lon, display), fallback, None
        return (lat, lon, display), [], "Listing service is busy — try again in a moment."
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
        dist = haversine_miles(lat, lon, float(elat), float(elon))
        if dist > radius_miles + 0.05:
            continue
        street = " ".join(x for x in [tags.get("addr:housenumber"), tags.get("addr:street")] if x)
        addr_parts = [street, tags.get("addr:city"), tags.get("addr:postcode")]
        results.append({
            "name": name, "brand": tags.get("brand", ""),
            "address": ", ".join(p for p in addr_parts if p),
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "website": tags.get("website") or tags.get("contact:website") or "",
            "cuisine": tags.get("cuisine", ""),
            "hours": tags.get("opening_hours", ""),
            "distance": round(dist, 1),
        })
    results.sort(key=lambda r: r["distance"])
    return (lat, lon, display), results, None

# ---------------------------------------------------------------- rendering
CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
a{color:#7dd3fc}.wrap{max-width:760px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px}
nav{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
nav a,.btn{background:#1e293b;color:#e2e8f0;text-decoration:none;padding:10px 14px;border-radius:10px;border:1px solid #334155;display:inline-block}
nav a.active{background:#0369a1}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:16px;margin:12px 0}
input,select,textarea{width:100%;padding:10px;margin:6px 0 12px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;box-sizing:border-box}
button,.btn-primary{background:#0284c7;color:#fff;border:0;padding:12px 16px;border-radius:10px;font-size:16px;cursor:pointer;width:100%}
label{font-weight:600}.muted{color:#94a3b8}.stars{color:#facc15;letter-spacing:2px}
.tag{display:inline-block;background:#0f172a;border:1px solid #475569;border-radius:999px;padding:2px 10px;margin:2px;font-size:13px}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.thumbs img{width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid #475569}
.flash{background:#365314;border:1px solid #65a30d;padding:10px;border-radius:10px;margin:12px 0}
.error{background:#7f1d1d;border:1px solid #ef4444;padding:10px;border-radius:10px;margin:12px 0}
.appbar{display:flex;align-items:center;gap:12px;padding:10px 0}
#menu_btn{background:#1e293b;border:1px solid #334155;border-radius:10px;width:44px;height:44px;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;flex:none}
#menu_btn span{display:block;width:20px;height:2px;background:#e2e8f0}
.appbar_title{font-size:20px;font-weight:800}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .2s;z-index:40}
#drawer{position:fixed;top:0;left:0;bottom:0;width:240px;background:#1e293b;border-right:1px solid #334155;transform:translateX(-105%);transition:transform .25s ease;z-index:50;padding:14px 0;box-shadow:8px 0 24px rgba(0,0,0,.5);box-sizing:border-box}
body.menu_open #scrim{opacity:1;pointer-events:auto}
body.menu_open #drawer{transform:none}
.drawer_head{padding:2px 16px 10px;font-weight:800;font-size:13px;color:#7dd3fc;letter-spacing:1px}
#drawer a{display:block;padding:11px 16px;color:#e2e8f0;text-decoration:none;font-size:15px}
#drawer a.active{background:#0369a1}
#drawer a.logout{color:#94a3b8}
.home_btn{margin-left:auto;background:#1e293b;border:1px solid #334155;border-radius:10px;width:44px;height:44px;flex:none;display:flex;align-items:center;justify-content:center}
@media(max-width:600px){h1{font-size:22px}.wrap{padding:12px}}
"""

# Auto-fills the city when an airport code is typed on the Check-In form.
# Data: static/airports.json (OurAirports, public domain; large + medium airports).
AUTOFILL_JS = """<script>
(function(){
  var codeEl=document.getElementById('airport_code'),cityEl=document.getElementById('city');
  if(!codeEl||!cityEl)return;
  var db=null,loading=false;
  function lookup(){
    var code=codeEl.value.trim().toUpperCase();
    if(code.length<3||!db)return;
    var city=db[code];
    if(city&&(!cityEl.value||cityEl.dataset.auto==='1')){cityEl.value=city;cityEl.dataset.auto='1';}
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
FINDME_JS = """<script>
(function(){
  var btn=document.getElementById('findme'),msg=document.getElementById('findme_msg');
  var codeEl=document.getElementById('airport_code'),cityEl=document.getElementById('city');
  if(!btn||!codeEl||!cityEl)return;
  function say(t){ if(msg)msg.textContent=t; }
  btn.addEventListener('click',function(){
    if(!navigator.geolocation){ say('Location is not supported on this device. Type the airport code instead.'); return; }
    btn.disabled=true; var orig=btn.textContent; btn.textContent='Locating…';
    function done(){ btn.disabled=false; btn.textContent=orig; }
    navigator.geolocation.getCurrentPosition(function(pos){
      fetch('/nearest_airport?lat='+pos.coords.latitude+'&lon='+pos.coords.longitude)
        .then(function(r){ return r.json(); })
        .then(function(d){
          if(d.code){
            codeEl.value=d.code; cityEl.value=d.city; cityEl.dataset.auto='1';
            say('Nearest airport: '+d.code+' ('+d.name+'), '+d.distance_mi+' mi away.');
          } else { say('No airport found near you. Type the airport code instead.'); }
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

def page(title, user, body, flash=None, error=None):
    chrome = ""
    if user:
        links = [("Check-In", url_for("board")), ("Hotels", url_for("hotels")),
                 ("Restaurants", url_for("restaurants")), ("Friends", url_for("friends")),
                 ("My Profile", url_for("profile"))]
        items = "".join(
            f'<a href="{u}" class="{"active" if t == title else ""}">{t}</a>' for t, u in links)
        items += f'<a href="{url_for("logout")}" class="logout">Log out ({esc(user["name"])})</a>'
        chrome = f"""<div class="appbar"><button id="menu_btn" aria-label="Open menu"><span></span><span></span><span></span></button>
          <span class="appbar_title">{esc(title)}</span>
          <a href="{url_for('board')}" class="home_btn" aria-label="Home"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#e2e8f0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg></a></div>
        <div id="scrim"></div><aside id="drawer"><div class="drawer_head">PILOTS AND CREW COMMUNITY</div>{items}</aside>
        <script>
        document.getElementById('menu_btn').addEventListener('click',function(){{document.body.classList.add('menu_open')}});
        document.getElementById('scrim').addEventListener('click',function(){{document.body.classList.remove('menu_open')}});
        </script>"""
    msg = f'<div class="flash">{flash}</div>' if flash else ""
    err = f'<div class="error">{error}</div>' if error else ""
    return render_template_string(f"""<!doctype html><html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{{{{ title }}}} - Pilots and Crew Community</title><style>{CSS}</style></head>
      <body><div class="wrap">{chrome}{msg}{err}{body}</div></body></html>""",
      title=title)

def esc(s):
    import html
    return html.escape(str(s or ""))

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

def review_photos_html(review_id):
    rows = get_db().execute("SELECT id FROM photos WHERE review_id=? ORDER BY id", (review_id,)).fetchall()
    if not rows:
        return ""
    thumbs = "".join(
        f'<a href="{url_for("photo", photo_id=r["id"])}" target="_blank" rel="noopener">'
        f'<img src="{url_for("photo", photo_id=r["id"])}" alt="Review photo" loading="lazy"></a>'
        for r in rows)
    return f'<div class="thumbs">{thumbs}</div>'

def review_card(r, user):
    tags = "".join(f'<span class="tag">{esc(t.strip())}</span>' for t in (r["tags"] or "").split(",") if t.strip())
    delete = ""
    if user and r["user_id"] == user["id"]:
        delete = f"""<form method="post" action="{url_for('delete_review', review_id=r['id'])}" style="margin-top:10px">
          <button type="submit" style="background:#7f1d1d;width:auto;padding:8px 12px">Delete my review</button></form>"""
    loc_query = ", ".join(p for p in [r["place_name"] or "", r["city"] or ""] if p)
    loc_url = "https://www.openstreetmap.org/search?" + urllib.parse.urlencode({"query": loc_query})
    photos = review_photos_html(r["id"])
    return f"""<div class="card"><a href="{loc_url}" target="_blank" rel="noopener"><strong>{esc(r['place_name'])}</strong></a> <span class="stars">{stars(r['rating'])}</span>
      <div class="muted">{esc(r['airport_code'])} · {esc(r['city'])} · by {esc(r['author'])}</div>
      <div>{tags}</div><p>{esc(r['notes'])}</p>{photos}{delete}</div>"""

def saved_reviews_section(kind, user, q, scope):
    in_results, all_results, checkin = search_saved_reviews(kind, q, scope, user)
    label = "hotel" if kind == "hotel" else "restaurant"
    other_count = 0
    hint = ""
    if scope == "in" and checkin and not in_results and all_results:
        other_count = len(all_results)
        hint = (f'<div class="card">Found in another area — see all areas. '
                f'<a href="?q={urllib.parse.quote(q or "")}&scope=all">See all areas ({other_count})</a></div>')
    scope_links = ""
    if checkin:
        scope_links = (f'<p>Scope: <a href="?q={urllib.parse.quote(q or "")}&scope=in">In this area</a> | '
                       f'<a href="?q={urllib.parse.quote(q or "")}&scope=all">All areas</a> '
                       f'(current check-in: {esc(checkin["airport_code"])} · {esc(checkin["city"])})</p>')
    else:
        scope_links = '<p class="muted">No active check-in — showing all areas. Check in on the Check-In page to scope to your area.</p>'
    cards = "".join(review_card(r, user) for r in in_results) or '<p class="muted">No saved reviews match yet.</p>'
    return f"""
      <div class="card"><h2>Search saved pilot {label} reviews</h2>
      <form method="get"><label>Search saved pilot {label} reviews</label>
      <input type="text" name="q" value="{esc(q)}" placeholder="Place, airport code, city, notes, or tag">
      <input type="hidden" name="scope" value="{esc(scope)}">
      <button type="submit">Search</button></form>
      <p class="muted">Searches pilot reviews saved in this app — place name, airport code, city, notes, and tags. Every word must match somewhere.</p>
      {scope_links}</div>{hint}{cards}"""

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
        params = (name, request.form.get("airline", "").strip(), request.form.get("base", "").strip(),
                  request.form.get("aircraft", "").strip(), email, generate_password_hash(password))
        try:
            if USE_POSTGRES:
                cur = db.execute(
                    "INSERT INTO users (name, airline, base, aircraft, email, password_hash) "
                    "VALUES (?,?,?,?,?,?) RETURNING id", params)
                new_id = cur.fetchone()["id"]
            else:
                cur = db.execute(
                    "INSERT INTO users (name, airline, base, aircraft, email, password_hash) VALUES (?,?,?,?,?,?)",
                    params)
                new_id = cur.lastrowid
            db.commit()
        except _INTEGRITY_ERRORS:
            db.rollback()
            return page("Register", None, register_form(), error="That email is already registered. Try logging in.")
        session["user_id"] = new_id
        return redirect(url_for("board"))
    return page("Register", None, register_form())

def register_form():
    return """<div class="card"><h2>Create a pilot account</h2><p class="muted">Open to all pilots.</p>
    <form method="post"><label>Name</label><input name="name" required>
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
            session["user_id"] = row["id"]
            return redirect(url_for("board"))
        return login_page(error="Email or password did not match.")
    return login_page()

def login_page(error=None):
    """Full-bleed photo login page. Standalone document, not wrapped in page()."""
    err = f'<div class="error">{error}</div>' if error else ""
    return render_template_string(f"""<!doctype html><html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Log in - Pilots and Crew Community</title><style>
      body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#e2e8f0;
        min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}}
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
      </style></head><body><div class="bg"></div><div class="scrim"></div>
      <div class="card"><h1>Pilots and Crew Community</h1>
      <p class="tag">The Pilots and Crew Community App is where pilots share real layover intel. Check in at your overnight, leave your honest opinion of the hotels and restaurants nearby, and read what other pilots thought before you book, eat, or sleep.</p>
      {err}<form method="post">
      <label>Email</label><input type="email" name="email" required autocomplete="email">
      <label>Password</label><input type="password" name="password" required autocomplete="current-password">
      <button type="submit">Log in</button></form>
      <p class="reg">No account yet? <a href="/register">Register</a></p></div>
      </body></html>""")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        if not name or not email:
            return page("My Profile", user, profile_form(user), error="Name and email are required.")
        clash = db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email, user["id"])).fetchone()
        if clash:
            return page("My Profile", user, profile_form(user), error="That email is already used by another account.")
        db.execute("UPDATE users SET name=?, airline=?, base=?, aircraft=?, email=? WHERE id=?",
                   (name, request.form.get("airline", "").strip(), request.form.get("base", "").strip(),
                    request.form.get("aircraft", "").strip(), email, user["id"]))
        db.commit()
        user = current_user()
        return page("My Profile", user, profile_form(user), flash="Profile updated.")
    return page("My Profile", user, profile_form(user))

def profile_form(user):
    return f"""<div class="card"><h2>My Profile</h2>
    <form method="post"><label>Name</label><input name="name" value="{esc(user['name'])}" required>
    <label>Airline</label><input name="airline" value="{esc(user['airline'])}">
    <label>Base</label><input name="base" value="{esc(user['base'])}">
    <label>Aircraft</label><input name="aircraft" value="{esc(user['aircraft'])}">
    <label>Email</label><input type="email" name="email" value="{esc(user['email'])}" required>
    <button type="submit">Save</button></form></div>"""

@app.route("/")
def index():
    return redirect(url_for("board"))

# ---------------------------------------------------------------- board
@app.route("/nearest_airport")
@login_required
def nearest_airport():
    """Nearest airport to a tap-initiated GPS fix. Nothing is stored."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400
    if not AIRPORT_COORDS:
        return jsonify({"error": "airport data unavailable"}), 503
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
    code, city, name = best
    return jsonify({"code": code, "city": city, "name": name, "distance_mi": round(best_d, 1)})

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
        kind = request.form.get("kind", "").strip()
        kind = kind if kind in ("hotel", "restaurant") else None
        start = request.form.get("start_date") or date.today().isoformat()
        end = request.form.get("end_date") or None
        if place and kind:
            # From a search result: attach the hotel/restaurant to the current
            # check-in at this place instead of replacing the whole check-in.
            col = "hotel_place" if kind == "hotel" else "restaurant_place"
            cur = current_checkin(user["id"])
            if cur and (cur["airport_code"] or "").upper() == code and \
                    (cur["city"] or "").strip().lower() == city.strip().lower():
                db.execute(f"UPDATE checkins SET {col}=? WHERE id=?", (place, cur["id"]))
            else:
                db.execute(
                    f"INSERT INTO checkins (user_id, airport_code, city, {col}, start_date, end_date) "
                    "VALUES (?,?,?,?,?,?)",
                    (user["id"], code, city, place, start, end))
        else:
            # Main check-in form: a new check-in at a new place.
            db.execute(
                "INSERT INTO checkins (user_id, airport_code, city, start_date, end_date) VALUES (?,?,?,?,?)",
                (user["id"], code, city, start, end))
        # A new place means old search results no longer apply.
        db.execute("DELETE FROM search_cache WHERE user_id=? AND (upper(airport_code) != ? OR lower(city) != ?)",
                   (user["id"], code, city.strip().lower()))
        db.commit()
        flash = f"Checked in at {esc(place)}." if place else "Checked in."
    if request.method == "POST" and request.form.get("action") == "checkout":
        db.execute("UPDATE checkins SET checked_out = 1 WHERE user_id = ? AND checked_out = 0", (user["id"],))
        db.commit()
        flash = "Checked out."
    checkin = current_checkin(user["id"])
    if checkin:
        def _place_link(place, kind):
            url = (url_for("new_review") + "?" + urllib.parse.urlencode(
                {"kind": kind, "place": place,
                 "airport": checkin["airport_code"], "city": checkin["city"]}))
            return f'<a href="{url}">{esc(place)}</a>'
        status_lines = []
        if checkin["hotel_place"]:
            status_lines.append(f"Hotel: {_place_link(checkin['hotel_place'], 'hotel')}")
        if checkin["restaurant_place"]:
            status_lines.append(f"Restaurant: {_place_link(checkin['restaurant_place'], 'restaurant')}")
        if not status_lines and checkin["place_name"]:
            # Legacy rows (pre hotel/restaurant split).
            status_lines.append(_place_link(checkin["place_name"], checkin["kind"] or "hotel"))
        status_lines.append(f'{esc(checkin["airport_code"])} · {esc(checkin["city"])}')
        status_html = "<br>".join(status_lines)
        checkin_html = f"""<div class="card"><h2>Current check-in</h2>
          <p><strong>{status_html}</strong></p>
          <p class="muted">{esc(checkin['start_date'])} to {esc(checkin['end_date'] or 'open')}</p>
          <form method="post"><input type="hidden" name="action" value="checkout">
          <button type="submit">Check out</button></form></div>"""
    else:
        checkin_html = '<div class="card"><h2>Current check-in</h2><p class="muted">No active check-in.</p></div>'
    # Friends nearby: matching check-ins only.
    friend_rows = db.execute(
        "SELECT u.* FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = ?", (user["id"],)
    ).fetchall()
    nearby = []
    for fr in friend_rows:
        fc = current_checkin(fr["id"])
        if checkin and fc and (fc["airport_code"].upper() == checkin["airport_code"].upper()
                               or fc["city"].strip().lower() == checkin["city"].strip().lower()):
            nearby.append(f"{esc(fr['name'])} — {checkin_label(fc)}")
    nearby_html = "".join(f"<li>{n}</li>" for n in nearby) or "<li class='muted'>No friends checked in to the same area.</li>"
    # Check-in history: everything except the current check-in, newest first.
    all_rows = db.execute(
        "SELECT * FROM checkins WHERE user_id=? ORDER BY created_at DESC, id DESC", (user["id"],)).fetchall()
    past = [r for r in all_rows if not checkin or r["id"] != checkin["id"]]
    if past:
        items = "".join(
            f"<li>{checkin_label(r)} <span class='muted'>— {esc(r['start_date'])} to {esc(r['end_date'] or 'open')}</span></li>"
            for r in past)
        history_html = f'<div class="card"><h2>Check-in history</h2><ul>{items}</ul></div>'
    else:
        history_html = ""
    # Pilot reviews in the current check-in area, newest first.
    if checkin:
        rev_rows = db.execute(
            """SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id
               WHERE upper(r.airport_code) = ? OR lower(r.city) = ?
               ORDER BY r.created_at DESC, r.id DESC LIMIT 20""",
            ((checkin["airport_code"] or "").upper(), (checkin["city"] or "").strip().lower())).fetchall()
        reviews_html = '<div class="card"><h2>Pilot reviews in this area</h2>'
        if rev_rows:
            reviews_html += (f'<p class="muted">{len(rev_rows)} reviews at '
                             f'{esc(checkin["airport_code"])} · {esc(checkin["city"])}</p></div>')
            reviews_html += "".join(review_card(r, user) for r in rev_rows)
        else:
            reviews_html += '<p class="muted">No pilot reviews here yet — be the first.</p></div>'
    else:
        reviews_html = ""
    body = f"""{checkin_html}
      <div class="card"><h2>Check in</h2><p class="muted">Type the airport code — the city fills in automatically — or tap Find me for a one-time location fix. This is the only location this app uses.</p>
      <p><button type="button" id="findme">Find me</button></p><p class="muted" id="findme_msg"></p>
      <form method="post"><input type="hidden" name="action" value="checkin">
      <label>Airport code</label><input id="airport_code" name="airport_code" placeholder="KTUL" autocomplete="off" required>
      <label>City</label><input id="city" name="city" placeholder="Tulsa" required>
      <label>Start date</label><input type="date" name="start_date" value="{date.today().isoformat()}">
      <label>End date (optional)</label><input type="date" name="end_date">
      <button type="submit">Check in</button></form></div>{AUTOFILL_JS}{FINDME_JS}
      <div class="card"><h2>Friends in the same area</h2><ul>{nearby_html}</ul>
      <p><a href="{url_for('friends')}">Manage friends</a></p></div>{reviews_html}{history_html}"""
    return page("Check-In", user, body, flash=flash)

# ---------------------------------------------------------------- reviews
@app.route("/reviews/new", methods=["GET", "POST"])
@login_required
def new_review():
    user = current_user()
    checkin = current_checkin(user["id"])
    if request.method == "POST":
        kind = request.form.get("kind", "hotel")
        if kind not in ("hotel", "restaurant"):
            kind = "hotel"
        chosen = request.form.getlist("tag")
        custom = request.form.get("custom_tags", "").strip()
        tags = ", ".join([t for t in chosen] + ([custom] if custom else []))
        db = get_db()
        params = (user["id"], kind, request.form.get("place_name", "").strip(),
                  request.form.get("airport_code", "").strip().upper(), request.form.get("city", "").strip(),
                  int(request.form.get("rating", 5)), tags, request.form.get("notes", "").strip())
        if USE_POSTGRES:
            cur = db.execute(
                "INSERT INTO reviews (user_id, kind, place_name, airport_code, city, rating, tags, notes) "
                "VALUES (?,?,?,?,?,?,?,?) RETURNING id", params)
            review_id = cur.fetchone()["id"]
        else:
            cur = db.execute(
                "INSERT INTO reviews (user_id, kind, place_name, airport_code, city, rating, tags, notes) "
                "VALUES (?,?,?,?,?,?,?,?)", params)
            review_id = cur.lastrowid
        _save_review_photos(db, review_id, request.files.getlist("photos"))
        db.commit()
        return redirect(url_for("hotels" if kind == "hotel" else "restaurants"))
    kind = request.args.get("kind", "hotel")
    place = request.args.get("place", "")
    airport = request.args.get("airport", checkin["airport_code"] if checkin else "")
    city = request.args.get("city", checkin["city"] if checkin else "")
    tag_list = RESTAURANT_TAGS if kind == "restaurant" else HOTEL_TAGS
    boxes = "".join(
        f'<label><input type="checkbox" name="tag" value="{esc(t)}" style="width:auto"> {esc(t)}</label><br>' for t in tag_list)
    body = f"""<div class="card"><h2>Add a {esc(kind)} review</h2>
      <form method="post" enctype="multipart/form-data"><input type="hidden" name="kind" value="{esc(kind)}">
      <label>Place name</label><input name="place_name" value="{esc(place)}" required>
      <label>Airport code</label><input name="airport_code" value="{esc(airport)}">
      <label>City</label><input name="city" value="{esc(city)}">
      <label>Star rating</label><select name="rating">
      <option value="5">5 stars</option><option value="4">4 stars</option><option value="3">3 stars</option>
      <option value="2">2 stars</option><option value="1">1 star</option></select>
      <label>Pilot tags</label><div>{boxes}</div>
      <label>Other tags (comma-separated)</label><input name="custom_tags">
      <label>Notes</label><textarea name="notes" rows="4"></textarea>
      <label>Photos (up to 5)</label><input type="file" name="photos" accept="image/*" multiple>
      <button type="submit">Save review</button></form></div>"""
    return page("Add review", user, body)

@app.route("/photo/<int:photo_id>")
@login_required
def photo(photo_id):
    row = get_db().execute("SELECT data FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        return "Not found", 404
    return Response(base64.b64decode(row["data"]), mimetype="image/jpeg")

@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@login_required
def delete_review(review_id):
    user = current_user()
    db = get_db()
    db.execute("DELETE FROM photos WHERE review_id IN (SELECT id FROM reviews WHERE id=? AND user_id=?)",
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
    """The 'at my location' card. Location = check-in only.
    Search results are kept until the pilot checks in somewhere else."""
    checkin = current_checkin(user["id"])
    if not checkin:
        return ('<div class="card"><h2>Search ' + kind + 's at my location</h2>'
                '<p>Check in on the <a href="' + url_for("board") + '">Check-In</a> first, '
                'then come back here to search around your check-in location.</p></div>', None, None)
    radius_raw = request.args.get("radius", str(default_radius))
    try:
        radius = int(radius_raw)
    except ValueError:
        radius = default_radius
    # Hotels: 2/5/10. Restaurants: 5/10/20, default 20, capped at 20.
    if kind == "restaurant":
        radius_options = [5, 10, 20]
        radius = max(1, min(radius, 20))
    else:
        radius = max(1, min(radius, 10))
    if request.args.get("at_checkin") == "1":
        centre, results, error = places_at_checkin(checkin, radius, kind)
        if results is not None:
            save_search_cache(user["id"], kind, checkin, radius, centre, results)
        data = (centre, results, error, radius, checkin)
    else:
        cached = load_search_cache(user["id"], kind, checkin)
        if cached:
            radius, centre, results = cached
            data = (centre, results, None, radius, checkin)
        else:
            radius, data = default_radius, None
    radios = "".join(
        f'<label><input type="radio" name="radius" value="{r}" style="width:auto" {"checked" if r == radius else ""}> {r} miles</label> '
        for r in radius_options)
    card = f"""<div class="card"><h2>Search {kind}s at my location</h2>
      <p class="muted">Uses your check-in location: {esc(checkin['airport_code'])} · {esc(checkin['city'])}. No other location is used.</p>
      <form method="get"><input type="hidden" name="at_checkin" value="1">{radios}
      <button type="submit">Search {kind}s at my location</button></form></div>"""
    return card, data, checkin

def external_results_html(kind, data, user):
    centre, results, error, radius, checkin = data
    if error and not results:
        return f'<div class="error">{esc(error)}</div>'
    if centre:
        lat, lon, display = centre
        header = f"<h2>{len(results)} {kind}s within {radius} mi of {esc(display)}</h2>"
    else:
        header = f"<h2>{len(results)} {kind}s within {radius} mi</h2>"
    if not results:
        return header + '<p class="muted">No places found in that radius.</p>'
    cards = ""
    for r in results:
        detail = ""
        if kind == "hotel":
            detail = f"<div>Brand: {esc(r['brand'] or r['name'])}</div>"
        else:
            detail = f"<div>Cuisine: {esc(r['cuisine'])}</div><div>Hours: {esc(r['hours'])}</div>"
        web = f'<div><a href="{esc(r["website"])}" target="_blank" rel="noopener">Website</a></div>' if r["website"] else ""
        add = (url_for("new_review") + "?" + urllib.parse.urlencode(
            {"kind": kind, "place": r["name"], "airport": checkin["airport_code"], "city": checkin["city"]}))
        checkin_btn = (f'<form method="post" action="{url_for("board")}" style="margin-top:8px">'
            f'<input type="hidden" name="action" value="checkin">'
            f'<input type="hidden" name="airport_code" value="{esc(checkin["airport_code"])}">'
            f'<input type="hidden" name="city" value="{esc(checkin["city"])}">'
            f'<input type="hidden" name="place_name" value="{esc(r["name"])}">'
            f'<input type="hidden" name="kind" value="{kind}">'
            f'<button type="submit" style="width:auto;padding:6px 12px">Check in</button></form>')
        cards += f"""<div class="card"><a href="{add}"><strong>{esc(r['name'])}</strong></a> <span class="muted">{r['distance']} mi</span>
          {detail}<div>Address: {esc(r['address'])}</div><div>Phone: {esc(r['phone'])}</div>{web}
          <p><a href="{add}">Add review</a></p>{checkin_btn}</div>"""
    return header + cards

@app.route("/hotels")
@login_required
def hotels():
    user = current_user()
    q = request.args.get("q", "")
    scope = request.args.get("scope", "in")
    card, data, _ = at_location_section("hotel", user, [2, 5, 10], 5)
    ext = external_results_html("hotel", data, user) if data else ""
    body = card + ext + saved_reviews_section("hotel", user, q, scope)
    return page("Hotels", user, body)

@app.route("/restaurants")
@login_required
def restaurants():
    user = current_user()
    q = request.args.get("q", "")
    scope = request.args.get("scope", "in")
    card, data, _ = at_location_section("restaurant", user, [5, 10, 20], 20)
    ext = external_results_html("restaurant", data, user) if data else ""
    body = card + ext + saved_reviews_section("restaurant", user, q, scope)
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
@app.route("/friends", methods=["GET", "POST"])
@login_required
def friends():
    user = current_user()
    db = get_db()
    flash = None
    error = None
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
        pilot_name = request.form.get("pilot_name", "").strip()
        if pilot_name:
            want = pilot_name.lower()
            named = [u for u in db.execute("SELECT * FROM users WHERE id != ?", (user["id"],)).fetchall()
                     if (u["name"] or "").strip().lower() == want]
            new = [u for u in named if u["id"] not in friend_ids]
            if len(new) == 1:
                fid = new[0]["id"]
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
                    flash = f"You are now friends with {esc(new[0]['name'])}."
                elif outgoing:
                    error = f"You already sent {esc(new[0]['name'])} a request."
                else:
                    db.execute("INSERT INTO friend_requests (from_user_id, to_user_id) VALUES (?,?)",
                               (user["id"], fid))
                    db.commit()
                    flash = f"Friend request sent to {esc(new[0]['name'])}. They need to approve it."
            elif len(new) > 1:
                detail = ", ".join(f"{esc(m['name'])} ({esc(m['airline'])} · {esc(m['base'])})" for m in new)
                error = f"More than one pilot is named '{esc(pilot_name)}': {detail}."
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
        items += (f"<li>{esc(fr['name'])} ({esc(fr['airline'])} · {esc(fr['base'])} · {esc(fr['aircraft'])}){nearby}"
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
                    f'<input name="pilot_name" placeholder="Jane Doe" autocomplete="off" required>'
                    f'<button type="submit">Add friend</button></form></div>')
    else:
        add_card = ('<div class="card"><h2>Add a friend</h2>'
                    '<p class="muted">No more pilots to add — everyone here is already your friend.</p></div>')
    req_items = ""
    for q in db.execute(
            "SELECT r.id, r.from_user_id, u.name, u.airline, u.base, u.aircraft FROM friend_requests r "
            "JOIN users u ON u.id = r.from_user_id WHERE r.to_user_id=?", (user["id"],)).fetchall():
        req_items += (f"<li>{esc(q['name'])} ({esc(q['airline'])} · {esc(q['base'])} · {esc(q['aircraft'])})"
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
    body = f"""<div class="card"><h2>Friends</h2>
      <p class="muted">Friends show as nearby only when their check-in matches yours.</p>
      <ul>{items or '<li class="muted">No friends yet.</li>'}</ul></div>
      {req_card}
      {add_card}"""
    return page("Friends", user, body, flash=flash, error=error)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
