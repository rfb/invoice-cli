# Invoice CLI: Codex implementation handoff

## Goal

Build a small local command-line invoicing tool to replace GnuCash for invoice creation only. Use SQLite for storage and generate PDFs. Prioritize a quick, understandable workflow over accounting features. This document is a plan; no implementation exists yet.

## Agreed requirements

- Manage customers and create invoices.
- Support hourly work and fixed-price services, including both on one invoice.
- Add, edit, remove, and reorder line items.
- Handle taxes, particularly GST, per line item with a configurable default.
- Display complete invoice details and calculated totals directly in the CLI.
- Edit line items only through a CSV file opened in the user's editor.
- Duplicate a customer's latest posted invoice into a new draft and immediately open its copied line items in the editor.
- Configure a default invoice note, copied to new drafts and overridable per invoice.
- Keep preview PDF generation separate from posting/finalizing.
- Invoice IDs auto-increment, display as six digits with leading zeroes, and have a configurable starting point. The ID is assigned on draft creation and remains unchanged after posting. Posted invoices have immutable financial content.
- Storage must conform to the XDG Base Directory Specification.
- Generated PDFs default to the working directory, with an explicit output path option. Generated filenames include the invoice identifier and customer slug.

## Implementation defaults

These are recommended choices, not additional user requirements. Proceed with these unless the existing repository dictates otherwise.

- Python, Typer for commands, Rich for terminal formatting, standard-library sqlite3 and csv, Decimal for arithmetic, ReportLab for PDF generation, pytest for meaningful tests.
- Installable package with a console entry point named `invoice` and a pyproject.toml. Keep dependencies small; no server or browser runtime.
- One local SQLite database, one TOML configuration file, ordinary PDF output files. Support explicit `--db` and `--config` paths; use the XDG locations specified below.
- Single business profile; CAD as default currency, one currency per invoice. No exchange conversion.
- Tax-exclusive prices. No compounded taxes or tax-inclusive pricing in v1.
- Dates use ISO YYYY-MM-DD; derive defaults from the local calendar date.
- Default payment terms: configurable, initially 30 days. Invoice IDs use six-digit zero-padded display, with a configurable next ID for migration from GnuCash; no annual reset or separate posting number.
- No automatic import from GnuCash in v1.

## XDG storage requirements

Use `invoice` as the application directory name. XDG layout is a requirement, not a platform-dependent implementation default.

| Content | Path | Fallback when the variable is unset or empty |
| --- | --- | --- |
| Configuration and business profile | `$XDG_CONFIG_HOME/invoice/config.toml` | `~/.config/invoice/config.toml` |
| SQLite database, including adjacent SQLite journal/WAL files | `$XDG_DATA_HOME/invoice/invoices.sqlite3` | `~/.local/share/invoice/invoices.sqlite3` |
| Recoverable CSV edits and optional operational logs | `$XDG_STATE_HOME/invoice/` | `~/.local/state/invoice/` |
| Optional disposable rendering cache | `$XDG_CACHE_HOME/invoice/` | `~/.cache/invoice/` |

- Ignore relative XDG environment values and use the corresponding fallback, as required by the specification. Explicit user-supplied CLI paths may be relative to the working directory.
- Explicit `--config`, `--db`, and PDF `--output` options override their corresponding default paths. These overrides do not relocate other directories implicitly.
- PDFs are user-requested exports, not internal XDG application data. Default to the current working directory; do not keep an automatic PDF copy under XDG_DATA_HOME.
- Do not write a database, config, or hidden application directory into the working directory or directly under the home directory by default.
- Treat configuration, data, and recoverable edits as persistent. Deleting caches must never destroy invoices, issued PDFs, configuration, or recoverable work.
- Create app directories lazily with private permissions; create sensitive files with restrictive permissions. Use unique private files for CSV editing/recovery and remove them after a successful import or explicit discard. Do not put recoverable edits in a disposable cache or runtime directory.
- No runtime directory is required in v1. Do not use XDG_RUNTIME_DIR for persistent files, and do not write to XDG_CONFIG_DIRS or XDG_DATA_DIRS. No system-wide defaults are required in v1.
- Include resolved storage paths in `invoice config paths` and document them in the README. No silent fallback to platform-native non-XDG paths.

## CLI contract

```sh
invoice init --next-id 1234               # Example: first new invoice is 001234
invoice sequence show                   # Show the next invoice ID
invoice sequence set-next 1500          # Explicit forward-only change
invoice config set default-note "Payment by e-transfer to billing@example.com. Thank you."
invoice config set default-tax GST
invoice config set gst-number "123456789RT0001"
invoice tax add GST --rate 5 --label "GST"

invoice customer add                     # Prompts for missing fields
invoice customer list
invoice customer show acme
invoice customer update acme --name "Acme Ltd."

invoice new --customer acme              # Returns next permanent ID (000042 in examples below)
invoice repeat --customer acme           # Copy latest posted invoice, then open line-item editor
invoice item add 000042 "Software development" --hours 24 --rate 150
invoice item add 000042 "Website deployment" --amount 800
invoice item add 000042 "Other service" --amount 200 --tax exempt
invoice item list 000042
invoice item edit 000042 1 --hours 25
invoice item remove 000042 2
invoice edit 000042                         # CSV editor, line items only

invoice update 000042 --due 2026-11-25
invoice update 000042 --note "Payment due under our service agreement."
invoice update 000042 --note ""             # Clear note
invoice show 000042
invoice show 000042 --json
invoice list
invoice list --customer acme
invoice list --status draft
invoice pdf 000042                         # Preview, visibly marked DRAFT
invoice post 000042                        # Freeze; keep the same ID
invoice show 000042
invoice pdf 000042                    # ./000042-acme.pdf
invoice pdf 000042 --output ./exports/ # ./exports/000042-acme.pdf
invoice pdf 000042 --output ./exports/000042-acme.pdf
invoice void 000042 --reason "Issued in error"
invoice delete 000042                      # Drafts only
```

All amounts and rates in examples are sample configuration, not a determination of the user's tax obligations. Do not hardcode GST rates. Use flags for scripting and prompts for missing required interactive values; never prompt when input is noninteractive. Fail with a useful error instead. JSON output must contain no decorative text and encode decimal values as strings.

`post` finalizes the invoice independently of rendering and never assigns or changes its ID. Print the six-digit ID and the command for generating its PDF. Accept either padded or unpadded decimal IDs in commands (42 and 000042 refer to the same invoice); display padded IDs consistently, including as strings in JSON. Reposting must be idempotent or report already posted without changing content.

## Repeat the last invoice for a customer

`invoice repeat --customer acme` is a one-command shortcut: find the customer's latest posted, non-void invoice, create a new draft with copied line items, and immediately launch the normal CSV editor (including generated help).

- Resolve the customer by its stable customer record, not a billing-name match. Define latest as most recently posted (posted timestamp descending, invoice ID descending as a deterministic tie-breaker), not latest issue date. Exclude drafts and void invoices. If none exists, return a clear error with guidance to use `invoice new`; do not create an empty draft silently.
- Show the source ID and new draft ID before opening the editor.
- Allocate a fresh permanent auto-incrementing ID. Use today's local issue date, current payment terms for the due date, current business/customer details, and the current default note. Copy the source currency and line descriptions, quantities, units, unit prices, order, and tax treatment/code. This is a new invoice, not a copy of historical dates, status, number, snapshots, totals, or payment history.
- Resolve copied tax codes using rates applicable to the new issue date, then recalculate amounts. Explicitly report changed tax rates. If a source tax code is unavailable for the new date, fail before creating a draft and identify the code; never silently substitute another treatment. The old invoice remains unchanged.
- Open the copied lines in the same editor and use the same validation/recovery workflow as `invoice edit`. The new draft is committed before launching the editor; cancellation, unchanged contents, or editor failure leaves the copied draft available and reports its ID. Failed imports never partially update it. Do not automatically post or generate a PDF.
- In noninteractive use, fail before creating a draft unless editor execution is explicitly supported in that invocation. Validate editor availability before creation where possible.
- Store an optional source_invoice_id for provenance. No need for a general duplication/template framework in v1.

## CSV editor

```csv
# Invoice 000042: edit line items below. Add/delete/reorder rows as needed.
# description: required text; quote values containing commas or newlines.
# quantity: positive decimal. Hourly = hours worked; fixed-price = 1.
# unit: descriptive label; hour for hourly work, each for fixed-price work.
# unit_price: nonnegative decimal, before tax; hourly rate or fixed amount.
# tax: use one of the available codes/treatments listed below.
# GST: configured GST rate applicable on this invoice's issue date.
# exempt: exempt treatment, zero tax.
# zero-rated: zero-rated treatment, zero tax.
# none: no tax applied; distinct from exempt and zero-rated.
# Hourly example: Software development,24,hour,150.00,GST
# Fixed-price example: Website deployment,1,each,800.00,GST
# Only active CSV rows become invoice items. Calculated totals are not editable.
description,quantity,unit,unit_price,tax
Software development,24,hour,150.00,GST
Website deployment,1,each,800.00,GST
"Hosting, October",1,each,45.00,GST
```

- Open a temporary UTF-8 CSV using VISUAL, then EDITOR; otherwise return guidance to configure an editor. Support editor commands with arguments without shell interpolation.
- Prepend a commented help block on every editor launch. Explain every header, valid values/constraints, hourly versus fixed-price conventions, and row operations. Show every configured tax code with its label and resolved percentage for the invoice issue date, plus exempt, zero-rated, and none. If a configured code has no applicable rate, label it unavailable rather than offering it as valid. Generate this list from actual configuration, not a hardcoded GST-only list. The sample help above is illustrative.
- There is no separate line-type column: explain that hourly lines use quantity=hours, unit=hour, unit_price=hourly rate; fixed-price lines use quantity=1, unit=each, unit_price=fixed amount. Unit is a descriptive label, not a restricted enumeration; other labels are allowed. Show both commented examples using an available default tax treatment.
- Comment syntax: a physical line starting with # in column one is ignored only when outside a quoted CSV field. Preserve # lines inside quoted multiline descriptions as data. A first-field description beginning with # must be quoted; export it quoted even if a normal minimal CSV writer would not. Do not strip comments with a naive line filter before CSV parsing. Ignore blank lines outside quoted records. Comments are never persisted as line items.
- Exactly these five headers; standard CSV quoting supports commas, quotes, and multiline descriptions. Validation errors refer to physical file line numbers, including the help block, so the location matches the editor.
- Each row becomes a line item; row order is display order. Add/delete/reorder rows naturally. Computed amounts and invoice metadata are deliberately absent.
- Hourly and fixed lines use the same model: quantity times unit price. `--hours` sets unit to hour; `--amount` sets quantity to one and unit to each. Reject incompatible flags.
- Export explicit tax codes on every row. Require a valid tax value on import, including `exempt`, `zero-rated`, or `none`; never silently reinterpret a blank or misspelled code.
- Validate the entire file before changing SQLite. On failure, report row/column, preserve the edited file, and offer to reopen interactively. On cancellation or editor failure, leave the invoice unchanged and report the recovery path.
- Import all changes in one transaction and show the recalculated invoice. Detect intervening invoice changes through a revision check; refuse to overwrite concurrent changes.
- Posted and void invoices cannot enter editable mode.

## Calculations and taxes

- Use Decimal constructed from strings, never binary floats. Persist decimal inputs as text and finalized currency totals as integer minor units or canonical decimal strings; do not use SQLite REAL.
- For v1, two-decimal currencies only. Permit fractional quantities and unit prices with documented bounded precision. Reject nonfinite numbers, negative amounts, zero/negative quantities, and invalid dates. Negative adjustments and credit notes are out of scope.
- Proposed consistent rounding policy: ROUND_HALF_UP; round each quantity times price to cents, calculate and round tax per line to cents, then sum the rounded line and tax amounts. Document this policy; do not describe it as a universally mandated tax rule.
- Maintain tax code, display label, percentage, and effective date in tax definitions. Pick the applicable rate using the invoice issue date. Reject posting when a referenced code has no applicable rate.
- Distinguish taxable, exempt, zero-rated, and no-tax treatments even when the tax amount is zero. One tax code/treatment per line in v1; multiple different taxes may appear on different lines. Stacking taxes on one line is deferred.
- Resolve and persist a draft's tax rate when adding/importing a line. If the issue date changes, explicitly recalculate applicable rates and show changed totals. Later tax configuration changes must not silently change existing drafts; provide an explicit draft tax refresh operation if needed.
- Display subtotal, separate totals for each tax label/rate, and grand total. Include business tax registration details in CLI details and PDFs.
- Business configuration changes affect new drafts. Existing draft defaults and tax choices remain stable unless explicitly changed. Posting uses the reviewed draft values and freezes them.

## Suggested storage model

Use versioned schema migrations, foreign keys, constraints, and transactions.

| Entity | Main fields |
| --- | --- |
| customers | id, unique slug, name, billing address, optional email, timestamps |
| tax_rates | code, label, percentage as decimal text, effective_from; unique code/date |
| invoices | id (INTEGER PRIMARY KEY AUTOINCREMENT), status, issue date, due date, currency, customer id, customer snapshot, business snapshot, note, revision, timestamps, posted totals, void reason/date, optional source_invoice_id |
| line_items | id, invoice id, position, description, quantity, unit, unit price, tax treatment/code/label/rate snapshot, finalized net/tax amounts |
| schema_migrations | applied schema version |

Store the business profile and defaults in TOML, then copy business and customer details into each new draft. Provide an explicit draft refresh operation if needed, rather than joining live customer information when rendering. Posting freezes all content. Customer edits, default-note changes, tax configuration changes, and business-profile changes must never rewrite an issued invoice.

Invoice ID allocation happens transactionally on draft creation using SQLite INTEGER PRIMARY KEY AUTOINCREMENT. IDs are positive integers, never reset annually, and committed IDs must never be reused, including after deleting a draft. Gaps are acceptable. Format IDs with a minimum width of six digits (`000042`); do not truncate or wrap after 999999.

`invoice init --next-id N` specifies the exact ID to assign to the first new invoice (not the last previously used ID). Default to 1 on a new database if omitted. `invoice sequence show` reports the next ID; `invoice sequence set-next N` allows an explicit forward-only change on an existing database. Require a positive integer within SQLite's supported range and reject any value lower than the existing next allocation or previously used high-water mark. Do not expose sequence control as a TOML setting that could silently reset numbering. Implement initialization/sequence updates transactionally with the SQLite sequence; never use a race-prone SELECT MAX(id) + 1 allocation strategy. Document entering the last GnuCash invoice number plus one. Example: if the last invoice was 001233, initialize with --next-id 1234 and the next invoice is 001234.

Posting validation and snapshot finalization happen in one SQLite write transaction. Require business/customer names and billing addresses, at least one valid line, valid dates, and registration details for the configured GST treatment. A failed posting leaves the draft unchanged. Posting does not allocate another ID or alter the sequence.

Void retains the number, original content, totals, and reason. It is an administrative status, not a credit note or tax adjustment. No deletion or financial editing of posted/void invoices.

## CLI and PDF presentation

Use one shared invoice representation/calculation service for terminal and PDF output. Render a posted invoice from stored snapshots and finalized amounts, not current configuration or recalculated live rates.

CLI `show` includes the six-digit invoice ID, status, business and customer details, issue/due dates, currency, numbered items, quantities/units, unit prices, tax treatments, subtotal, tax breakdown, total, and note. Wrap long descriptions without hiding financial values. Row numbers correspond to edit/remove commands.

PDF uses a clean restrained layout with the same information, readable typography, wrapped descriptions, repeated table headers across pages, page numbers, and an intact totals section. Mark draft previews and void invoices conspicuously. Default names must not overwrite unrelated files; require an explicit overwrite option. Write PDFs atomically. Use font support appropriate to customer names and ordinary Unicode punctuation.

PDF export destinations and filenames:

- Without `--output`, write to the current working directory using `<invoice-id>-<customer-slug>.pdf`, for example `000042-acme.pdf`. Use the same padded permanent ID for drafts and posted invoices. The PDF content clearly indicates draft status; exporting a posted version over a draft requires explicit overwrite.
- `--output` accepts an explicit file path or an existing directory. A directory receives the generated filename. A path ending in a directory separator is also a directory destination and may be created; otherwise a nonexistent path is treated as a file path and must end in `.pdf`. Explicit file paths may override the generated basename.
- Snapshot the customer slug alongside customer details so a later customer rename cannot change a posted invoice's generated filename.
- Validate customer slugs as safe lowercase ASCII filename components (letters, digits, hyphens); sanitize invoice-number components for filenames without permitting path separators or traversal. Reject ambiguous slug collisions rather than silently sharing a slug.
- Print the actual output path on success. Never silently overwrite an existing destination, including a previous export of the same invoice; require `--overwrite`.

Regeneration preserves invoice content, but byte-for-byte PDF reproducibility is not required in v1. Document that users should retain the original issued PDF alongside database backups.

## Build sequence

1. Scaffold package and entry point; implement configuration, database initialization, migrations, and isolated test fixtures.
2. Build Decimal calculations and tax treatment/rate resolution with tests before presentation code.
3. Implement customers, drafts, item commands, metadata/note updates, CLI show/list, and JSON output.
4. Implement atomic CSV editing, validation/recovery, and revision conflict detection; add the customer repeat shortcut using that same editor workflow.
5. Implement transactional draft ID allocation, sequence initialization/advancement, posting, immutable snapshots, and voiding.
6. Implement PDF output and visually inspect short and multipage fixtures.
7. Write a README with installation, business/GST setup, a complete hourly-plus-fixed workflow, rounding policy, numbering continuation, database/config locations, and backup/restore instructions. Back up SQLite through its backup API or with the app closed; do not suggest copying only an active database file in WAL mode.

## Acceptance checks

- Repeating a customer selects its latest posted non-void invoice deterministically, allocates a new ID, copies the intended lines/currency, applies current dates/defaults/tax rates, and launches the editor. Other customers' invoices and the source remain unchanged. Missing sources or unavailable tax codes fail without creating a draft; canceled/failed editor sessions retain a recoverable copied draft. No automatic posting or PDF output occurs.

- A new invoice inherits its default note; overriding/clearing it works, and changing the default leaves existing invoices unchanged.
- 24 hours at 150 plus a fixed 800 produces subtotal 4,400, sample GST at 5% of 220, total CAD 4,620.
- Taxable, exempt, zero-rated, and none lines coexist and retain their distinct treatments.
- Fractional hours, sub-cent unit prices, rounding boundaries, and rate effective dates calculate deterministically.
- CSV quoting and multiline descriptions round-trip, including descriptions beginning with # and # lines inside quoted fields. Generated help lists all headers, line conventions, configured tax codes/rates and built-in treatments; comments/blank lines do not become items. Error locations match physical editor line numbers. Invalid imports or editor failures preserve both the database and edited recovery file.
- Concurrent CSV edits are rejected safely rather than losing updates.
- Initializing with --next-id 1234 produces 001234, then 001235. Padded/unpadded lookup resolves identically; CLI, PDF, filenames, and JSON display padded IDs. Concurrent draft creation produces unique IDs, deleted draft IDs are never reused, and backward/invalid sequence updates fail. Posting preserves the ID and sequence; repeated or failed posting cannot allocate a new ID. IDs larger than six digits do not wrap or truncate.
- Business/customer/tax/default-note edits cannot change posted CLI or PDF content.
- Posted editing/deletion fails; void retains original content and number.
- CLI and PDF amounts agree. Long descriptions, Unicode, and enough items for multiple pages render without clipping or lost totals.
- PDF exports default to the working directory with invoice-id/customer-slug filenames; directory and file output overrides work, posted filename slugs remain stable, and existing files require explicit overwrite. No automatic XDG PDF copy is created.
- Commands work against isolated temporary paths and require no network access.
- XDG path tests cover unset/empty variables, absolute overrides, invalid relative XDG values, and explicit CLI path precedence. Tests isolate HOME and all XDG variables so no real user files are touched. Clearing the cache leaves persistent invoice data and CSV recovery files intact.

## Deliberately deferred

Bookkeeping, double-entry accounting, payment tracking, bank integrations, reminders, recurring invoices, time tracking, inventory, email delivery, web UI, foreign exchange, discounts, credit notes, tax returns, and full jurisdictional tax automation. Tax classification remains the user's responsibility; avoid expanding this into an accounting platform.

## Instruction to Codex

Implement this plan in the target repository, respecting its existing instructions. Start by inspecting the repository, then deliver the working CLI, meaningful tests, PDF examples, and README. Make routine choices using the defaults above. Ask only if an existing project constraint or a material ambiguity blocks implementation. Do not send invoices, contact customers, or treat sample business details as real user data.
