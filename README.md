<div align="center">
  <img src="static/logo-mark.png" alt="Telegram Vault" width="360">

  # Telegram Vault

  **LLM-assisted Telegram OSINT tool · by c1tymorgu3**

  ![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python)
  ![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=flat-square&logo=fastapi)
  ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Async-336791?style=flat-square&logo=postgresql)
  ![Telethon](https://img.shields.io/badge/Telethon-1.36.0-2CA5E0?style=flat-square&logo=telegram)
  ![Status](https://img.shields.io/badge/Status-Active-success?style=flat-square)

  *Reads every message in your chats, keeps it in a searchable archive, and turns it into briefs where every claim carries its message id.*
</div>

---

## What it does

A userbot sits on one Telegram account and writes every message it sees into a
database. A web panel reads that database: search, per-sender profiles, charts,
CSV export. The account never sends anything.

Every two hours the new messages are grouped per chat and pulled into a JSON
record: sectors in play, what people asked for, who was active. At midnight those
records become one daily brief. Each claim carries the message ids it came from,
and those ids are checked against the archive before the brief is saved. If the
model invents a citation, the count on the card shows the shortfall.

Ask for a profile of a Telegram id and the model reads that account's own
messages: sectors it worked in and when, what it said about itself, what it only
repeated from others. Nothing is generated until you press the button, and the
result is cached for a day.

`LLM_PROVIDER` in `.env` picks between opencode, OpenRouter, OpenAI, DeepSeek,
Groq or any OpenAI-compatible endpoint.

---

## Screenshots

Every message, deleted ones included.

<img src="static/screenshot_log.png" alt="Live archive" width="100%">

Two-hour segments, reduced to a daily brief.

<img src="static/screenshot_digest.png" alt="Digest" width="460">

Sector history, self-disclosed facts and hearsay for any Telegram id.

<img src="static/screenshot_dossier.png" alt="Actor dossier" width="260">

Velocity, active senders, deletion rate.

<img src="static/screenshot_dash.png" alt="Dashboard" width="100%">

---

## Installation

Use Python 3.12, not 3.13 or 3.14. The pinned stack (`pydantic-core`, `asyncpg`)
has no wheels there and the build fails without a C toolchain.

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

For a local run, point the database at SQLite and turn off HTTPS-only cookies:

```env
DATABASE_URL=sqlite+aiosqlite:///./telegramlogs.db
DASHBOARD_PASSWORD=pick-something
SESSION_SECRET=paste-a-random-32-char-string
SESSION_HTTPS_ONLY=0
```

That last line matters on `http://127.0.0.1`. With the default `1` the browser
never sends the session cookie back, and every page bounces to `/login`.

Then `start.bat`, or both processes by hand:

```bat
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
.venv\Scripts\python.exe bot.py
```

On a server the steps are the same with `python3 -m venv venv`. `API_ID` and
`API_HASH` come from [my.telegram.org](https://my.telegram.org). Create the
database once, then fill `.env`:

```sql
CREATE DATABASE vault;
```

```env
API_ID=1234567
API_HASH=your_api_hash
DATABASE_URL=postgresql+asyncpg://vault:pass@localhost/vault
DASHBOARD_PASSWORD=pick-something
SESSION_SECRET=random-32-char-string
```

Run both processes under a supervisor (`systemd`, `tmux`, `screen`). The first
login is a one-time interactive step through `login_once.py`; after that the
session file is reused and `ALLOW_NEW_LOGIN=0` keeps it that way.

Chart.js is vendored in `static/vendor/`, so the panel needs no internet access.

---

## Ban safety

A listener is not banned for reading. It is banned for looking like a fleet, and
every rule below is enforced in `bot.py`.

One account, one `api_id`, one egress IP. Telegram clusters accounts that share
an `api_id` and an IP address, so when one gets flagged the rest go with it. Use
`ACCOUNT=alt1` with `API_ID_ALT1` / `API_HASH_ALT1`, one `SESSION_NAME` and one
`PROXY` per account.

One process per session. `AuthKeyDuplicatedError` means two processes touched the
same `.session`, and Telegram revokes the key on the spot. `bot.py` takes a file
lock and refuses to start a second copy.

No automatic login. Repeated code requests sit on the list of number-level ban
triggers. `ALLOW_NEW_LOGIN=0` is the default; set it to `1` only for one
deliberate first run.

Real client identity. `DEVICE_MODEL`, `SYSTEM_VERSION` and `APP_VERSION` are
filled in; the stock `Telethon` banner is an easy fingerprint.

No per-message API calls. Entities come from the update cache, startup reads at
most 100 dialogs, and `catch_up` is off, so a restart does not replay a backlog.

Slow polling. Delivery runs on a 50 to 60 second randomised dialog poll, about
1,500 requests a day, with exponential backoff on `FloodWait`.

Let a cold number live normally for a week or two before attaching a userbot.

`bot.py` never sends, joins or invites. Read-only is the whole point.

---

## Project structure

```text
├── app.py              # FastAPI app and routers
├── bot.py              # Telethon userbot listener
├── digest.py           # segment extraction and daily reduce
├── profiles.py         # actor dossiers
├── llm.py              # provider switch, JSON calls, retries
├── daily_digest.py     # cron entry point, 00:00 TRT
├── config.py           # environment variables loader
├── database.py         # SQLAlchemy ORM models
├── start.bat           # multi-process launcher
├── routes/             # auth, messages, dashboard, websocket, digest, profiles
├── middleware/         # security headers
├── templates/          # Jinja2 HTML views
└── static/             # assets (CSS, JS, logo, screenshots)
```

---

<div align="center">
  <br>
   <a href="https://github.com/c1tymorgu3">github.com/c1tymorgu3</a>
</div>
