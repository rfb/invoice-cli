"""Generate synthetic invoices; never access the user's XDG locations."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice_cli.core import App
from invoice_cli.pdf import render
from invoice_cli.storage import Config, Store


def main():
    output = Path(__file__).resolve().parents[1] / "examples"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = Config(root / "config.toml")
        config.values.update({"business-name": "Example Consulting", "business-address": "123 Example Road\nBritish Columbia, Canada",
                              "business-email": "billing@example.com", "gst-number": "123456789RT0001", "default-tax": "GST",
                              "default-note": "Payment due within 30 days. Please reference the invoice number.\nThank you for your business."})
        store = Store(root / "example.sqlite3", initialize=True)
        try:
            store.set_next(1234)
            app = App(store, config)
            app.add_tax("GST", "5")
            app.add_customer("acme", "Acme Ltd.", "456 Client Road\nBritish Columbia, Canada", "accounts@example.com")
            inv = app.create("acme", "2026-10-26")
            app.add_line(inv["id"], dict(description="Software development", quantity="24", unit="hour", unit_price="150"))
            app.add_line(inv["id"], dict(description="Website deployment", quantity="1", unit="each", unit_price="800"))
            print(render(app.post(inv["id"]), output, overwrite=True))
            long = app.create("acme", "2026-10-26")
            lines = []
            for n in range(1, 66):
                lines.append(app.line(dict(description=f"Work package {n}: Développement, review, and reliability improvements. Detailed implementation and validation of the requested changes.",
                                           quantity="1.25", unit="hour", unit_price="150", tax="GST" if n % 3 else "exempt"), long["issue_date"]))
            app.replace_lines(long["id"], lines, long["revision"])
            print(render(app.post(long["id"]), output, overwrite=True))
        finally:
            store.close()


if __name__ == "__main__":
    main()
