# 135/121 Pilots — deployable version

A web app for pilots, open to all pilots. Any pilot can register, save hotel
and restaurant reviews, check in to an area, and see friends checked in to
the same area. Reviews are visible to all pilots.

## Location model — check-in only

Location is **only** the airport code and city a pilot types when checking
in on the Board. This app has no device-location feature, no browser
location prompt, and no map-service links. The "Search hotels at my
location" and "Search restaurants at my location" buttons search around
that typed check-in place only.

Hotel and restaurant listings for those buttons come from OpenStreetMap
(Nominatim for resolving the check-in place, Overpass for nearby places).
Pilot reviews themselves are stored in this app's own database.

## Features

- Register / log in: name, airline, base, aircraft, email + password
  (hashed with Werkzeug; session cookies). Any pilot can register.
- Board: current check-in (airport code + city, with dates), check out,
  and friends checked in to the same area.
- Hotels: saved pilot reviews with star ratings and pilot tags
  (quiet rooms, blackout curtains, crew shuttle, open late, and more),
  search saved reviews by place name, airport code, city, notes, and tags
  (every word must match somewhere), default "In this area" scope with an
  "All areas" toggle, and a "Found in another area — see all areas" link
  when "In this area" is empty but "All areas" has a match.
  Plus "Search hotels at my location": 2 / 5 / 10-mile choices, real
  OpenStreetMap hotel results on the page sorted by distance, with brand,
  address, phone, and website where listed, plus an Add review shortcut
  pre-filled with the hotel and check-in area.
  With no active check-in, it prompts you to check in on the Board first.
- Restaurants: same saved-review search, plus "Search restaurants at my
  location": 5 / 10 / 20-mile choices (20 is the limit and the default),
  real OpenStreetMap restaurant results on the page sorted by distance,
  with cuisine, address, phone, hours, and website where listed, plus an
  Add review shortcut. With no active check-in, it prompts you to check
  in first.
- Friends: pilot friends list. Friends show as nearby only when their
  check-in matches yours.
- Mobile-responsive: works on a phone and on desktop.
- Starts with an empty database: no seed users, reviews, friends, or
  check-ins are included.

## Run locally (exact steps)

```bash
cd 135-121-pilots-deployable
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit SECRET_KEY in .env to a long random string
.venv/bin/python app.py
```

Then open http://127.0.0.1:5000/ — register a pilot account and use it.
The database file `pilots.db` is created next to `app.py` on first run
and starts empty. Delete `pilots.db` (while the server is stopped) to
reset to an empty database.

Or with Docker:

```bash
cd 135-121-pilots-deployable
docker build -t pilots-135-121 .
docker run --rm -p 5000:5000 -e SECRET_KEY='a-long-random-string' pilots-135-121
```

Then open http://127.0.0.1:5000/

## Deploy

This code is **not** a live public link by itself. Deploy it with one of
the options below, then share the URL that service gives you.

### Option A: Render — one-click Blueprint (recommended)

`render.yaml` in this folder defines the full production setup: the web
service (Docker, served by gunicorn) plus a managed Postgres database.

1. Put this folder in a GitHub repository (include `render.yaml`).
2. In Render: **New → Blueprint**, select the repository, and apply.
   Render provisions the web service and the Postgres database, wires
   `DATABASE_URL` automatically, and generates `SECRET_KEY`.
3. Deploy. Render will give you a `https://…onrender.com` URL — that is
   the link to send to other pilots.

Pilot accounts, reviews, friends, and check-ins live in the managed
Postgres database, so they survive restarts and redeploys. The database
tables are created automatically on first boot.

### Option B: Railway

1. Put this folder in a GitHub repository.
2. In Railway, create a project from that repository.
   - Railway will detect the Dockerfile and build it, or you can set
     the start command to `python app.py`.
3. Set variables in Railway:
   - `SECRET_KEY` = a long random string.
   - `PORT` is set automatically by Railway; the app reads it.
4. Deploy and use the public domain Railway assigns (under Settings →
   Networking → Generate Domain) as the link to send to other pilots.
5. Database: by default the app uses SQLite, which resets on redeploy
   unless you attach a Railway volume and set `DATABASE_PATH` to a path
   on that volume (for example `/data/pilots.db`). Better: add a
   Postgres database to the Railway project and set `DATABASE_URL` to
   its connection string — the app will use Postgres automatically.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes, in production | Signs session cookies. Use a long random string. The built-in default is for local development only. |
| `DATABASE_URL` | No | Postgres connection string. When set, the app uses Postgres; when unset, it uses SQLite at `DATABASE_PATH`. Set automatically by the Render Blueprint (`render.yaml`). |
| `DATABASE_PATH` | No | Path to the SQLite file (only used when `DATABASE_URL` is unset). Defaults to `pilots.db` next to `app.py`. |
| `PORT` | No (hosting sets it) | Port to listen on. Defaults to 5000. |
| `SMTP_HOST` | No | SMTP server for friend-request email notifications (e.g. `smtp.gmail.com`). When unset, no emails are sent. |
| `SMTP_PORT` | No | SMTP port. Defaults to 587. |
| `SMTP_USERNAME` | No | SMTP login username. |
| `SMTP_PASSWORD` | No | SMTP login password (use an app-specific password for Gmail/Yahoo). |
| `SMTP_FROM` | No | From address for notification emails. Defaults to `SMTP_USERNAME`. |

When someone sends a friend request, the recipient gets an email with a link to approve or decline it. Set the `SMTP_*` variables in the Render dashboard (Service → Environment) for the live site.

## Known limitations

- OpenStreetMap's public Overpass mirrors can be slow or return
  capacity errors at busy times. The app races three mirrors in
  parallel and, if they are all busy, falls back to a place-type
  search filtered by distance; if that also fails, it shows a clear
  "Listing service is busy — try again" state instead of failing
  silently. A search can therefore take a while or need a retry.
- Database: without `DATABASE_URL` the app uses SQLite, a single file
  database — fine for local development and small tryouts, but the file
  is ephemeral on most hosting (it resets on redeploy/restart). For
  production, deploy with the Render Blueprint (`render.yaml`), which
  provisions managed Postgres and sets `DATABASE_URL` automatically —
  data then survives restarts and redeploys.
- There is no password-reset email flow and no moderation tooling in
  this version. Anyone who can register can see all reviews, by design
  ("open to all pilots").
- This repository is code to deploy. It is not itself a live hosted
  service for other pilots until you deploy it with Option A or B above.
