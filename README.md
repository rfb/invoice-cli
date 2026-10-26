# Invoice CLI

A local invoicing tool for hourly and fixed-price work. SQLite stores your invoices; a command-line interface and a commented CSV editor handle line items; ReportLab produces PDFs. No server, accounts, browser, or network access is needed at runtime.

Requires Python 3.11 or later. ReportLab is the only runtime dependency. The CLI uses Python's standard-library argparse rather than Typer/Rich to keep installation small. Tests use unittest, with pypdf for PDF verification.

## Install

From this directory:

```sh
pipx install .
# Or use a virtual environment:
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/invoice --help
```

`pipx` makes the `invoice` command available on your PATH. Source installs require setuptools >=68. A built wheel is also included in `dist/` and can be installed directly with pipx or pip.

## First setup

Use the next number after your last GnuCash invoice. If that was `001233`:

```sh
invoice init --next-id 1234
invoice config set business-name "Example Consulting"
invoice config set business-address "123 Example Road, BC, Canada"
invoice config set business-email "billing@example.com"
invoice config set gst-number "123456789RT0001"
invoice config set currency CAD
invoice config set payment-terms 30
invoice config set default-note "Payment by e-transfer to billing@example.com. Thank you."
invoice tax add GST --rate 5 --label GST
invoice config set default-tax GST

invoice customer add --slug acme --name "Acme Ltd." \
  --address "456 Client Road, BC, Canada" --email "accounts@example.com"
```

These are example business details and a sample tax configuration. Replace them with yours. Rates are configurable; the tool does not determine tax eligibility or jurisdiction. Tax is not configured automatically.

`customer add` prompts for missing slug, name, or address when run in a terminal. In scripts, provide all three flags. Multiline addresses/notes may be supplied as a single quoted argument containing newlines. `invoice config show` displays the available flat settings and their current values.

## Everyday workflow

```sh
invoice new --customer acme
# Created draft invoice 001234

invoice item add 1234 "Software development" --hours 24 --rate 150
invoice item add 1234 "Website deployment" --amount 800
invoice show 1234
# Subtotal: 4,400.00 CAD
# GST (5%): 220.00
# TOTAL: 4,620.00 CAD

export EDITOR='nano'  # Or: export VISUAL='code --wait'
invoice edit 1234
invoice update 1234 --note "Payment due under our service agreement."
invoice pdf 1234        # Draft preview: ./001234-acme.pdf
invoice post 1234       # Finalize the same ID; does not generate a PDF
invoice pdf 1234 --overwrite
```

IDs are integers. `1234` and `001234` refer to the same invoice. Leading zeroes are display formatting only. IDs start when the draft is created, remain unchanged on posting, never reset annually, and are not reused when drafts are deleted. Gaps are expected. Six digits is a minimum width; IDs beyond 999999 expand without wrapping.

```sh
invoice sequence show
invoice sequence set-next 1500  # Next draft will be 001500; forward only
```

To start the next billing cycle from a customer's most recent invoice:

```sh
invoice repeat --customer acme
# Copies the latest posted, non-void invoice into a new draft, then opens EDITOR.
```

"Latest" means most recently posted, with invoice ID breaking timestamp ties. The shortcut copies descriptions, quantities, units, prices, line order, currency, and tax treatments. It uses today's local issue date, current payment terms, current business/customer details, current default note, and tax rates applicable to the new date. Changed rates are reported before editing. The original invoice stays unchanged. There is no automatic posting or PDF generation.

An unavailable source, missing tax rate, unavailable editor, or noninteractive invocation fails before creating a draft. If the editor fails after creation, the copied draft remains available with its new ID.

## Line items and CSV editing

```sh
invoice item edit 1234 1 --hours 25
invoice item remove 1234 2
invoice item list 1234
# Generic quantities/units:
invoice item add 1234 "On-site work" --quantity 2 --unit day --unit-price 900
# Override the configured default tax:
invoice item add 1234 "Other service" --amount 200 --tax exempt
```

`invoice edit 1234` opens a private UTF-8 CSV file with a commented guide explaining all columns, hourly/fixed conventions, examples, and every configured tax code and its applicable rate. VISUAL takes precedence over EDITOR; editor command arguments are supported without shell execution. Editors that detach must use a wait flag.

```csv
# Guide omitted here; the editor contains it automatically.
"description","quantity","unit","unit_price","tax"
"Software development","24","hour","150.00","GST"
"Website deployment","1","each","800.00","GST"
```

Add/delete/reorder rows naturally. Hourly work is quantity × rate with unit `hour`; fixed-price work uses quantity 1 and unit `each`. Unit is a label, not an enum. Tax is required on every row. Amounts/totals and invoice metadata are not editable in this file.

CSV quoting supports commas, quotes, and multiline descriptions. A line starting with `#` in column one is a comment only outside a quoted field. Quote descriptions beginning with `#`; the exporter quotes all fields automatically. `#` lines inside a quoted multiline description remain data. Empty lines outside quoted fields are ignored. Header names/order must match exactly.

All rows are validated before a single transaction replaces the items. Error line numbers match the editor's physical file lines. A concurrent invoice change causes a conflict instead of overwriting newer data. An invalid file may be reopened; cancellation, failures, and conflicts preserve the edited CSV under the XDG state directory and print its recovery path. Successful imports remove the temporary CSV.

For an explicitly reviewed recovery file, or scripted CSV input:

```sh
invoice edit 1234 --from /path/to/recovered.csv
```

This imports against the draft's current revision and leaves the source file intact. Review concurrent changes before importing a recovered file. A nonzero editor exit is cancellation/failure; exiting successfully with an unchanged file simply keeps the copied lines.

## Taxes and rounding

Each line has one tax code or built-in treatment:

| Treatment | Meaning |
| --- | --- |
| Configured code, e.g. `GST` | Named percentage tax applicable on issue date |
| `exempt` | Exempt treatment, zero calculated tax |
| `zero-rated` | Zero-rated treatment, zero calculated tax |
| `none` | No tax applied |

```sh
invoice tax list
invoice tax add GST --rate 6 --effective 2030-01-01 --label GST
```

The future rate above is illustrative, not a predicted rate change. Codes and their effective dates are unique; existing definitions are not overwritten. The rate with the latest effective date on or before the issue date is selected. You can configure different codes on different lines; stacking or compounding taxes on one line is not implemented.

Prices are before tax. Quantity and unit price allow up to six decimal places; quantity must be positive, price nonnegative. Nonfinite values and negative amounts are rejected. Decimal arithmetic rounds each line's net amount to cents, then its tax to cents using ROUND_HALF_UP; invoice totals sum those rounded values. This is the app's explicit rounding policy, not a claim about a universal tax rule. Supported currencies are CAD, USD, EUR, GBP, AUD, NZD, and CHF, all with two decimal places. No exchange conversion.

Drafts snapshot line rates when entered. Normal line edits retain those rates; configuration changes do not silently change a reviewed draft. When importing CSV, existing codes retain their reviewed rates; genuinely new codes resolve against the issue date. If an unusual draft contains ambiguous historical rates for one code, refresh its taxes before importing edited lines.

```sh
invoice update 1234 --refresh-tax
invoice update 1234 --issue 2026-10-26 --due 2026-11-25
```

Changing the issue date recalculates applicable rates and displays all totals. An explicit tax refresh does the same without changing dates. The GST registration field is required to post lines using the `GST` code. Codes/treatments must be chosen by the user; no automated tax/legal classification is performed.

## Drafts, posting, and corrections

New drafts copy your business/customer details and the current default note. Changes to the defaults or customer record do not rewrite existing drafts. To intentionally refresh a draft's business/customer details:

```sh
invoice customer update acme --address "New billing address"
invoice update 1234 --refresh-details
invoice update 1234 --note ""  # Clear this invoice's note
```

Posting requires business and customer names/addresses, valid dates, at least one line, and applicable tax setup. It freezes the financial content and finalized totals. CLI and PDF use the same snapshots; changing configuration, customers, or tax definitions cannot alter an issued invoice. Posting twice is safe and never changes its ID or allocates a new one.

```sh
invoice list --customer acme
invoice list --status draft
invoice show 1234 --json
invoice list --json
invoice delete 1235                       # Drafts only
invoice void 1234 --reason "Issued in error"
```

Voiding retains the original number, items, and totals with a reason/date. It is an administrative status, not a credit note or tax adjustment. Posted/void invoices cannot be deleted or financially edited. Issue a new invoice for corrected content as appropriate to your process; full credit-note accounting is outside this version.

## PDF output

```sh
invoice pdf 1234                         # ./001234-acme.pdf
invoice pdf 1234 --output ./exports/     # ./exports/001234-acme.pdf
invoice pdf 1234 --output ./custom.pdf   # Explicit filename override
```

Existing directories receive the generated filename. End a new directory path with `/`; otherwise the destination must be a `.pdf` file. No files are silently overwritten, even when regenerating the same invoice: use `--overwrite`. Publication is atomic. The printed path tells you where the output went.

Drafts and voids are visibly marked on all pages. Tables repeat headers and wrap/split long descriptions. Totals stay together. The bundled DejaVu Sans fonts support common accented names and punctuation; they do not cover every writing system. Retain the originally issued PDF: financial content is stable, but regenerated PDF bytes/layout are not a long-term archival guarantee across software versions.

## Storage and backups

| Content | Default path when XDG variable is unset/empty |
| --- | --- |
| `$XDG_CONFIG_HOME/invoice/config.toml` | `~/.config/invoice/config.toml` |
| `$XDG_DATA_HOME/invoice/invoices.sqlite3` | `~/.local/share/invoice/invoices.sqlite3` |
| `$XDG_STATE_HOME/invoice/edits/` | `~/.local/state/invoice/edits/` |
| `$XDG_CACHE_HOME/invoice/` (reserved, currently unused) | `~/.cache/invoice/` |

Relative XDG environment values are ignored in favor of standard fallbacks. Generated PDFs default to the working directory, not XDG storage. No automatic PDF archive copy is created. Recovery CSVs are persistent state, not disposable cache. App-created directories use private permissions and sensitive files use mode 0600 on POSIX.

```sh
invoice config paths
invoice --db ./test.sqlite3 --config ./test.toml init
invoice backup ./backups/2026-10-26.sqlite3
```

Global `--db` and `--config` options must precede the command. Explicit paths may be relative to the working directory. Overriding one path does not relocate any others. Tests isolate all locations and never touch your real files.

The backup command uses SQLite's online backup API and refuses to overwrite an existing file. Also back up `config.toml`, issued PDFs, and any recovery files you care about. Alternatively, stop all invoice commands before copying the database. Never copy just an actively written SQLite database file as a general backup strategy, especially if journal mode is changed externally.

To restore, stop invoice commands, retain a copy of current files, place the backup at the configured database path, and restore the matching configuration separately. You can inspect a backup without replacing the live database using `invoice --db ./backup.sqlite3 show 1234`.

## Development and validation

```sh
python3 -m pip install -e '.[test]'
python3 -m unittest discover -s tests -v
python3 tools/make_examples.py
```

Tests cover decimal rounding, tax effective dates, mixed treatments, frozen snapshots, ID allocation/concurrency/no reuse, note defaults, comment-aware CSV, recovery/conflict behavior, repeat selection/editor invocation, JSON, XDG paths, online backups, and PDF extraction/multipage/long-row behavior.

`examples/001234-acme.pdf` and `examples/001235-acme.pdf` are synthetic short and multipage invoices. The implementation plan is in `docs/invoice-cli-plan.md`.

Not included: bookkeeping, payment tracking, bank feeds, recurring jobs, time tracking, email delivery, credit notes, tax returns, or jurisdictional tax automation.
# invoice-cli
