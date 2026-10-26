"""Comment-aware CSV editing, with durable recovery and optimistic concurrency."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from .core import COLUMNS
from .storage import Error, private_dir


def export_csv(app, invoice):
    help_lines = [
        f"Invoice {invoice['id']:06d}: edit line items; add/delete/reorder rows.",
        "description: required text. Quote values containing commas or newlines.",
        "quantity: positive decimal, up to 6 decimal places.",
        "unit: required descriptive label (max 40 characters); hour, each, or your own label.",
        "unit_price: nonnegative decimal before tax, up to 6 decimal places.",
        "tax: one of the configured codes or treatments listed below; required.",
        "Hourly: quantity=hours, unit=hour, unit_price=hourly rate.",
        "Fixed-price: quantity=1, unit=each, unit_price=fixed amount.",
        "Available taxes on " + invoice["issue_date"] + ":",
        *app.tax_options(invoice["issue_date"]),
        "Existing lines retain reviewed rates unless you explicitly refresh taxes.",
        "CSV does not contain totals or invoice metadata.",
        "Lines starting with # outside quoted fields are comments.",
        "Quote a description beginning with #. Multiline quoted descriptions are supported.",
    ]
    try:
        code = app.config.values["default-tax"]
        app.tax(code, invoice["issue_date"])
    except Error:
        code = "none"
    help_lines += [f"Hourly example: Software development,24,hour,150.00,{code}",
                   f"Fixed example: Website deployment,1,each,800.00,{code}"]
    out = io.StringIO(newline="")
    for line in help_lines:
        # Configured labels must not inject active CSV records into the guide.
        for part in str(line).splitlines():
            out.write("# " + part + "\n")
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow(COLUMNS)
    for item in invoice["lines"]:
        writer.writerow([item[k] for k in COLUMNS])
    return out.getvalue()


def comment_aware_lines(text):
    """Replace comments with blank lines; never filter inside quoted fields.

    Track CSV field starts so a quote in unquoted text does not make the rest of
    the file look quoted. csv.reader remains the authoritative syntax validator.
    Preserving blank physical lines keeps reader.line_num useful for errors.
    """
    quoted = False
    field_start = True
    for line in io.StringIO(text, newline=""):
        if not quoted and line.startswith("#"):
            yield "\n"
            continue
        yield line
        i = 0
        while i < len(line):
            char = line[i]
            if quoted:
                if char == '"':
                    if i + 1 < len(line) and line[i + 1] == '"':
                        i += 1
                    else:
                        quoted = False
            elif char == '"' and field_start:
                quoted = True
                field_start = False
            elif char == "," or char in "\r\n":
                field_start = True
            else:
                field_start = False
            i += 1


def import_csv(app, invoice, text):
    reader = csv.reader(comment_aware_lines(text), strict=True)
    result = []
    header = False
    try:
        for row in reader:
            if not row:
                continue
            if not header:
                if row != COLUMNS:
                    raise Error(f"Line {reader.line_num}: headers must be {','.join(COLUMNS)}")
                header = True
                continue
            if len(row) != len(COLUMNS):
                raise Error(f"Line {reader.line_num}: expected 5 columns, got {len(row)}")
            values = dict(zip(COLUMNS, row))
            matches = [x for x in invoice["lines"] if x["tax"] == values["tax"]]
            exact = [x for x in matches if all(str(x[k]) == values[k] for k in COLUMNS)]
            described = [x for x in matches if x["description"] == values["description"] and x["unit"] == values["unit"]]
            candidates = exact or described or matches
            rates = {(x["tax_label"], x["tax_rate"]) for x in candidates}
            if len(rates) > 1:
                raise Error(f"Line {reader.line_num}, tax: ambiguous historical rates; refresh draft taxes before editing")
            previous = candidates[0] if candidates else None
            try:
                result.append(app.line(values, invoice["issue_date"], previous))
            except Error as error:
                raise Error(f"Line {reader.line_num}, {error}") from None
    except csv.Error as error:
        raise Error(f"Line {reader.line_num}: invalid CSV: {error}") from None
    if not header:
        raise Error("CSV header row is missing")
    app.totals(result)
    return result


def editor_command():
    if not sys.stdin.isatty():
        raise Error("Editor requires an interactive terminal. Use item commands for scripts.")
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not command:
        raise Error("Set VISUAL or EDITOR to your editor command (for example: export EDITOR='nano')")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise Error(f"Invalid editor command: {error}") from None
    if not argv or not shutil.which(argv[0]):
        raise Error("Editor executable not found; check VISUAL/EDITOR")
    return argv


def edit(app, ref, state: Path, command=None):
    invoice = app.draft(ref)
    argv = command if command is not None else editor_command()
    private_dir(state / "edits")
    fd, name = tempfile.mkstemp(prefix=f"{invoice['id']:06d}-", suffix=".csv", dir=state / "edits")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
        target.write(export_csv(app, invoice))
    while True:
        try:
            completed = subprocess.run([*argv, str(path)], check=False)
            if completed.returncode != 0:
                raise Error(f"Editor exited with status {completed.returncode}")
            text = path.read_text(encoding="utf-8")
            lines = import_csv(app, invoice, text)
            app.replace_lines(ref, lines, invoice["revision"])
        except (Error, OSError, UnicodeError, KeyboardInterrupt) as error:
            print(f"Invoice {invoice['id']:06d} remains a draft. Edited CSV retained: {path}", file=sys.stderr)
            # Concurrency errors must not retry against a stale snapshot.
            if isinstance(error, Error) and "Invoice changed" not in str(error) and sys.stdin.isatty():
                print(str(error), file=sys.stderr)
                try:
                    retry = input("Reopen editor? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    retry = "n"
                if retry == "y":
                    continue
            if isinstance(error, KeyboardInterrupt):
                raise Error("Editing canceled; saved draft was not changed") from None
            raise Error(f"{error}. CSV recovery: {path}") from None
        else:
            path.unlink(missing_ok=True)
            return app.invoice(ref)
