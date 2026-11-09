#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent
PAGE = "Schneider_MP-C_Controller_Selector_macOS.html"
MAX_REQUEST_SIZE = 10 * 1024 * 1024
CODE_ALPHABET = "ABCDEFGHIJKLMNPQRSTUVWXYZ0123456789"
FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = 60
CREATE_LIMIT = 10
CREATE_WINDOW = 3600
attempts_lock = threading.Lock()
failed_attempts = defaultdict(deque)
workspace_creations = defaultdict(deque)


def connect_database(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workspaces ("
        "code_hash TEXT PRIMARY KEY NOT NULL, created_at TEXT NOT NULL, "
        "revision INTEGER NOT NULL DEFAULT 0)"
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(workspaces)")
    }
    if "revision" not in columns:
        connection.execute(
            "ALTER TABLE workspaces ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workspace_selections ("
        "code_hash TEXT NOT NULL REFERENCES workspaces(code_hash) ON DELETE CASCADE, "
        "id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY (code_hash, id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS migration_state (name TEXT PRIMARY KEY NOT NULL)"
    )
    return connection


def hash_code(code):
    canonical = unicodedata.normalize("NFC", code).upper()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid_workspace_code(code):
    if not isinstance(code, str):
        return False
    normalized = unicodedata.normalize("NFC", code)
    return len(normalized) == 8 and all(
        character.isprintable() and not character.isspace()
        for character in normalized
    )


class SelectorHandler(BaseHTTPRequestHandler):
    database_path = None
    allowed_origins = set()

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8")

    def client_key(self):
        return self.client_address[0]

    def is_rate_limited(self, buckets, window, limit):
        now = time.monotonic()
        key = self.client_key()
        with attempts_lock:
            timestamps = buckets[key]
            while timestamps and now - timestamps[0] >= window:
                timestamps.popleft()
            return len(timestamps) >= limit

    def record_attempt(self, buckets):
        with attempts_lock:
            buckets[self.client_key()].append(time.monotonic())

    def do_OPTIONS(self):
        if self.path not in ("/api/selections", "/api/workspaces"):
            self.send_error(404, "Not found.")
            return
        origin = self.headers.get("Origin")
        if origin not in self.allowed_origins:
            self.send_json(403, {"error": "This website is not allowed to use the workspace API."})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Session-Code, X-Workspace-Revision",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def authorized_workspace(self):
        try:
            code = unquote(
                self.headers.get("X-Session-Code", ""),
                encoding="utf-8",
                errors="strict",
            )
        except UnicodeDecodeError:
            self.record_attempt(failed_attempts)
            return None
        if not valid_workspace_code(code):
            self.record_attempt(failed_attempts)
            return None
        code_hash = hash_code(code)
        try:
            with closing(connect_database(self.database_path)) as connection:
                row = connection.execute(
                    "SELECT 1 FROM workspaces WHERE code_hash = ?", (code_hash,)
                ).fetchone()
        except sqlite3.Error as error:
            self.log_error("Could not check the workspace code: %s", error)
            self.send_json(500, {"error": "Could not access the workspace database."})
            return False
        if row is None:
            self.record_attempt(failed_attempts)
            return None
        return code_hash

    def do_GET(self):
        if self.path == "/api/selections":
            if self.is_rate_limited(failed_attempts, ATTEMPT_WINDOW, FAILED_ATTEMPTS):
                self.send_json(429, {"error": "Too many invalid workspace codes. Try again shortly."})
                return
            code_hash = self.authorized_workspace()
            if code_hash is False:
                return
            if code_hash is None:
                self.send_json(404, {"error": "Workspace code not found."})
                return
            try:
                with closing(connect_database(self.database_path)) as connection:
                    workspace = connection.execute(
                        "SELECT revision FROM workspaces WHERE code_hash = ?",
                        (code_hash,),
                    ).fetchone()
                    records = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT record_json FROM workspace_selections "
                            "WHERE code_hash = ? ORDER BY rowid",
                            (code_hash,),
                        )
                    ]
            except (sqlite3.Error, json.JSONDecodeError) as error:
                self.log_error("Could not read the selections database: %s", error)
                self.send_json(500, {"error": "Could not read the selections database."})
                return
            self.send_json(200, {"records": records, "revision": workspace[0]})
            return
        if self.path in ("/", f"/{PAGE}"):
            try:
                body = (ROOT / PAGE).read_bytes()
            except OSError as error:
                self.log_error("Could not read the selector page: %s", error)
                self.send_error(500, "Could not read the selector page.")
                return
            self.send_body(200, body, "text/html; charset=utf-8")
            return
        self.send_error(404, "Not found.")

    def do_POST(self):
        if self.path != "/api/workspaces":
            self.send_error(404, "Not found.")
            return
        if self.is_rate_limited(workspace_creations, CREATE_WINDOW, CREATE_LIMIT):
            self.send_json(429, {"error": "Too many workspaces created from this address. Try again later."})
            return
        self.record_attempt(workspace_creations)
        custom_code = None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "A valid Content-Length header is required.")
            return
        if content_length < 0 or content_length > 1024:
            self.send_error(413, "The workspace request is too large.")
            return
        if content_length:
            try:
                body = json.loads(self.rfile.read(content_length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "The request body must be valid JSON.")
                return
            custom_code = body.get("code") if isinstance(body, dict) else None
            if not valid_workspace_code(custom_code):
                self.send_json(
                    400,
                    {"error": "Custom workspace codes must contain exactly 8 printable Unicode characters without spaces."},
                )
                return
            custom_code = unicodedata.normalize("NFC", custom_code)
        while True:
            code = custom_code or "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            code_hash = hash_code(code)
            connection = connect_database(self.database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO workspaces (code_hash, created_at) VALUES (?, ?)",
                    (code_hash, datetime.now(timezone.utc).isoformat()),
                )
                migrated = connection.execute(
                    "SELECT 1 FROM migration_state WHERE name = 'legacy_selections'"
                ).fetchone()
                if not migrated:
                    has_legacy_table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'selections'"
                    ).fetchone()
                    if has_legacy_table:
                        connection.execute(
                            "INSERT INTO workspace_selections (code_hash, id, record_json) "
                            "SELECT ?, id, record_json FROM selections",
                            (code_hash,),
                        )
                    connection.execute(
                        "INSERT INTO migration_state (name) VALUES ('legacy_selections')"
                    )
                connection.commit()
                self.send_json(201, {"code": code})
                return
            except sqlite3.IntegrityError:
                connection.rollback()
                if custom_code:
                    self.send_json(409, {"error": "A workspace with that code already exists."})
                    return
            except sqlite3.Error as error:
                connection.rollback()
                self.log_error("Could not create workspace: %s", error)
                self.send_json(500, {"error": "Could not create a workspace."})
                return
            finally:
                connection.close()

    def do_PUT(self):
        if self.path != "/api/selections":
            self.send_error(404, "Not found.")
            return
        if self.is_rate_limited(failed_attempts, ATTEMPT_WINDOW, FAILED_ATTEMPTS):
            self.send_json(429, {"error": "Too many invalid workspace codes. Try again shortly."})
            return
        code_hash = self.authorized_workspace()
        if code_hash is False:
            return
        if code_hash is None:
            self.send_json(404, {"error": "Workspace code not found."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(400, "A valid Content-Length header is required.")
            return
        if content_length < 0 or content_length > MAX_REQUEST_SIZE:
            self.send_error(413, "The selections database is too large.")
            return
        try:
            expected_revision = int(self.headers.get("X-Workspace-Revision", ""))
        except ValueError:
            self.send_error(428, "A valid X-Workspace-Revision header is required.")
            return
        if expected_revision < 0:
            self.send_error(400, "Workspace revision cannot be negative.")
            return
        try:
            records = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "The request body must be valid JSON.")
            return
        if not isinstance(records, list) or len(records) > 10000:
            self.send_error(400, "The request body must be a list of at most 10,000 selections.")
            return
        ids = [
            record.get("id")
            for record in records
            if isinstance(record, dict)
        ]
        if (
            len(ids) != len(records)
            or any(not isinstance(record_id, str) or not record_id for record_id in ids)
            or len(ids) != len(set(ids))
        ):
            self.send_error(400, "Each selection must be an object with a unique, non-empty id.")
            return

        connection = None
        revision_conflict = False
        try:
            connection = connect_database(self.database_path)
            connection.execute("BEGIN IMMEDIATE")
            workspace = connection.execute(
                "SELECT revision FROM workspaces WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if workspace is None or workspace[0] != expected_revision:
                connection.rollback()
                revision_conflict = True
            else:
                connection.execute(
                    "DELETE FROM workspace_selections WHERE code_hash = ?", (code_hash,)
                )
                connection.executemany(
                    "INSERT INTO workspace_selections (code_hash, id, record_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (code_hash, record["id"], json.dumps(record, ensure_ascii=False))
                        for record in records
                    ],
                )
                connection.execute(
                    "UPDATE workspaces SET revision = revision + 1 WHERE code_hash = ?",
                    (code_hash,),
                )
                connection.commit()
        except sqlite3.Error as error:
            if connection is not None:
                connection.rollback()
            self.log_error("Could not update the selections database: %s", error)
            self.send_json(500, {"error": "Could not update the selections database."})
            return
        finally:
            if connection is not None:
                connection.close()
        if revision_conflict:
            self.send_json(409, {"error": "Workspace changed elsewhere. Reconnect before saving."})
            return
        self.send_json(200, {"saved": len(records), "revision": expected_revision + 1})


def main():
    parser = argparse.ArgumentParser(
        description="Serve the MP-C controller selector with private, code-protected SQLite workspaces."
    )
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind (default: all interfaces)")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="HTTP port (default: PORT environment variable or 8000)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DATABASE_PATH", ROOT / "saved_selections.sqlite3")),
        help="SQLite database path (default: DATABASE_PATH environment variable or repository database)",
    )
    parser.add_argument(
        "--allowed-origins",
        default=os.environ.get("ALLOWED_ORIGINS", "https://js-forbes.github.io"),
        help="Comma-separated browser origins allowed to call the API",
    )
    args = parser.parse_args()
    database_path = args.database.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(database_path)):
        pass
    bootstrap_codes = [
        code.strip()
        for code in os.environ.get("BOOTSTRAP_CODES", "").split(",")
        if code.strip()
    ]
    for code in bootstrap_codes:
        if not valid_workspace_code(code):
            parser.error("BOOTSTRAP_CODES must contain comma-separated 8-character codes.")
    if bootstrap_codes:
        with closing(connect_database(database_path)) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO workspaces (code_hash, created_at) VALUES (?, ?)",
                [
                    (
                        hash_code(unicodedata.normalize("NFC", code)),
                        datetime.now(timezone.utc).isoformat(),
                    )
                    for code in bootstrap_codes
                ],
            )
            connection.commit()
    SelectorHandler.database_path = database_path
    SelectorHandler.allowed_origins = {
        origin.strip().rstrip("/")
        for origin in args.allowed_origins.split(",")
        if origin.strip()
    }
    server = ThreadingHTTPServer((args.host, args.port), SelectorHandler)
    print(f"Serving http://{args.host}:{args.port}/ with database {database_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping selector server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
