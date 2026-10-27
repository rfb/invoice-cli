"""Invoice operations. All changes are transactional; all money uses Decimal."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import re

from .storage import Config, Error, Store, iso_date, timestamp

TREATMENTS = {"exempt": "Exempt", "zero-rated": "Zero-rated", "none": "No tax"}
COLUMNS = ["description", "quantity", "unit", "unit_price", "tax"]
MAX_DECIMAL = Decimal("1000000000000")


def decimal(value, field, *, positive=False, places=6, maximum=MAX_DECIMAL):
    try:
        raw = str(value)
        if len(raw) > 80:
            raise ValueError
        number = Decimal(raw)
        if not number.is_finite() or number < 0 or (positive and number == 0) or number > maximum:
            raise ValueError
        if number.as_tuple().exponent < -places:
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        qualifier = "positive" if positive else "nonnegative"
        raise Error(f"{field}: expected a finite {qualifier} decimal, at most {places} decimal places and <= {maximum}") from None


def money(cents):
    return f"{Decimal(cents) / 100:,.2f}"


def price_display(value):
    number = Decimal(value)
    places = max(2, -number.as_tuple().exponent)
    return f"{number:,.{places}f}"


def cents(number):
    result = int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if abs(result) > 9000000000000000:
        raise Error("Amount exceeds supported currency total")
    return result


def reference(value):
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or len(text) > 24:
        raise Error("Invoice ID must be a positive integer, padded or unpadded")
    result = int(text)
    if not 1 <= result <= 9223372036854775807:
        raise Error("Invoice ID is outside the supported range")
    return result


class App:
    def __init__(self, store: Store, config: Config):
        self.store, self.config, self.db = store, config, store.conn

    def customer(self, slug):
        row = self.db.execute("SELECT * FROM customers WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise Error(f"Customer {slug!r} not found")
        return dict(row)

    def add_customer(self, slug, name, address="", email=""):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise Error("Slug must use lowercase ASCII letters/digits separated by hyphens")
        if not name.strip():
            raise Error("Customer name is required")
        if not address.strip() and not email.strip():
            raise Error("Customer requires an email address or postal address (or both)")
        if self.db.execute("SELECT 1 FROM customers WHERE slug=?", (slug,)).fetchone():
            raise Error(f"Customer slug {slug!r} is already in use")
        with self.store.transaction():
            self.db.execute("INSERT INTO customers(slug,name,address,email) VALUES(?,?,?,?)", (slug, name, address, email))
        return self.customer(slug)

    def update_customer(self, slug, **values):
        customer = self.customer(slug)
        for key, value in values.items():
            if value is not None:
                customer[key] = value
        if not customer["name"].strip():
            raise Error("Customer name is required")
        if not customer["address"].strip() and not customer["email"].strip():
            raise Error("Customer requires an email address or postal address (or both)")
        with self.store.transaction():
            self.db.execute("UPDATE customers SET name=?,address=?,email=? WHERE id=?",
                            (customer["name"], customer["address"], customer["email"], customer["id"]))
        return customer

    def add_tax(self, code, rate, label=None, effective="0001-01-01"):
        if code in TREATMENTS or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", code):
            raise Error("Tax code must start with a letter; exempt, zero-rated and none are reserved")
        iso_date(effective)
        rate = str(decimal(rate, "tax rate", maximum=Decimal(100)))
        if self.db.execute("SELECT 1 FROM tax_rates WHERE code=? AND effective_from=?", (code, effective)).fetchone():
            raise Error("That code/effective date already exists. Use tax update or add a new effective date.")
        with self.store.transaction():
            self.db.execute("INSERT INTO tax_rates VALUES(?,?,?,?)", (code, label or code, rate, effective))

    def update_tax(self, code, *, effective=None, new_effective=None, rate=None, label=None):
        if code in TREATMENTS:
            raise Error("Built-in tax treatments cannot be edited")
        if all(value is None for value in (new_effective, rate, label)):
            raise Error("Specify --rate, --label or --new-effective to update")
        if effective is not None:
            iso_date(effective)
        if new_effective is not None:
            iso_date(new_effective)
        if rate is not None:
            rate = str(decimal(rate, "tax rate", maximum=Decimal(100)))
        if label is not None and not label.strip():
            raise Error("Tax label is required")
        with self.store.transaction():
            rows = self.db.execute("SELECT * FROM tax_rates WHERE code=? ORDER BY effective_from", (code,)).fetchall()
            if effective is not None:
                rows = [row for row in rows if row["effective_from"] == effective]
            if not rows:
                raise Error(f"Tax definition {code!r} not found" + (f" effective {effective}" if effective else ""))
            if len(rows) > 1:
                raise Error("Multiple tax definitions exist; select one with --effective YYYY-MM-DD")
            row = rows[0]
            target = new_effective if new_effective is not None else row["effective_from"]
            if target != row["effective_from"] and self.db.execute(
                    "SELECT 1 FROM tax_rates WHERE code=? AND effective_from=?", (code, target)).fetchone():
                raise Error("That code/effective date already exists")
            self.db.execute("UPDATE tax_rates SET label=?,rate=?,effective_from=? WHERE code=? AND effective_from=?",
                            (label if label is not None else row["label"],
                             rate if rate is not None else row["rate"], target, code, row["effective_from"]))

    def tax(self, code, issue_date):
        if code in TREATMENTS:
            return TREATMENTS[code], "0"
        row = self.db.execute("SELECT label,rate FROM tax_rates WHERE code=? AND effective_from<=? ORDER BY effective_from DESC LIMIT 1", (code, issue_date)).fetchone()
        if not row:
            raise Error(f"tax: {code!r} has no applicable rate on {issue_date}")
        return row["label"], row["rate"]

    def tax_options(self, issue_date):
        result = []
        for row in self.db.execute("SELECT DISTINCT code FROM tax_rates ORDER BY code"):
            try:
                label, rate = self.tax(row[0], issue_date)
                result.append(f"{row[0]}: {label}, {rate}%")
            except Error:
                result.append(f"{row[0]}: unavailable on {issue_date}")
        result.extend(f"{code}: {label}, 0%" for code, label in TREATMENTS.items())
        return result

    def line(self, values, issue_date, previous=None):
        description = values.get("description", "")
        if not description.strip():
            raise Error("description: required text")
        if len(description) > 20000:
            raise Error("description: maximum 20000 characters")
        quantity = decimal(values.get("quantity", ""), "quantity", positive=True)
        price = decimal(values.get("unit_price", ""), "unit_price")
        unit = values.get("unit", "")
        if not unit.strip() or len(unit) > 40 or "\n" in unit or "\r" in unit:
            raise Error("unit: required descriptive label, at most 40 characters, on one line")
        code = values.get("tax", "")
        # Preserve already reviewed rates during ordinary editing.
        if previous and previous["tax"] == code:
            label, rate = previous["tax_label"], previous["tax_rate"]
        else:
            label, rate = self.tax(code, issue_date)
        with localcontext() as ctx:
            ctx.prec = 50
            net = cents(quantity * price)
            tax = cents(Decimal(net) / 100 * Decimal(rate) / 100)
        return {"description": description, "quantity": str(quantity), "unit": unit,
                "unit_price": str(price), "tax": code, "tax_label": label,
                "tax_rate": rate, "net_cents": net, "tax_cents": tax}

    @staticmethod
    def totals(lines):
        groups = {}
        for item in lines:
            if item["tax"] in TREATMENTS:
                continue
            key = (item["tax"], item["tax_label"], item["tax_rate"])
            groups[key] = groups.get(key, 0) + item["tax_cents"]
        net = sum(x["net_cents"] for x in lines)
        tax = sum(x["tax_cents"] for x in lines)
        if net + tax > 9000000000000000:
            raise Error("Invoice total exceeds supported currency total")
        return {"subtotal_cents": net, "tax_cents": tax, "total_cents": net + tax,
                "taxes": [{"code": k[0], "label": k[1], "rate": k[2], "cents": v} for k, v in groups.items()]}

    def invoice(self, ref):
        row = self.db.execute("SELECT * FROM invoices WHERE id=?", (reference(ref),)).fetchone()
        if not row:
            raise Error(f"Invoice {reference(ref):06d} not found")
        invoice = dict(row)
        for key in ("customer", "business"):
            invoice[key] = json.loads(invoice[key])
        invoice["lines"] = [dict(x) for x in self.db.execute("SELECT * FROM line_items WHERE invoice_id=? ORDER BY position", (invoice["id"],))]
        invoice["totals"] = json.loads(invoice["totals"]) if invoice["totals"] else self.totals(invoice["lines"])
        return invoice

    def draft(self, ref):
        inv = self.invoice(ref)
        if inv["status"] != "draft":
            raise Error(f"Invoice {inv['id']:06d} is {inv['status']}; its contents are read-only")
        return inv

    def create(self, slug, issue_date=None, due_date=None, source=None):
        customer = self.customer(slug)
        issue = iso_date(issue_date or date.today().isoformat())
        due = iso_date(due_date) if due_date else issue + timedelta(days=int(self.config.values["payment-terms"]))
        if due < issue:
            raise Error("Due date cannot be before issue date")
        lines = []
        currency = self.config.values["currency"]
        if source:
            if source["customer_id"] != customer["id"]:
                raise Error("Source invoice belongs to a different customer")
            currency = source["currency"]
            lines = [self.line(item, issue.isoformat()) for item in source["lines"]]
        with self.store.transaction():
            cursor = self.db.execute("INSERT INTO invoices(issue_date,due_date,currency,customer_id,customer,business,note,created_at,source_invoice_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                     (issue.isoformat(), due.isoformat(), currency, customer["id"], json.dumps(customer), json.dumps(self.config.business()),
                                      self.config.values["default-note"], timestamp(), source["id"] if source else None))
            ref = cursor.lastrowid
            self._write_lines(ref, lines)
        return self.invoice(ref)

    def latest(self, slug):
        customer = self.customer(slug)
        row = self.db.execute("SELECT id FROM invoices WHERE customer_id=? AND status='posted' ORDER BY posted_at DESC,id DESC LIMIT 1", (customer["id"],)).fetchone()
        if not row:
            raise Error(f"No posted, non-void invoices for {slug}. Use `invoice new --customer {slug}`.")
        return self.invoice(row[0])

    def _write_lines(self, ref, lines):
        self.db.execute("DELETE FROM line_items WHERE invoice_id=?", (ref,))
        for position, line in enumerate(lines, 1):
            self.db.execute("INSERT INTO line_items(invoice_id,position,description,quantity,unit,unit_price,tax,tax_label,tax_rate,net_cents,tax_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (ref, position, *(line[k] for k in ["description", "quantity", "unit", "unit_price", "tax", "tax_label", "tax_rate", "net_cents", "tax_cents"])))

    def replace_lines(self, ref, lines, revision):
        self.totals(lines)
        with self.store.transaction():
            current = self.draft(ref)
            if current["revision"] != revision:
                raise Error("Invoice changed while you were editing; refusing to overwrite concurrent changes")
            self._write_lines(current["id"], lines)
            self.db.execute("UPDATE invoices SET revision=revision+1 WHERE id=?", (current["id"],))

    def add_line(self, ref, values):
        inv = self.draft(ref)
        values = dict(values)
        if values.get("tax") is None:
            values["tax"] = self.config.values["default-tax"]
        lines = inv["lines"] + [self.line(values, inv["issue_date"])]
        self.replace_lines(ref, lines, inv["revision"])

    def edit_line(self, ref, position, changes):
        inv = self.draft(ref)
        if not 1 <= position <= len(inv["lines"]):
            raise Error("Line number does not exist")
        original = inv["lines"][position - 1]
        values = {**original, **{k: v for k, v in changes.items() if v is not None}}
        inv["lines"][position - 1] = self.line(values, inv["issue_date"], previous=original)
        self.replace_lines(ref, inv["lines"], inv["revision"])

    def remove_line(self, ref, position):
        inv = self.draft(ref)
        if not 1 <= position <= len(inv["lines"]):
            raise Error("Line number does not exist")
        del inv["lines"][position - 1]
        self.replace_lines(ref, inv["lines"], inv["revision"])

    def update(self, ref, *, due=None, issue=None, note=None, refresh=False, refresh_tax=False):
        with self.store.transaction():
            inv = self.draft(ref)
            if issue:
                inv["issue_date"] = iso_date(issue).isoformat()
            if due:
                inv["due_date"] = iso_date(due).isoformat()
            if inv["due_date"] < inv["issue_date"]:
                raise Error("Due date cannot be before issue date")
            if note is not None:
                inv["note"] = note
            if refresh:
                inv["customer"] = self.customer(inv["customer"]["slug"])
                inv["business"] = self.config.business()
            if issue or refresh_tax:
                inv["lines"] = [self.line(x, inv["issue_date"]) for x in inv["lines"]]
                self.totals(inv["lines"])
                self._write_lines(inv["id"], inv["lines"])
            self.db.execute("UPDATE invoices SET issue_date=?,due_date=?,note=?,customer=?,business=?,revision=revision+1 WHERE id=?",
                            (inv["issue_date"], inv["due_date"], inv["note"], json.dumps(inv["customer"]), json.dumps(inv["business"]), inv["id"]))

    def post(self, ref):
        with self.store.transaction():
            inv = self.invoice(ref)
            if inv["status"] == "posted":
                return inv
            if inv["status"] != "draft":
                raise Error("Cannot post a void invoice")
            for kind in ("business", "customer"):
                if not inv[kind]["name"].strip():
                    raise Error(f"Set {kind} name before posting (then refresh draft details if needed)")
                if not inv[kind]["address"].strip() and not inv[kind]["email"].strip():
                    raise Error(f"Set {kind} email or postal address before posting (then refresh draft details if needed)")
            if not inv["lines"]:
                raise Error("Add at least one line item before posting")
            for line in inv["lines"]:
                if line["tax"] not in TREATMENTS:
                    self.tax(line["tax"], inv["issue_date"])
                    if line["tax"].upper() == "GST" and not inv["business"]["gst_number"].strip():
                        raise Error("Set gst-number and refresh draft details before posting GST invoices")
            self.db.execute("UPDATE invoices SET status='posted',totals=?,posted_at=?,revision=revision+1 WHERE id=?",
                            (json.dumps(self.totals(inv["lines"])), timestamp(), inv["id"]))
        return self.invoice(ref)

    def void(self, ref, reason):
        if not reason.strip():
            raise Error("A void reason is required")
        with self.store.transaction():
            inv = self.invoice(ref)
            if inv["status"] != "posted":
                raise Error("Only posted invoices can be voided")
            self.db.execute("UPDATE invoices SET status='void',void_reason=?,voided_at=?,revision=revision+1 WHERE id=?", (reason, timestamp(), inv["id"]))

    def delete(self, ref):
        with self.store.transaction():
            inv = self.draft(ref)
            self.db.execute("DELETE FROM invoices WHERE id=?", (inv["id"],))

    def list_invoices(self, customer=None, status=None):
        clauses, params = [], []
        if customer:
            clauses.append("customer_id=?")
            params.append(self.customer(customer)["id"])
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        ids = self.db.execute("SELECT id FROM invoices" + where + " ORDER BY id DESC", params).fetchall()
        return [self.invoice(row[0]) for row in ids]


def public_invoice(inv):
    """JSON decimals remain strings; padded IDs are presentation, not keys."""
    result = dict(inv)
    result["id"] = f"{inv['id']:06d}"
    result["source_invoice_id"] = f"{inv['source_invoice_id']:06d}" if inv["source_invoice_id"] else None
    result["lines"] = [{k: v for k, v in x.items() if k not in {"id", "invoice_id"}} for x in inv["lines"]]
    return result
