"""One-shot, deliberate login for Telegram Vault.

Two phases instead of an interactive prompt, so a single code request is made
once and never retried in a loop (a code-request loop is what gets numbers
limited):

    python login_once.py request          # sends one login code, saves the hash
    python login_once.py verify 12345     # signs in; prints NEED_2FA if a cloud
                                          # password is set
    python login_once.py 2fa mypassword   # only when verify said NEED_2FA
    python login_once.py status           # who is authorized in the session

The session file is written by Telethon as soon as sign_in succeeds.
"""

import asyncio
import json
import os
import sys
import time

from telethon import TelegramClient, errors

import config

STATE_FILE = f"{config.SESSION_NAME}.login.json"


def build_client():
    return TelegramClient(
        config.SESSION_NAME,
        config.API_ID,
        config.API_HASH,
        device_model=config.DEVICE_MODEL,
        system_version=config.SYSTEM_VERSION,
        app_version=config.APP_VERSION,
        lang_code=config.LANG_CODE,
        system_lang_code=config.LANG_PACK,
        catch_up=False,
        sequential_updates=True,
    )


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if not config.API_ID or not config.API_HASH:
        print("API_ID / API_HASH missing in .env")
        return 2

    client = build_client()
    await client.connect()
    try:
        if cmd == "request":
            if not config.PHONE_NUMBER:
                print("PHONE_NUMBER missing in .env")
                return 2
            if await client.is_user_authorized():
                print("ALREADY_AUTHORIZED")
                return 0
            sent = await client.send_code_request(config.PHONE_NUMBER)
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(
                    {"phone": config.PHONE_NUMBER,
                     "phone_code_hash": sent.phone_code_hash},
                    fh,
                )
            print("CODE_SENT", type(sent.type).__name__)
        elif cmd == "verify":
            with open(STATE_FILE, encoding="utf-8") as fh:
                state = json.load(fh)
            try:
                await client.sign_in(
                    phone=state["phone"],
                    code=sys.argv[2],
                    phone_code_hash=state["phone_code_hash"],
                )
                print("LOGIN_OK")
            except errors.SessionPasswordNeededError:
                print("NEED_2FA")
        elif cmd == "2fa":
            await client.sign_in(password=sys.argv[2])
            print("LOGIN_OK")
        elif cmd == "file":
            # The code never travels through a chat: it is typed into a local
            # file. Telegram invalidates a login code it sees shared in a
            # message ("bu kod daha önce paylaşıldı").
            if await client.is_user_authorized():
                print("ALREADY_AUTHORIZED")
                return 0
            path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
                os.path.expanduser("~"), "Desktop", "code.txt")
            sent = await client.send_code_request(config.PHONE_NUMBER)
            print("CODE_SENT", type(sent.type).__name__, flush=True)
            print("WRITE_CODE_INTO", path, flush=True)
            digits = ""
            deadline = time.time() + 280
            while time.time() < deadline:
                if os.path.exists(path):
                    raw = open(path, encoding="utf-8", errors="ignore").read()
                    digits = "".join(ch for ch in raw if ch.isdigit())
                    if len(digits) >= 5:
                        break
                time.sleep(2)
            if len(digits) < 5:
                print("NO_CODE_IN_FILE")
                return 3
            print("READ_CODE_LEN", len(digits), flush=True)
            try:
                await client.sign_in(phone=config.PHONE_NUMBER, code=digits,
                                     phone_code_hash=sent.phone_code_hash)
                print("LOGIN_OK")
            except errors.SessionPasswordNeededError:
                print("NEED_2FA")
        elif cmd == "status":
            if await client.is_user_authorized():
                me = await client.get_me()
                print("AUTHORIZED", me.id, me.first_name, me.username)
            else:
                print("NOT_AUTHORIZED")
        else:
            print(__doc__)
            return 1
    except errors.PhoneCodeInvalidError:
        print("CODE_INVALID")
        return 3
    except errors.PhoneCodeExpiredError:
        print("CODE_EXPIRED")
        return 3
    except errors.FloodWaitError as exc:
        print("FLOOD_WAIT", exc.seconds)
        return 4
    except errors.AuthKeyDuplicatedError:
        print("AUTH_KEY_DUPLICATED — another process is using this session file")
        return 5
    finally:
        await client.disconnect()
        if os.path.exists(STATE_FILE) and cmd in ("verify", "2fa"):
            try:
                if await_authorized_marker():
                    os.remove(STATE_FILE)
            except OSError:
                pass
    return 0


def await_authorized_marker():
    """The session file physically exists and has a non-empty auth key."""
    path = f"{config.SESSION_NAME}.session"
    return os.path.exists(path) and os.path.getsize(path) > 1024


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
