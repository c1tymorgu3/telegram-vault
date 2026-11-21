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
database. A web panel reads it: search, per-sender profiles, charts, CSV export.
The account never sends anything.

**Digests.** Every two hours new messages are grouped per chat and extracted into
a JSON record: sectors in play, what people asked for, who was active. At midnight
those records become one daily brief. Every claim carries the message ids it came
from, and those ids are checked against the archive before saving, so a citation
the model invented shows up as a shortfall in the count.

**Actor dossiers.** Ask for a profile of a Telegram id and the model reads only
that account's messages: sectors it worked in and when, what it said about itself,
what it repeated from others. Nothing is generated until you press the button.

`LLM_PROVIDER` in `.env` switches between opencode, OpenRouter, OpenAI, DeepSeek,
Groq or any OpenAI-compatible endpoint.

---

## Screenshots

**Live archive**  
Every message, deleted ones included.

<img src="static/screenshot_log.png" alt="Live archive" width="100%">

**Digests**  
Two-hour segments, reduced to a daily brief.

<img src="static/screenshot_digest.png" alt="Digest" width="460">

**Actor dossiers**  
Sector history, self-disclosed facts and hearsay for any Telegram id.

<img src="static/screenshot_dossier.png" alt="Actor dossier" width="260">

**Dashboard**  
Velocity, active senders, deletion rate.

<img src="static/screenshot_dash.png" alt="Dashboard" width="100%">

---

## Installation & Setup

### Local install (Windows)

Use **Python 3.12**, not 3.13/3.14: the pinned stack (`pydantic-core`, `asyncpg`)
has no wheels there and the build fails without a C toolchain.

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

`.env` for a local run (SQLite, plain HTTP on loopback):

```env
DATABASE_URL=sqlite+aiosqlite:///./telegramlogs.db
DASHBOARD_PASSWORD=pick-something
SESSION_SECRET=paste-a-random-32-char-string
SESSION_HTTPS_ONLY=0
```

`SESSION_HTTPS_ONLY=0` matters on `http://127.0.0.1`: with the default `1` the
browser never sends the session cookie back and every page bounces to `/login`.

Then `start.bat`, or manually:

```bat
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
.venv\Scripts\python.exe bot.py
```

Chart.js is vendored in `static/vendor/`, so the panel needs no internet access.

### Server install

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`API_ID` and `API_HASH` come from [my.telegram.org](https://my.telegram.org).
Create the database once, then fill `.env`:

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

Run both processes under a supervisor (`systemd`, `tmux`, `screen`):

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
python3 bot.py
```

First login is a one-time interactive step (`login_once.py`); after that the
session file is reused and `ALLOW_NEW_LOGIN=0` keeps it that way.

---

## Ban safety

A listener is not banned for reading, it is banned for looking like a fleet. Every
rule below is enforced in `bot.py`:

- **One account, one `api_id`, one egress IP.** Accounts sharing both are clustered
  by Telegram; when one is flagged, the cluster goes with it. Set `ACCOUNT=alt1`
  with `API_ID_ALT1` / `API_HASH_ALT1`, one `SESSION_NAME` and one `PROXY` per account.
- **One process per session.** `AuthKeyDuplicatedError` means two processes touched
  the same `.session`. Telegram revokes the key instantly. `bot.py` takes a file lock
  (`*.lock`) and refuses to start a second copy.
- **No automatic login.** Repeated code requests are a number-level ban trigger.
  `ALLOW_NEW_LOGIN=0` by default; set it to `1` only for one deliberate first run.
- **Real client identity.** `DEVICE_MODEL` / `SYSTEM_VERSION` / `APP_VERSION` are
  filled in; the default `Telethon` banner is the easiest unofficial-client
  fingerprint.
- **No per-message API calls.** Entities come from the update cache. Startup reads
  at most 100 dialogs and `catch_up` is off, so a restart does not replay a backlog.
- **Slow polling.** Delivery runs on a 50 to 60 second randomised dialog poll,
  roughly 1,500 requests a day, with exponential backoff on `FloodWait`.
- **Warm the number up.** 7-10 days of normal use before attaching a userbot.

Read-only by design: `bot.py` never sends, joins, or invites. Keep it that way.

---

## Project Structure

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
├── middleware/         # custom middlewares (security headers)
├── templates/          # Jinja2 HTML views
└── static/             # assets (CSS, JS, logo, screenshots)
```

---

<div align="center">
  <br>
   <a href="https://github.com/c1tymorgu3">github.com/c1tymorgu3</a>
</div>
