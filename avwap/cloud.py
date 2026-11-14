"""Keep the scanner's data in GitHub so it can run on GitHub Actions and Streamlit Cloud.

Files are stored as assets on a GitHub release called "data" in your repository:

  avwap.sqlite.enc      full database (used by the GitHub Actions scan)
  dashboard.sqlite.enc  same without raw prices (smaller, used by the dashboard)
  user-state.json.enc   your positions, manual anchors and pins (written by the dashboard)

Everything is gzip-compressed and encrypted with APP_PASSWORD, so nothing is readable even
though the repository is public. Keep APP_PASSWORD the same everywhere (Actions, Streamlit,
your Mac's .env); changing it makes old files unreadable.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import requests

log = logging.getLogger(__name__)
TAG = "data"
DB_ASSET = "avwap.sqlite.enc"
DASH_ASSET = "dashboard.sqlite.enc"
STATE_ASSET = "user-state.json.enc"
USER_TABLES = {
    "manual_anchors": ["id", "symbol", "date", "label", "action", "created_at"],
    "pins": ["symbol", "anchor_date", "anchor_label"],
    "positions": ["id", "symbol", "entry_date", "entry_price", "qty", "trailing_anchor_date",
                  "notes", "closed", "last_hl_seen"],
}
MAGIC = b"AVW1"


# ------------------------------------------------------------------ config
def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    try:  # Streamlit secrets, when running on Streamlit Cloud
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def repo() -> str:
    r = _env("GITHUB_REPOSITORY") or _env("GITHUB_REPO")
    if not r:
        raise RuntimeError("Set GITHUB_REPO (e.g. ss-sumansaurav/Stock-Trading)")
    return r


def enabled() -> bool:
    return bool(_env("APP_PASSWORD") and (_env("GITHUB_REPOSITORY") or _env("GITHUB_REPO")))


def _headers(json_api=True):
    h = {"Accept": "application/vnd.github+json" if json_api else "application/octet-stream",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = _env("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# ------------------------------------------------------------------ crypto
def _fernet(salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    pw = _env("APP_PASSWORD")
    if not pw:
        raise RuntimeError("APP_PASSWORD is not set")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(pw.encode())))


def encrypt(data: bytes) -> bytes:
    salt = os.urandom(16)
    return MAGIC + salt + _fernet(salt).encrypt(gzip.compress(data, 6))


def decrypt(blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("Not an AVWAP data file")
    from cryptography.fernet import InvalidToken
    salt = blob[4:20]
    try:
        return gzip.decompress(_fernet(salt).decrypt(blob[20:]))
    except InvalidToken:
        raise RuntimeError("Could not decrypt the data: APP_PASSWORD differs from the one used to save it")


# ------------------------------------------------------------------ release assets
class GitHubError(RuntimeError):
    pass


def _check(r: requests.Response, what: str):
    """Raise a readable error with GitHub's own message and the usual fixes."""
    if r.ok:
        return
    try:
        msg = r.json().get("message", r.text[:300])
    except ValueError:
        msg = r.text[:300]
    hint = ""
    if r.status_code in (401, 403, 404):
        tok = _env("GITHUB_TOKEN") or ""
        hint = (
            "\n\nGitHub refused the token. Check, at github.com > Settings > Developer settings > "
            "Personal access tokens > Fine-grained tokens > your token:\n"
            f"  1. 'Repository access' is 'Only select repositories' with {repo()} selected "
            "(the default 'Public repositories' choice is read-only).\n"
            "  2. 'Repository permissions' > 'Contents' is 'Read and write'.\n"
            "  3. 'Resource owner' is your own account.\n"
            "  4. GITHUB_TOKEN in .env is that token's value (starts with github_pat_), "
            "and the token isn't expired."
        )
        if tok and not tok.startswith(("github_pat_", "ghp_", "ghs_")):
            hint += f"\n  The token in use starts with '{tok[:4]}…', which doesn't look like a GitHub token."
        if os.environ.get("GITHUB_TOKEN") and not tok.startswith("github_pat_"):
            hint += "\n  Note: a GITHUB_TOKEN set in your shell overrides the one in .env."
    raise GitHubError(f"GitHub said {r.status_code} while {what}: {msg}{hint}")


def _release(create: bool = False) -> dict | None:
    r = requests.get(f"https://api.github.com/repos/{repo()}/releases/tags/{TAG}", headers=_headers(), timeout=30)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404 and create:
        r = requests.post(f"https://api.github.com/repos/{repo()}/releases", headers=_headers(), timeout=30,
                          json={"tag_name": TAG, "name": "Scanner data (encrypted)", "prerelease": True,
                                "body": "Encrypted data store for the AVWAP scanner. Updated automatically."})
        _check(r, "creating the 'data' release")
        return r.json()
    if r.status_code == 404:
        return None
    _check(r, "reading the 'data' release")


def download(name: str) -> bytes | None:
    rel = _release()
    if not rel:
        return None
    for a in rel.get("assets", []):
        if a["name"] == name:
            r = requests.get(a["url"], headers=_headers(json_api=False), timeout=300)
            _check(r, f"downloading {name}")
            return r.content
    return None


def upload(name: str, data: bytes):
    rel = _release(create=True)
    for a in rel.get("assets", []):
        if a["name"] == name:
            _check(requests.delete(a["url"], headers=_headers(), timeout=30), f"replacing {name}")
    url = f"https://uploads.github.com/repos/{repo()}/releases/{rel['id']}/assets?name={name}"
    h = _headers()
    h["Content-Type"] = "application/octet-stream"
    r = requests.post(url, headers=h, data=data, timeout=600)
    _check(r, f"uploading {name}")
    log.info("Uploaded %s (%.1f MB)", name, len(data) / 1e6)


# ------------------------------------------------------------------ user state
def export_user_state(conn: sqlite3.Connection) -> dict:
    out = {}
    for t, cols in USER_TABLES.items():
        cur = conn.execute(f"SELECT {', '.join(cols)} FROM {t}")
        out[t] = [dict(zip(cols, row)) for row in cur.fetchall()]
    return out


def import_user_state(conn: sqlite3.Connection, state: dict):
    for t, cols in USER_TABLES.items():
        if t not in state:
            continue
        conn.execute(f"DELETE FROM {t}")
        conn.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [[r.get(c) for c in cols] for r in state[t]])
    conn.commit()


def push_user_state(conn: sqlite3.Connection):
    upload(STATE_ASSET, encrypt(json.dumps(export_user_state(conn)).encode()))


def pull_user_state(conn: sqlite3.Connection) -> bool:
    blob = download(STATE_ASSET)
    if blob is None:
        return False
    import_user_state(conn, json.loads(decrypt(blob)))
    return True


# ------------------------------------------------------------------ whole database
def pull_db(db_path: str, asset: str = DB_ASSET) -> bool:
    """Download + decrypt the database to db_path, then apply the latest user state."""
    blob = download(asset)
    if blob is None:
        log.warning("No %s in the 'data' release yet", asset)
        return False
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path + ".download"
    Path(tmp).write_bytes(decrypt(blob))
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)
    shutil.move(tmp, db_path)
    conn = sqlite3.connect(db_path)
    try:
        from .db import SCHEMA
        conn.executescript(SCHEMA)  # make sure newer tables/columns exist
        pull_user_state(conn)
    finally:
        conn.close()
    return True


def push_db(db_path: str, include_user_state: bool = True):
    """Upload the full database, a slim dashboard copy, and (optionally) the user state."""
    with tempfile.TemporaryDirectory() as td:
        full = os.path.join(td, "full.sqlite")
        src = sqlite3.connect(db_path)
        try:
            src.execute("PRAGMA wal_checkpoint(FULL)")
            src.execute(f"VACUUM INTO '{full}'")
            if include_user_state:
                state = export_user_state(src)
        finally:
            src.close()
        upload(DB_ASSET, encrypt(Path(full).read_bytes()))
        slim = os.path.join(td, "slim.sqlite")
        c = sqlite3.connect(full)
        try:
            c.execute("DELETE FROM prices_raw")
            c.commit()
            c.execute(f"VACUUM INTO '{slim}'")
        finally:
            c.close()
        upload(DASH_ASSET, encrypt(Path(slim).read_bytes()))
    if include_user_state:
        upload(STATE_ASSET, encrypt(json.dumps(state).encode()))
