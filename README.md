# WildTrack — Endangered Species Tracker

WildTrack is a responsive Flask conservation workspace, upgraded with learning, community, moderation, and member tools. Species records continue to come from your existing Google Sheet. Accounts and private member activity are stored locally in SQLite—not in a public spreadsheet.

## Start here — your existing Mac project

In VS Code, choose **File → Open Folder** and select `Downloads/endangered_species_tracker_app`. Open **Terminal → New Terminal**, then run:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser. Stop an older running copy with **Ctrl+C**, then start it again after an upgrade.

**Do not double-click `templates/index.html` or use VS Code Live Server.** These are Flask/Jinja templates. Opening them directly displays template code instead of the working website. If port 5000 is busy, run `python -m flask --app app run --port 5001` and visit port 5001.

## The 12 additions

1. **Learn & Protect** (`/learn`): why species matter, threats, IUCN categories, Nepal conservation, six practical actions, saved personal pledges, and official sources.
2. **Compare** (`/compare`): compare two or three species side by side, using current directory records.
3. **Quiz & certificates** (`/quiz`): server-scored bilingual questions, saved attempts, answer explanations, and printable learning certificates for scores of 80% or higher. Choose **Save as PDF** in the print dialog. Certificates recognize participation in this project, not professional accreditation.
4. **Conservation news** (`/news`): live Mongabay RSS headlines with dates, source links, topic filters, a 15-minute cache, and an honest unavailable/last-loaded notice when offline. Filters cover the currently loaded feed; some topics may have no matching articles.
5. **Photo observations** (`/observations/new`, `/community`): private field journal or opt-in public submission; categories, photos, approximate map points, and administrator review. Community reports are not scientific verification of species identity.
6. **Achievements** (`/achievements`): six progress-based badges and points calculated from watchlists, approved observations, quiz scores, and saved conservation commitments.
7. **Alerts** (`/alerts`): watched-species changes, reviewed community observations, and reminders for followed events within seven days. Includes preferences, unread counts, and mark-all-read. These are **in-app alerts, not email or push messages**.
8. **Administrator workspace** (`/admin`): review observations, enable/disable non-admin accounts, add local species records or amendments, restore Sheet values, export observation CSV, publish events, and refresh source data.
9. **Analytics** (`/analytics`): threats, regions, population trends, classes, habitats, Nepal scope, and recorded status changes. Counts reflect this dataset, not all wildlife worldwide. Threat groups use keywords. History starts when this version first sees a successful Sheet read; it does not invent past population counts or habitat-loss measurements.
10. **Organizations** (`/organizations`): searchable Nepal-focused directory linking to WWF Nepal, NTNC, Red Panda Network, and Bird Conservation Nepal. Availability of volunteering/programs must be confirmed with each organization.
11. **Species of the week** (homepage): automatically rotating feature with an image when available, facts, threats, actions, a profile link, watchlist control, and a mini quiz.
12. **Language & accessibility**: English/Nepali menus, learning, quizzes and core labels; keyboard focus, skip navigation, large text, high contrast, dark/light themes, responsive layouts, accessible chart labels, and text alternatives to maps. Scientific Sheet descriptions and external headlines remain in their source language. Some administrative/system messages remain English.

## Enable your administrator account

First register a normal account in the app. In the activated VS Code terminal, replace `YOUR_USERNAME` with that account's username:

```bash
python -m flask --app app promote-admin YOUR_USERNAME
```

Reload the website. **Admin** will appear in the tools navigation. No default administrator/password is created, and ordinary members cannot grant themselves administrator access.

The public Google Sheet connection is read-only. Change the shared source data directly in Google Sheets. Admin species changes are clearly labelled **project amendments**, stored in local SQLite, and can be reset; they never silently overwrite your workbook.

## Photo privacy and moderation

- Entries are private by default. Check **Submit for community review** to make an entry eligible for the community page.
- New public submissions remain pending until an administrator approves them. Old journal entries remain private after migration.
- Only JPEG, PNG and WebP images are accepted: up to 5 MB and 20 megapixels. Images are resized and re-encoded without EXIF/GPS metadata. Private/pending photos require owner or admin access.
- Coordinates are optional and rounded to whole-degree grid centers **before saving**. Sensitive locations are hidden publicly; critically endangered species always have hidden locations. Map points are approximate, not precise sighting records.
- Reviewers must check photos and notes for identifiable nests/dens or sensitive locations before approval. Metadata removal does not hide landmarks or location details typed into notes.
- Members export only their own journal; administrators can export all reports. CSV cells are protected against spreadsheet formula injection.

## Alerts and source updates

The app checks Google Sheets on page requests after its two-minute cache expires. Only successful source reads create change history and watcher alerts; offline sample data never generates false change alerts. The first source read establishes a baseline. Event reminders are generated when a member opens Alerts. There is no background email service or scheduled job installed.

An administrator can use **Refresh data**, or the project owner can run:

```bash
python -m flask --app app sync-species
```

## Preserve your data

Back up `data/wildtrack.db` and `instance/` together. The database contains accounts and activity; `instance/uploads/` contains cleaned photos and `instance/session-secret` keeps local sessions stable between restarts. Do not replace these with a test database or commit them to Git. New database tables/columns are added automatically while preserving existing accounts, watchlists and observations.

For public deployment, use HTTPS, a production WSGI server, a strong environment secret, rate limiting, backups, and a reviewed email/password-recovery setup. The local development server is not a production host. Python 3.11+ is recommended for a fresh setup; this upgrade is also tested with the existing Python 3.9 environment.

### Vercel deployment

WildTrack detects Vercel automatically and puts its writable runtime files in `/tmp/wildtrack`, because deployed application files are read-only. In **Vercel → Project → Settings → Environment Variables**, add a long random `FLASK_SECRET_KEY` for Production, Preview, and Development, then redeploy. The Google Sheet remains the live species source without any additional setting.

Do not add empty `GOOGLE_SHEET_*` variables in Vercel. Blank values are treated as unset and use this repository's supplied Sheet ID, `Species` tab, `A4:W` range, and public-read mode.

Vercel's `/tmp` storage is temporary and is not shared by every serverless instance. The public species directory, learning pages, comparison, quiz content, news, organizations, analytics, and Google Sheets integration work normally, but SQLite-backed accounts, watchlists, observations, admin changes, and uploaded photos can reset after a cold start. Use a hosted database and object storage before treating those member features as permanent production data. Local VS Code runs continue using `data/wildtrack.db` and `instance/uploads` as before.

## Run tests

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked external calls. They cover authentication, source parsing, permissions, quiz/certificate ownership, moderation, photo privacy, sensitive locations, alerts, local amendments, exports, news failures, and migration preservation.

## Features

- Search by common name, scientific name, habitat, country, or region
- Filter by IUCN status, animal class, population trend, and Nepal relevance
- Interactive conservation chart and Leaflet world map
- Detailed species profiles with habitat, threats, actions, sources, and imagery
- Account registration and login with hashed passwords
- CSRF-protected forms and safe post-login redirects
- Personal species watchlists
- Private member workspace and field journal
- Responsible observation reports with date and general location
- Light and dark themes, responsive navigation, and mobile layouts
- Live public Google Sheets data source with automatic local CSV fallback
- JSON endpoints at `/api/species` and `/api/species/<species_id>`

## Run in VS Code

Open this folder in VS Code, then open the integrated terminal.

```bash
python -m venv .venv
```

Activate the environment.

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
source .venv/bin/activate
```

Install dependencies and run the app:

```bash
pip install -r requirements.txt
python app.py
```

Open the address shown in the terminal, normally [http://127.0.0.1:5000](http://127.0.0.1:5000).

The app creates `data/wildtrack.db` and `instance/` automatically the first time it runs. Keep both when moving your project.

## Optional environment settings

Copy `.env.example` values into your shell or environment manager. For any shared or deployed environment, set a strong `FLASK_SECRET_KEY`.

macOS or Linux example:

```bash
export FLASK_SECRET_KEY="replace-with-a-long-random-secret"
export GOOGLE_SHEET_ID="your-sheet-id"
export GOOGLE_SHEET_NAME="Species"
export GOOGLE_SERVICE_ACCOUNT_FILE="service_account.json"
```

The project is already connected to the supplied public workbook and its `Species` tab. New or edited sheet rows appear after the two-minute cache expires on the next data request, or after an administrator refreshes data. If Google Sheets is unavailable, WildTrack labels and uses `data/species_sample.csv` as an offline sample so the site remains usable.

## Connected Google Sheet

The default database is:

`https://docs.google.com/spreadsheets/d/1GimbfdLW2aQtIhUWw1lXM4JJ4ZBALlzQldT-o6FhFzE/edit`

The application reads `A4:W` from the `Species` tab through Google Sheets’ public CSV endpoint. Keep row 4 as the column-header row and retain the existing column names.

To switch to a private workbook later, set `GOOGLE_SHEET_PUBLIC=false`, provide a service-account JSON file, share the workbook with its `client_email`, and update `GOOGLE_SHEET_ID`.

Never commit `service_account.json`, `.env`, or `data/wildtrack.db`.

## Data note

Map coordinates are representative visualization points, not precise occurrence records. Conservation assessments change, so each species profile links to IUCN for current verification. Users should never place exact nest, den, or sensitive-species coordinates in field observations.
