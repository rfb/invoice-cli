"""Command-line entry point. Commands also work with isolated --db/--config paths."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .core import App, TREATMENTS, money, public_invoice
from .editor import edit, editor_command, import_csv
from .presentation import show
from .storage import Config, Error, Paths, Store


def parser():
    p = argparse.ArgumentParser(prog="invoice", description="Local invoices, SQLite storage, CSV editing, PDF exports.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", help="Override XDG database path")
    p.add_argument("--config", help="Override XDG configuration path")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize storage and configure the next invoice ID")
    init.add_argument("--next-id", type=int)
    config = commands.add_parser("config", help="Business configuration and resolved paths").add_subparsers(dest="action", required=True)
    config.add_parser("paths")
    config.add_parser("show")
    setting = config.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value", nargs="?")
    setting.add_argument("--address-line", action="append", help="Business postal address line; repeat for each line")
    seq = commands.add_parser("sequence").add_subparsers(dest="action", required=True)
    seq.add_parser("show")
    setter = seq.add_parser("set-next")
    setter.add_argument("value", type=int)
    customers = commands.add_parser("customer").add_subparsers(dest="action", required=True)
    for action in ("add", "show", "update", "list"):
        c = customers.add_parser(action)
        if action in {"show", "update"}:
            c.add_argument("slug")
        if action == "add":
            c.add_argument("--slug")
        if action in {"add", "update"}:
            c.add_argument("--name")
            address = c.add_mutually_exclusive_group()
            address.add_argument("--address", help="Postal address; may contain literal newlines")
            address.add_argument("--address-line", action="append", help="Postal address line; repeat for each line")
            c.add_argument("--email")
    taxes = commands.add_parser("tax").add_subparsers(dest="action", required=True)
    tax = taxes.add_parser("add")
    tax.add_argument("code")
    tax.add_argument("--rate", required=True)
    tax.add_argument("--label")
    tax.add_argument("--effective", default="0001-01-01", help="Effective date, YYYY-MM-DD")
    tax_update = taxes.add_parser("update", help="Edit an existing tax definition")
    tax_update.add_argument("code")
    tax_update.add_argument("--effective", help="Select the existing effective date (required if multiple definitions exist)")
    tax_update.add_argument("--new-effective", help="Change the effective date, YYYY-MM-DD")
    tax_update.add_argument("--rate")
    tax_update.add_argument("--label")
    tax_snapshots = taxes.add_parser("snapshots", help="List invoice snapshots using a tax code")
    tax_snapshots.add_argument("code")
    taxes.add_parser("list")
    new = commands.add_parser("new")
    new.add_argument("--customer", required=True)
    new.add_argument("--issue")
    new.add_argument("--due")
    repeat = commands.add_parser("repeat", help="Repeat a customer's latest posted invoice and edit its lines")
    repeat.add_argument("--customer", required=True)
    items = commands.add_parser("item").add_subparsers(dest="action", required=True)
    for action in ("add", "edit", "remove", "list"):
        item = items.add_parser(action)
        item.add_argument("id")
        if action in {"edit", "remove"}:
            item.add_argument("position", type=int)
        if action == "add":
            item.add_argument("description")
        if action == "edit":
            item.add_argument("--description")
        if action in {"add", "edit"}:
            amount = item.add_mutually_exclusive_group()
            amount.add_argument("--hours")
            amount.add_argument("--amount")
            amount.add_argument("--quantity")
            item.add_argument("--rate")
            item.add_argument("--unit-price")
            item.add_argument("--unit")
            item.add_argument("--tax")
    editor = commands.add_parser("edit", help="Edit line items in VISUAL/EDITOR, or import a recovery CSV")
    editor.add_argument("id")
    editor.add_argument("--from", dest="from_file", help="Explicitly import a CSV without launching an editor")
    update = commands.add_parser("update")
    update.add_argument("id")
    update.add_argument("--due")
    update.add_argument("--issue")
    update.add_argument("--note")
    update.add_argument("--refresh-details", action="store_true", help="Copy current business/customer details into draft")
    update.add_argument("--refresh-tax", action="store_true", help="Resolve all draft rates again on issue date")
    display = commands.add_parser("show")
    display.add_argument("id")
    display.add_argument("--json", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--customer")
    listing.add_argument("--status", choices=["draft", "posted", "void"])
    listing.add_argument("--json", action="store_true")
    post = commands.add_parser("post")
    post.add_argument("id")
    pdf = commands.add_parser("pdf")
    pdf.add_argument("id")
    pdf.add_argument("--output")
    pdf.add_argument("--overwrite", action="store_true")
    void = commands.add_parser("void")
    void.add_argument("id")
    void.add_argument("--reason", required=True)
    delete = commands.add_parser("delete")
    delete.add_argument("id")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup (configuration is separate)")
    backup.add_argument("output")
    return p


def required(value, label):
    if value is not None and value.strip():
        return value
    if not sys.stdin.isatty():
        raise Error(f"Missing {label}; supply its flag in noninteractive mode")
    answer = input(label + ": ").strip()
    if not answer:
        raise Error(label + " is required")
    return answer


def line_values(args, *, adding=False):
    values = {"description": args.description, "tax": args.tax, "unit": args.unit,
              "quantity": args.quantity, "unit_price": args.unit_price}
    if args.rate is not None and args.unit_price is not None:
        raise Error("Use either --rate or --unit-price, not both")
    if args.amount is not None:
        if args.rate is not None or args.unit_price is not None:
            raise Error("--amount cannot be combined with --rate or --unit-price")
        values.update(quantity="1", unit=args.unit or "each", unit_price=args.amount)
    elif args.hours is not None:
        if args.unit is not None and args.unit != "hour":
            raise Error("--hours uses unit hour; use --quantity for other units")
        values.update(quantity=args.hours, unit="hour", unit_price=args.rate if args.rate is not None else args.unit_price)
    elif args.rate is not None:
        values["unit_price"] = args.rate
    if adding:
        values["quantity"] = values["quantity"] if values["quantity"] is not None else "1"
        values["unit"] = values["unit"] or "each"
        if values["unit_price"] is None:
            raise Error("Supply --amount, --hours with --rate, or --quantity with --unit-price")
    return values


def show_tax_snapshots(app, code):
    rows = app.db.execute(
        "SELECT DISTINCT i.id,i.status,i.issue_date,l.tax_label,l.tax_rate "
        "FROM invoices i JOIN line_items l ON l.invoice_id=i.id "
        "WHERE l.tax=? ORDER BY i.id,l.tax_rate,l.tax_label", (code,)).fetchall()
    if not rows:
        print(f"No invoice snapshots use {code}.")
        return
    print(f"Invoice snapshots using {code}:")
    drafts = set()
    for row in rows:
        try:
            label, rate = app.tax(code, row["issue_date"])
            current = f"{rate}% {label}"
            changed = (Decimal(rate), label) != (Decimal(row["tax_rate"]), row["tax_label"])
        except Error:
            current = "unavailable on issue date"
            changed = False
        print(f"  {row['id']:06d}  {row['status']:<6}  {row['issue_date']}  "
              f"saved: {row['tax_rate']}% {row['tax_label']}  applicable now: {current}")
        if row["status"] == "draft" and changed:
            drafts.add(row["id"])
    if drafts:
        print("To apply current rates and labels to drafts (refreshes all taxes on each invoice):")
        for ref in sorted(drafts):
            print(f"  invoice update {ref:06d} --refresh-tax")
    if any(row["status"] != "draft" for row in rows):
        print("Posted and void invoice snapshots are fixed and cannot be refreshed.")


def dispatch(args):
    paths = Paths.resolve(args.db, args.config)
    config = Config(paths.config)
    if args.command == "config":
        if args.action == "paths":
            print(f"config: {paths.config}\ndatabase: {paths.db}\nstate: {paths.state}\ncache: {paths.cache}\npdf output: current working directory ({Path.cwd()})")
        elif args.action == "show":
            print(json.dumps(config.values, ensure_ascii=False, indent=2))
        else:
            if args.address_line is not None:
                if args.key != "business-address" or args.value is not None:
                    raise Error("Use --address-line only with config set business-address, without a value argument")
                args.value = "\n".join(args.address_line)
            if args.value is None:
                raise Error("Supply a setting value (or --address-line for business-address)")
            config.set(args.key, args.value)
            print(f"Set {args.key}")
        return
    store = Store(paths.db, initialize=args.command == "init")
    app = App(store, config)
    try:
        if args.command == "init":
            if args.next_id is not None:
                store.set_next(args.next_id)
            if not paths.config.exists():
                config.save()
            print(f"Ready. Next invoice: {store.next_id():06d}")
        elif args.command == "sequence":
            if args.action == "set-next":
                store.set_next(args.value)
            print(f"Next invoice: {store.next_id():06d}")
        elif args.command == "customer":
            if args.action in {"add", "update"} and args.address_line is not None:
                args.address = "\n".join(args.address_line)
            if args.action == "add":
                slug = required(args.slug, "slug")
                name = required(args.name, "name")
                address = args.address or ""
                email = args.email or ""
                if not address.strip() and not email.strip() and sys.stdin.isatty():
                    email = input("email (optional if postal address supplied): ").strip()
                    if not email:
                        print("Postal address (one line at a time; blank line to finish):")
                        lines = []
                        while True:
                            line = input()
                            if not line.strip():
                                break
                            lines.append(line)
                        address = "\n".join(lines)
                customer = app.add_customer(slug, name, address, email)
                print(f"Added {customer['slug']}: {customer['name']}")
            elif args.action == "update":
                app.update_customer(args.slug, name=args.name, address=args.address, email=args.email)
                print(f"Updated {args.slug}; existing invoice snapshots unchanged")
            elif args.action == "show":
                print(json.dumps(app.customer(args.slug), ensure_ascii=False, indent=2))
            else:
                for row in app.db.execute("SELECT slug,name FROM customers ORDER BY slug"):
                    print(f"{row['slug']:<24} {row['name']}")
        elif args.command == "tax":
            if args.action == "add":
                app.add_tax(args.code, args.rate, args.label, args.effective)
                print(f"Added {args.code} {args.rate}% effective {args.effective}; existing drafts unchanged")
            elif args.action == "update":
                app.update_tax(args.code, effective=args.effective, new_effective=args.new_effective,
                               rate=args.rate, label=args.label)
                print(f"Updated {args.code}; existing invoice snapshots unchanged")
                show_tax_snapshots(app, args.code)
            elif args.action == "snapshots":
                show_tax_snapshots(app, args.code)
            else:
                for row in app.db.execute("SELECT * FROM tax_rates ORDER BY code,effective_from"):
                    print(f"{row['code']:<16} {row['rate']:>10}%  {row['effective_from']}  {row['label']}")
                for code, label in TREATMENTS.items():
                    print(f"{code:<16}          0%  built-in    {label}")
        elif args.command == "new":
            inv = app.create(args.customer, args.issue, args.due)
            print(f"Created draft invoice {inv['id']:06d}")
        elif args.command == "repeat":
            command = editor_command()  # fail before creating a draft if no editor/TTY
            source = app.latest(args.customer)
            inv = app.create(args.customer, source=source)
            print(f"Copied {source['id']:06d} to draft {inv['id']:06d}", flush=True)
            for old, new in zip(source["lines"], inv["lines"]):
                if old["tax_rate"] != new["tax_rate"]:
                    print(f"Tax changed for {new['description']}: {old['tax_rate']}% -> {new['tax_rate']}%", flush=True)
            print(show(edit(app, inv["id"], paths.state, command)))
        elif args.command == "item":
            if args.action == "add":
                app.add_line(args.id, line_values(args, adding=True))
            elif args.action == "edit":
                app.edit_line(args.id, args.position, line_values(args))
            elif args.action == "remove":
                app.remove_line(args.id, args.position)
            print(show(app.invoice(args.id)))
        elif args.command == "edit":
            if args.from_file:
                inv = app.draft(args.id)
                lines = import_csv(app, inv, Path(args.from_file).read_text(encoding="utf-8"))
                app.replace_lines(args.id, lines, inv["revision"])
                print(show(app.invoice(args.id)))
            else:
                print(show(edit(app, args.id, paths.state)))
        elif args.command == "update":
            app.update(args.id, due=args.due, issue=args.issue, note=args.note,
                       refresh=args.refresh_details, refresh_tax=args.refresh_tax)
            print(show(app.invoice(args.id)))
        elif args.command == "show":
            inv = app.invoice(args.id)
            print(json.dumps(public_invoice(inv), ensure_ascii=False, indent=2) if args.json else show(inv))
        elif args.command == "list":
            invoices = app.list_invoices(args.customer, args.status)
            if args.json:
                print(json.dumps([public_invoice(i) for i in invoices], ensure_ascii=False, indent=2))
            else:
                print(f"{'ID':<12} {'Status':<8} {'Issued':<12} {'Customer':<24} {'Total':>18}")
                for inv in invoices:
                    print(f"{inv['id']:06d}       {inv['status']:<8} {inv['issue_date']:<12} {inv['customer']['slug']:<24} {money(inv['totals']['total_cents']):>14} {inv['currency']}")
        elif args.command == "post":
            inv = app.post(args.id)
            print(f"Posted invoice {inv['id']:06d}. Export: invoice pdf {inv['id']}")
        elif args.command == "pdf":
            from .pdf import render
            print(render(app.invoice(args.id), args.output, args.overwrite))
        elif args.command == "void":
            app.void(args.id, args.reason)
            print(f"Voided invoice {app.invoice(args.id)['id']:06d}; original content retained")
        elif args.command == "delete":
            inv = app.draft(args.id)
            app.delete(args.id)
            print(f"Deleted draft {inv['id']:06d}; its ID will not be reused")
        elif args.command == "backup":
            from .storage import private_dir
            import os
            import tempfile
            output = Path(args.output).expanduser().absolute()
            if output == paths.db.absolute():
                raise Error("Backup destination must differ from the live database")
            private_dir(output.parent)
            fd, name = tempfile.mkstemp(prefix=".invoice-backup-", dir=output.parent)
            os.close(fd)
            try:
                target = sqlite3.connect(name)
                try:
                    app.db.backup(target)
                finally:
                    target.close()
                try:
                    os.link(name, output)
                except FileExistsError:
                    raise Error("Backup destination already exists; choose a new path") from None
            finally:
                Path(name).unlink(missing_ok=True)
            print(f"Database backup: {output}\nAlso back up configuration: {paths.config}")
    finally:
        store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        dispatch(args)
    except (Error, OSError, sqlite3.Error, ValueError, OverflowError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Canceled", file=sys.stderr)
        raise SystemExit(130) from None
