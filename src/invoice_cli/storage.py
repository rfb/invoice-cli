"""XDG paths, restricted files, TOML configuration, and SQLite migrations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib


class Error(Exception):
    """An actionable, expected user error."""


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write(path: Path, content: str) -> None:
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class Paths:
    config: Path
    db: Path
    state: Path
    cache: Path

    @classmethod
    def resolve(cls, db=None, config=None):
        home = Path.home()

        def base(key, fallback):
            value = os.environ.get(key, "")
            return Path(value) if value and Path(value).is_absolute() else home / fallback

        return cls(
            Path(config).expanduser().absolute() if config else base("XDG_CONFIG_HOME", ".config") / "invoice/config.toml",
            Path(db).expanduser().absolute() if db else base("XDG_DATA_HOME", ".local/share") / "invoice/invoices.sqlite3",
            base("XDG_STATE_HOME", ".local/state") / "invoice",
            base("XDG_CACHE_HOME", ".cache") / "invoice",
        )


DEFAULTS = {
    "business-name": "", "business-address": "", "business-email": "",
    "gst-number": "", "default-note": "", "default-tax": "none",
    "payment-terms": 30, "currency": "CAD",
}


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.values = dict(DEFAULTS)
        if path.exists():
            with path.open("rb") as source:
                self.values.update(tomllib.load(source))
        for key, default in DEFAULTS.items():
            value = self.values[key]
            if key == "payment-terms":
                if type(value) is not int or not 0 <= value <= 3650:
                    raise Error("payment-terms in configuration must be an integer between 0 and 3650")
            elif not isinstance(value, str):
                raise Error(f"Configuration {key} must be a string")
        if self.values["currency"] not in {"CAD", "USD", "EUR", "GBP", "AUD", "NZD", "CHF"}:
            raise Error("Unsupported currency in configuration; use CAD USD EUR GBP AUD NZD CHF")

    def save(self):
        # This app deliberately uses a flat TOML configuration.
        text = "# invoice configuration; all prices are tax-exclusive\n"
        for key, value in self.values.items():
            text += f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}\n"
        atomic_write(self.path, text)

    def set(self, key, value):
        if key not in DEFAULTS:
            raise Error(f"Unknown setting {key!r}. Choose: {', '.join(DEFAULTS)}")
        if key == "payment-terms":
            try:
                value = int(value)
            except ValueError:
                raise Error("payment-terms must be an integer number of days") from None
            if not 0 <= value <= 3650:
                raise Error("payment-terms must be between 0 and 3650 days")
        if key == "currency":
            value = value.upper()
            if value not in {"CAD", "USD", "EUR", "GBP", "AUD", "NZD", "CHF"}:
                raise Error("Supported two-decimal currencies: CAD USD EUR GBP AUD NZD CHF")
        self.values[key] = value
        self.save()

    def business(self):
        return {"name": self.values["business-name"], "address": self.values["business-address"],
                "email": self.values["business-email"], "gst_number": self.values["gst-number"]}


SCHEMA = """
CREATE TABLE customers (
 id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 address TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE tax_rates (
 code TEXT NOT NULL, label TEXT NOT NULL, rate TEXT NOT NULL,
 effective_from TEXT NOT NULL, PRIMARY KEY(code, effective_from)
);
CREATE TABLE invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 status TEXT NOT NULL CHECK(status IN ('draft','posted','void')) DEFAULT 'draft',
 issue_date TEXT NOT NULL, due_date TEXT NOT NULL, currency TEXT NOT NULL,
 customer_id INTEGER NOT NULL REFERENCES customers(id),
 customer TEXT NOT NULL, business TEXT NOT NULL, note TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, totals TEXT,
 created_at TEXT NOT NULL, posted_at TEXT, voided_at TEXT, void_reason TEXT,
 source_invoice_id INTEGER REFERENCES invoices(id)
);
CREATE TABLE line_items (
 id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, description TEXT NOT NULL, quantity TEXT NOT NULL,
 unit TEXT NOT NULL, unit_price TEXT NOT NULL, tax TEXT NOT NULL,
 tax_label TEXT NOT NULL, tax_rate TEXT NOT NULL,
 net_cents INTEGER NOT NULL, tax_cents INTEGER NOT NULL,
 UNIQUE(invoice_id, position)
);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO schema_migrations VALUES(1, CURRENT_TIMESTAMP);
"""


class Store:
    def __init__(self, path: Path, initialize=False):
        self.path = path
        if not path.exists() and not initialize:
            raise Error("No invoice database. Run `invoice init` first.")
        private_dir(path.parent)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # DELETE journaling keeps the single-file backup story simple when closed.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if not initialize:
                self.conn.close()
                raise Error("Database is not initialized. Run `invoice init`.")
            try:
                self.conn.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")
            except Exception:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise
        elif version != 1:
            self.conn.close()
            raise Error(f"Unsupported database schema {version}; this version supports 1.")

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def next_id(self):
        row = self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='invoices'").fetchone()
        return (row[0] if row else 0) + 1

    def set_next(self, value):
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise Error("Next ID must be a positive integer") from None
        if not 1 <= value <= 9223372036854775807:
            raise Error("Next ID must be between 1 and 9223372036854775807")
        with self.transaction():
            if value < self.next_id():
                raise Error(f"Cannot move backwards; next ID is {self.next_id():06d}")
            if self.conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='invoices'").fetchone():
                self.conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='invoices'", (value - 1,))
            else:
                self.conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('invoices',?)", (value - 1,))


def iso_date(value):
    try:
        result = date.fromisoformat(str(value))
        if result.isoformat() != str(value):
            raise ValueError
        return result
    except ValueError:
        raise Error(f"Invalid date {value!r}; use YYYY-MM-DD") from None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
