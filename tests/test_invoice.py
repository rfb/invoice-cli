from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from invoice_cli.cli import main
from invoice_cli.core import App, decimal, public_invoice
from invoice_cli.editor import edit, export_csv, import_csv
from invoice_cli.pdf import destination, render
from invoice_cli.storage import Config, Error, Paths, Store


class InvoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / "config.toml")
        for key, value in {
            "business-name": "Example Consulting", "business-address": "123 Test Street\nBC, Canada",
            "gst-number": "123456789RT0001", "default-tax": "GST", "default-note": "Thank you!",
        }.items():
            self.config.set(key, value)
        self.store = Store(self.root / "db.sqlite3", initialize=True)
        self.app = App(self.store, self.config)
        self.app.add_customer("acme", "Acme Ltd.", "456 Client Street", "accounts@example.com")
        self.app.add_tax("GST", "5")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def invoice(self, posted=False):
        inv = self.app.create("acme", "2026-10-26")
        self.app.add_line(inv["id"], dict(description="Development", quantity="24", unit="hour", unit_price="150"))
        self.app.add_line(inv["id"], dict(description="Deployment", quantity="1", unit="each", unit_price="800"))
        return self.app.post(inv["id"]) if posted else self.app.invoice(inv["id"])

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(["--db", str(self.store.path), "--config", str(self.config.path), *args])
        return out.getvalue(), err.getvalue()

    def test_mixed_hourly_fixed_totals(self):
        t = self.invoice()["totals"]
        self.assertEqual((t["subtotal_cents"], t["tax_cents"], t["total_cents"]), (440000, 22000, 462000))

    def test_customer_contact_requirements(self):
        with self.assertRaisesRegex(Error, "email address or postal address"):
            self.app.add_customer("missing", "Missing", " ", " ")
        with self.assertRaises(Error):
            self.app.update_customer("acme", address="", email="")
        self.assertEqual(self.app.customer("acme")["address"], "456 Client Street")
        out, err = self.cli("customer", "add", "--slug", "email-only", "--name", "Email Only",
                            "--email", "billing@example.com")
        self.assertFalse(err)
        self.assertIn("Added email-only", out)
        inv = self.app.create("email-only", "2026-10-26")
        self.app.add_line(inv["id"], dict(description="Service", quantity="1", unit="each", unit_price="10"))
        self.assertEqual(self.app.post(inv["id"])["status"], "posted")

    def test_business_contact_requirements_and_multiline_address(self):
        out, err = self.cli("config", "set", "business-address", "--address-line", "123 Supplier St",
                            "--address-line", "Vancouver BC")
        self.assertFalse(err)
        self.config = Config(self.config.path)
        self.app = App(self.store, self.config)
        self.assertEqual(self.config.values["business-address"], "123 Supplier St\nVancouver BC")
        inv = self.invoice()
        self.assertEqual(inv["business"]["address"], "123 Supplier St\nVancouver BC")
        self.config.set("business-address", "")
        self.config.set("business-email", "")
        self.app.update(inv["id"], refresh=True)
        with self.assertRaisesRegex(Error, "business email or postal address"):
            self.app.post(inv["id"])
        self.config.set("business-email", "supplier@example.com")
        with self.assertRaises(Error):
            self.app.post(inv["id"])
        self.app.update(inv["id"], refresh=True)
        posted = self.app.post(inv["id"])
        self.assertEqual(posted["business"]["email"], "supplier@example.com")
        self.config.set("business-email", "new@example.com")
        self.assertEqual(self.app.invoice(inv["id"]), posted)

    def test_customer_multiline_address_cli_and_snapshots(self):
        inv = self.invoice()
        out, err = self.cli("customer", "update", "acme", "--address-line", "39614 Highway 16",
                            "--address-line", "PO Box 10", "--address-line", "Tlell BC V0T1Y0")
        self.assertFalse(err)
        address = "39614 Highway 16\nPO Box 10\nTlell BC V0T1Y0"
        self.assertEqual(self.app.customer("acme")["address"], address)
        self.assertEqual(self.app.invoice(inv["id"]), inv)
        self.app.update(inv["id"], refresh=True)
        out, _ = self.cli("show", str(inv["id"]))
        self.assertIn(address, out)
        from pypdf import PdfReader
        output = render(self.app.invoice(inv["id"]), self.root / "address.pdf")
        text = "\n".join(page.extract_text() for page in PdfReader(output).pages)
        for line in address.splitlines():
            self.assertIn(line, text)
        out, err = self.cli("customer", "add", "--slug", "postal-only", "--name", "Postal Only",
                            "--address-line", "123 Main St", "--address-line", "Vancouver BC")
        self.assertFalse(err)
        self.assertEqual(self.app.customer("postal-only")["address"], "123 Main St\nVancouver BC")

    def test_fractional_hours_and_rounding(self):
        inv = self.app.create("acme")
        self.app.add_line(inv["id"], dict(description="Tiny", quantity="1", unit="each", unit_price="0.105"))
        line = self.app.invoice(inv["id"])["lines"][0]
        self.assertEqual((line["net_cents"], line["tax_cents"]), (11, 1))
        result = self.app.line(dict(description="Fraction", quantity="1.25", unit="hour", unit_price="99.999", tax="GST"), inv["issue_date"])
        self.assertEqual(result["net_cents"], 12500)

    def test_decimal_validation(self):
        for raw in ["NaN", "Infinity", "-1", "0.0000001", "1e1000", "text"]:
            with self.subTest(raw=raw), self.assertRaises(Error):
                decimal(raw, "test")
        with self.assertRaises(Error):
            decimal("0", "test", positive=True)

    def test_tax_treatments_distinct(self):
        inv = self.app.create("acme")
        for tax in ["GST", "exempt", "zero-rated", "none"]:
            self.app.add_line(inv["id"], dict(description=tax, quantity="1", unit="each", unit_price="100", tax=tax))
        inv = self.app.invoice(inv["id"])
        self.assertEqual(inv["totals"]["tax_cents"], 500)
        self.assertEqual([x["tax"] for x in inv["lines"]], ["GST", "exempt", "zero-rated", "none"])

    def test_csv_tax_whitespace_and_case_preserve_reviewed_rate(self):
        inv = self.invoice()
        self.app.add_tax("GST", "7", effective="2026-01-01")
        text = "description,quantity,unit,unit_price,tax\nDevelopment,24,hour,150, gst \n"
        lines = import_csv(self.app, inv, text)
        self.assertEqual((lines[0]["tax"], lines[0]["tax_rate"]), ("GST", "5"))
        self.app.add_tax("gst", "3")
        with self.assertRaises(Error):
            import_csv(self.app, inv, text.replace(" gst ", " GsT "))
        lines = import_csv(self.app, inv, text)
        self.assertEqual((lines[0]["tax"], lines[0]["tax_rate"]), ("gst", "3"))

    def test_tax_effective_date_and_frozen_drafts(self):
        inv = self.invoice()
        self.app.add_tax("GST", "7", effective="2026-10-01")
        self.assertEqual(self.app.invoice(inv["id"])["totals"]["tax_cents"], 22000)
        self.app.update(inv["id"], refresh_tax=True)
        self.assertEqual(self.app.invoice(inv["id"])["totals"]["tax_cents"], 30800)
        self.app.update(inv["id"], issue="2026-09-30")
        self.assertEqual(self.app.invoice(inv["id"])["totals"]["tax_cents"], 22000)

    def test_cli_update_tax_and_snapshots(self):
        draft = self.invoice()
        posted = self.invoice(posted=True)
        out, err = self.cli("tax", "update", "GST", "--rate", "7", "--label", "Updated GST",
                            "--new-effective", "2026-01-01")
        self.assertFalse(err)
        self.assertIn("Updated GST", out)
        self.assertEqual(self.app.tax("GST", "2026-10-26"), ("Updated GST", "7"))
        with self.assertRaises(Error):
            self.app.tax("GST", "2025-12-31")
        self.assertEqual(self.app.invoice(draft["id"]), draft)
        self.assertEqual(self.app.invoice(posted["id"]), posted)
        self.app.update(draft["id"], refresh_tax=True)
        self.assertEqual(self.app.invoice(draft["id"])["totals"]["tax_cents"], 30800)
        self.cli("tax", "update", "GST", "--label", "Renamed")
        self.assertEqual(self.app.tax("GST", "2026-10-26"), ("Renamed", "7"))

    def test_tax_update_selection_and_validation(self):
        self.app.add_tax("GST", "6", effective="2030-01-01")
        original = [tuple(row) for row in self.app.db.execute("SELECT * FROM tax_rates ORDER BY effective_from")]
        for code, values in [
            ("GST", dict(rate="8")),
            ("MISSING", dict(rate="8")),
            ("none", dict(rate="8")),
            ("GST", dict(effective="2029-01-01", rate="8")),
            ("GST", dict(effective="0001-01-01", new_effective="2030-01-01")),
            ("GST", dict(effective="0001-01-01", new_effective="invalid")),
            ("GST", dict(effective="0001-01-01", label=" ")),
            ("GST", dict(effective="0001-01-01")),
            *[("GST", dict(effective="0001-01-01", rate=rate)) for rate in ["NaN", "-1", "101"]],
        ]:
            with self.subTest(code=code, values=values), self.assertRaises(Error):
                self.app.update_tax(code, **values)
            self.assertEqual([tuple(row) for row in self.app.db.execute("SELECT * FROM tax_rates ORDER BY effective_from")], original)
        out, err = self.cli("tax", "update", "GST", "--effective", "2030-01-01", "--rate", "0")
        self.assertFalse(err)
        self.assertIn("Updated GST", out)
        self.assertEqual(self.app.tax("GST", "2030-01-01"), ("GST", "0"))
        self.assertEqual(self.app.tax("GST", "2029-12-31"), ("GST", "5"))

    def test_tax_snapshot_listing(self):
        draft = self.invoice()
        posted = self.invoice(posted=True)
        out, err = self.cli("tax", "update", "GST", "--rate", "7", "--label", "New GST")
        self.assertFalse(err)
        self.assertIn(f"{draft['id']:06d}  draft", out)
        self.assertIn(f"{posted['id']:06d}  posted", out)
        self.assertIn("saved: 5% GST  applicable now: 7% New GST", out)
        self.assertIn(f"invoice update {draft['id']:06d} --refresh-tax", out)
        self.assertNotIn(f"invoice update {posted['id']:06d} --refresh-tax", out)
        self.assertEqual(self.app.invoice(draft["id"]), draft)
        again, err = self.cli("tax", "snapshots", "GST")
        self.assertFalse(err)
        self.assertIn("saved: 5% GST", again)
        self.app.update(draft["id"], refresh_tax=True)
        out, _ = self.cli("tax", "snapshots", "GST")
        self.assertNotIn(f"invoice update {draft['id']:06d} --refresh-tax", out)
        self.app.update_tax("GST", new_effective="2030-01-01")
        out, _ = self.cli("tax", "snapshots", "GST")
        self.assertIn("unavailable on issue date", out)
        out, _ = self.cli("tax", "snapshots", "unused")
        self.assertIn("No invoice snapshots", out)

    def test_line_tax_labels_in_terminal_and_pdf(self):
        from pypdf import PdfReader
        inv = self.invoice()
        self.app.update_tax("GST", label="Sales tax")
        self.app.update(inv["id"], refresh_tax=True)
        inv = self.app.invoice(inv["id"])
        out, err = self.cli("show", str(inv["id"]))
        self.assertFalse(err)
        self.assertIn("Sales tax 5%", out)
        self.assertNotIn("GST 5%", out)
        output = render(inv, self.root / "labels.pdf")
        text = " ".join(page.extract_text() for page in PdfReader(output).pages)
        self.assertIn("Sales tax 5%", " ".join(text.split()))
        self.assertNotIn("GST 5%", " ".join(text.split()))

    def test_note_defaults_override_and_clear(self):
        inv = self.app.create("acme")
        self.config.set("default-note", "New note")
        self.assertEqual(self.app.invoice(inv["id"])["note"], "Thank you!")
        self.app.update(inv["id"], note="Override")
        self.assertEqual(self.app.invoice(inv["id"])["note"], "Override")
        self.app.update(inv["id"], note="")
        self.assertEqual(self.app.invoice(inv["id"])["note"], "")
        self.assertEqual(self.app.create("acme")["note"], "New note")

    def test_sequence_start_delete_and_no_reuse(self):
        self.store.set_next(1234)
        inv = self.app.create("acme")
        self.assertEqual(inv["id"], 1234)
        self.assertEqual(self.app.invoice("001234")["id"], 1234)
        self.app.delete(1234)
        self.assertEqual(self.app.create("acme")["id"], 1235)
        with self.assertRaises(Error):
            self.store.set_next(1234)
        self.store.set_next(1500)
        self.assertEqual(self.app.create("acme")["id"], 1500)

    def test_more_than_six_digits_no_wrap(self):
        self.store.set_next(1000000)
        inv = self.app.create("acme")
        self.assertEqual(public_invoice(inv)["id"], "1000000")
        self.assertEqual(destination(inv).name, "invoice-acme-1000000-draft.pdf")

    def test_sequence_rejects_invalid_values(self):
        for value in [0, -1, 9223372036854775808, "not-an-id"]:
            with self.subTest(value=value), self.assertRaises(Error):
                self.store.set_next(value)

    def test_concurrent_creation_unique(self):
        def create(_):
            store = Store(self.store.path)
            try:
                return App(store, self.config).create("acme")["id"]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(12)))
        self.assertEqual(sorted(ids), list(range(1, 13)))

    def test_post_idempotent_and_snapshots_immutable(self):
        inv = self.invoice(posted=True)
        next_id = self.store.next_id()
        self.app.update_customer("acme", name="Different", address="Elsewhere")
        self.config.set("business-name", "Changed")
        self.config.set("default-note", "Changed")
        self.app.add_tax("GST", "8", effective="2026-10-01")
        self.assertEqual(self.app.post(inv["id"]), inv)
        self.assertEqual(self.store.next_id(), next_id)
        for op in [lambda: self.app.update(inv["id"], note="bad"),
                   lambda: self.app.remove_line(inv["id"], 1), lambda: self.app.delete(inv["id"])]:
            with self.assertRaises(Error):
                op()

    def test_post_validation_rollback(self):
        inv = self.app.create("acme")
        next_id = self.store.next_id()
        with self.assertRaises(Error):
            self.app.post(inv["id"])
        self.assertEqual(self.app.invoice(inv["id"])["status"], "draft")
        self.assertEqual(self.store.next_id(), next_id)
        self.assertIsNone(self.app.invoice(inv["id"])["posted_at"])

    def test_post_requires_gst_registration(self):
        self.config.set("gst-number", "")
        inv = self.invoice()
        with self.assertRaisesRegex(Error, "gst-number"):
            self.app.post(inv["id"])

    def test_void_retains_original(self):
        inv = self.invoice(posted=True)
        self.app.void(inv["id"], "Issued in error")
        result = self.app.invoice(inv["id"])
        self.assertEqual(result["lines"], inv["lines"])
        self.assertEqual(result["totals"], inv["totals"])
        self.assertEqual(result["status"], "void")
        with self.assertRaises(Error):
            self.app.delete(inv["id"])

    def test_repeat_uses_latest_posting_not_issue_date(self):
        old = self.invoice(posted=True)
        second = self.app.create("acme", "2025-01-01")
        self.app.add_line(second["id"], dict(description="Latest posting", quantity="2", unit="hour", unit_price="10"))
        second = self.app.post(second["id"])
        self.app.create("acme")  # later draft must not be selected
        self.assertEqual(self.app.latest("acme")["id"], second["id"])
        self.app.void(second["id"], "wrong")
        self.assertEqual(self.app.latest("acme")["id"], old["id"])

    def test_repeat_refreshes_defaults_and_rates(self):
        source = self.invoice(posted=True)
        self.app.add_tax("GST", "7", effective="2026-01-01")
        self.config.set("default-note", "New default")
        self.app.update_customer("acme", address="New address")
        before = self.app.invoice(source["id"])
        copy = self.app.create("acme", source=source)
        self.assertEqual(copy["source_invoice_id"], source["id"])
        self.assertEqual(copy["note"], "New default")
        self.assertEqual(copy["customer"]["address"], "New address")
        self.assertEqual(copy["issue_date"], date.today().isoformat())
        self.assertEqual(copy["status"], "draft")
        self.assertEqual(copy["totals"]["tax_cents"], 30800)
        self.assertEqual(self.app.invoice(source["id"]), before)

    def test_repeat_missing_source_fails(self):
        with self.assertRaisesRegex(Error, "No posted"):
            self.app.latest("acme")
        self.assertEqual(self.store.next_id(), 1)

    def test_repeat_unavailable_code_no_draft(self):
        source = self.invoice(posted=True)
        self.store.conn.execute("DELETE FROM tax_rates")  # simulate unavailable historical definition
        next_id = self.store.next_id()
        with self.assertRaises(Error):
            self.app.create("acme", source=source)
        self.assertEqual(self.store.next_id(), next_id)

    def test_csv_roundtrip_comments_and_multiline_hash(self):
        inv = self.app.create("acme")
        description = '#First, "quoted"\n#Not a comment\nlast line'
        self.app.add_line(inv["id"], dict(description=description, quantity="1.5", unit="hour", unit_price="12.345"))
        inv = self.app.invoice(inv["id"])
        text = export_csv(self.app, inv)
        self.assertIn("# quantity:", text)
        self.assertIn("# GST: GST, 5%", text)
        self.assertIn("# zero-rated:", text)
        lines = import_csv(self.app, inv, text + "# a final comment\n\n")
        self.assertEqual(lines[0]["description"], description)
        self.assertEqual(lines[0]["net_cents"], inv["lines"][0]["net_cents"])

    def test_csv_error_location_and_atomicity(self):
        inv = self.invoice()
        csv = "# guide\n\ndescription,quantity,unit,unit_price,tax\nGood,1,each,10,GST\nBad,NaN,each,10,GST\n"
        with self.assertRaisesRegex(Error, "Line 5, quantity"):
            import_csv(self.app, inv, csv)
        self.assertEqual(self.app.invoice(inv["id"]), inv)

    def test_csv_header_and_unclosed_quote(self):
        inv = self.invoice()
        for csv in ["bad,headers\n", 'description,quantity,unit,unit_price,tax\n"unclosed\n# data\n']:
            with self.assertRaises(Error):
                import_csv(self.app, inv, csv)

    def test_csv_preserves_reviewed_rates(self):
        inv = self.invoice()
        self.app.add_tax("GST", "7", effective="2026-10-01")
        text = export_csv(self.app, inv).replace('"24"', '"25"')
        lines = import_csv(self.app, inv, text)
        self.assertEqual(lines[0]["tax_rate"], "5")

    def test_csv_comment_guide_injection_safe(self):
        self.app.add_tax("OTHER", "2", label='Other\nInjected,1,each,100,none')
        inv = self.invoice()
        self.assertEqual(len(import_csv(self.app, inv, export_csv(self.app, inv))), 2)

    def test_concurrent_csv_edit_rejected(self):
        inv = self.invoice()
        self.app.update(inv["id"], note="Concurrent")
        with self.assertRaisesRegex(Error, "changed"):
            self.app.replace_lines(inv["id"], inv["lines"], inv["revision"])
        self.assertEqual(self.app.invoice(inv["id"])["note"], "Concurrent")

    def test_editor_failure_retains_recovery(self):
        inv = self.invoice()
        with redirect_stderr(io.StringIO()), self.assertRaisesRegex(Error, "recovery"):
            edit(self.app, inv["id"], self.root / "state", [sys.executable, "-c", "import sys; sys.exit(1)"])
        files = list((self.root / "state/edits").glob("*.csv"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.invoice(inv["id"]), inv)

    def test_editor_success_removes_recovery(self):
        inv = self.invoice()
        script = "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_text(p.read_text().replace('Development','New development'))"
        result = edit(self.app, inv["id"], self.root / "state", [sys.executable, "-c", script])
        self.assertEqual(result["lines"][0]["description"], "New development")
        self.assertEqual(list((self.root / "state/edits").glob("*.csv")), [])

    def test_cli_json_id_and_decimal_types(self):
        inv = self.invoice()
        out, err = self.cli("show", str(inv["id"]), "--json")
        data = json.loads(out)
        self.assertEqual(data["id"], "000001")
        self.assertIsInstance(data["lines"][0]["unit_price"], str)
        self.assertEqual(err, "")

    def test_cli_item_modes(self):
        inv = self.app.create("acme")
        self.cli("item", "add", str(inv["id"]), "Hourly", "--hours", "24", "--rate", "150")
        self.cli("item", "add", str(inv["id"]), "Fixed", "--amount", "800")
        self.assertEqual(self.app.invoice(inv["id"])["totals"]["total_cents"], 462000)

    def test_cli_repeat_preflights_editor(self):
        self.invoice(posted=True)
        next_id = self.store.next_id()
        with patch("sys.stdin.isatty", return_value=False), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("repeat", "--customer", "acme")
        self.assertEqual(self.store.next_id(), next_id)

    def test_cli_repeat_launches_editor_with_copied_lines(self):
        source = self.invoice(posted=True)
        command = [sys.executable, "-c", "import sys; from pathlib import Path; p=Path(sys.argv[1]); assert 'Development' in p.read_text(); p.write_text(p.read_text().replace('Development','Next month'))"]
        with patch("invoice_cli.cli.editor_command", return_value=command), patch("invoice_cli.cli.Paths.resolve", return_value=Paths(self.config.path, self.store.path, self.root / "state", self.root / "cache")):
            out, _ = self.cli("repeat", "--customer", "acme")
        self.assertIn("Copied 000001 to draft 000002", out)
        self.assertEqual(self.app.invoice(2)["lines"][0]["description"], "Next month")
        self.assertEqual(self.app.invoice(1), source)

    def test_pdf_paths_and_no_clobber(self):
        inv = self.invoice(posted=True)
        output = render(inv, str(self.root / "exports") + "/")
        self.assertEqual(output.name, f"invoice-acme-000001-{inv['posted_at'][:10]}.pdf")
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        original = output.read_bytes()
        with self.assertRaises(Error):
            render(inv, output)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(render(inv, self.root / "custom.pdf").name, "custom.pdf")
        render(inv, output, overwrite=True)
        with self.assertRaises(Error):
            destination(inv, "missing-directory-without-slash")

    def test_pdf_filename_uses_posted_date(self):
        inv = self.invoice()
        self.assertEqual(destination(inv).name, "invoice-acme-000001-draft.pdf")
        inv["posted_at"] = "2027-03-04T12:34:56+00:00"
        inv["status"] = "posted"
        self.assertEqual(destination(inv).name, "invoice-acme-000001-2027-03-04.pdf")
        inv["status"] = "void"
        self.assertEqual(destination(inv).name, "invoice-acme-000001-2027-03-04.pdf")

    def test_pdf_values_unicode_and_multipage(self):
        from pypdf import PdfReader
        inv = self.invoice()
        inv["lines"][0]["description"] = "Développement – café, £ and €"
        inv["lines"] *= 40
        inv["totals"] = self.app.totals(inv["lines"])
        output = render(inv, self.root / "long.pdf")
        reader = PdfReader(output)
        self.assertGreater(len(reader.pages), 2)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("Développement", text)
        self.assertIn("184,800.00", text)
        for page in reader.pages:
            self.assertIn("Description", page.extract_text())

    def test_pdf_giant_description_can_split(self):
        inv = self.invoice()
        inv["lines"][0]["description"] = "Long description " * 900
        self.assertTrue(render(inv, self.root / "giant.pdf").exists())

    def test_backup_is_consistent_and_no_overwrite(self):
        inv = self.invoice(posted=True)
        backup = self.root / "backup.sqlite3"
        self.cli("backup", str(backup))
        other = Store(backup)
        try:
            self.assertEqual(App(other, self.config).invoice(inv["id"]), inv)
        finally:
            other.close()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("backup", str(backup))

    def test_xdg_paths(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "relative", "XDG_STATE_HOME": str(self.root / "statebase"), "XDG_CACHE_HOME": ""}, clear=True):
            paths = Paths.resolve()
            self.assertEqual(paths.config, self.root / ".config/invoice/config.toml")
            self.assertEqual(paths.db, self.root / ".local/share/invoice/invoices.sqlite3")
            self.assertEqual(paths.state, self.root / "statebase/invoice")
            self.assertEqual(paths.cache, self.root / ".cache/invoice")
            explicit = Paths.resolve("custom.db", "custom.toml")
            self.assertEqual(explicit.db, Path.cwd() / "custom.db")
            self.assertEqual(explicit.config, Path.cwd() / "custom.toml")

    def test_xdg_absolute_config_and_data(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "cfg"), "XDG_DATA_HOME": str(self.root / "data")}):
            paths = Paths.resolve()
            self.assertEqual(paths.db, self.root / "data/invoice/invoices.sqlite3")
            self.assertEqual(paths.config, self.root / "cfg/invoice/config.toml")

    def test_private_config_and_database_permissions(self):
        self.assertEqual(self.config.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
