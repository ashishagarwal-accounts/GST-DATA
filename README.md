# Alcove GST Tool

Internal tool for preparing GSTR-1 / GSTR-3B data and GSTR-2A/2B reconciliation for Alcove entities.
Phase 1 produces files for manual upload on gst.gov.in; no GSTN/GSP integration.

## Status

| Module | State |
|---|---|
| Entity master (entities + GSTINs, PAN/checksum validation) | done |
| Ingestion: sales/purchase registers (Tally or manual Excel/CSV), GSTR-2A/2B JSON | done |
| Advances: GST on advances (opt-in per GSTIN), service types with land deduction, oldest-first adjustment, manual rules, refunds, opening balances, GSTR-1 Tables 11A / 11B | done |
| GSTR-3B + ITC cross-utilisation + RCM | next |
| GSTR-2A/2B reconciliation | planned |
| GSTR-1: B2B/SEZ/DE, B2CL, B2CS (net of B2C credit notes), exports, CDNR (B2B notes, separate), CDNUR, nil/exempt, 11A/11B, HSN (B2B/B2C), documents; review screen + offline-tool Excel | done |
| Web UI: dashboard, imports, invoice review, entity master | done |
| Exports (offline-utility JSON / Excel) | planned |

## Setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\setup_mysql.py    # asks for MySQL root password once; writes .env, applies migrations
.venv\Scripts\uvicorn app.main:app --reload    # web app at http://127.0.0.1:8000, API docs at /api/docs
.venv\Scripts\python -m pytest                 # tests run on in-memory SQLite, no MySQL needed
.venv\Scripts\python scripts\make_samples.py   # sample registers + GSTR-2B for a fictitious entity
.venv\Scripts\python scripts\smoke_test.py     # end-to-end check on the alcove_gst_test database (wiped each run)
```

## Sign-in

Every page and API call needs a signed-in user. On a fresh database the first visit shows "Create the
administrator account"; that administrator adds other users (avatar menu > Users). Passwords are stored as
salted scrypt hashes; sessions are server-side (12 hours) in an HttpOnly cookie; 5 failed attempts lock an
email / IP out for 15 minutes. Set `COOKIE_SECURE=true` in `.env` once the app is served over HTTPS.

## Periods

Return pages (GSTR-1, Advance Tax, Imports, Advances, Home checklist) use a single **Return period** chosen on
the page. Analysis pages (Dashboard, Transactions, GST by Cost Centre) use a period range: This / Last Month,
This / Last Quarter, This / Last Financial Year (April-March) or a custom from-to; the API accepts
`period=YYYY-MM` or `from=YYYY-MM&to=YYYY-MM` (up to 36 months).

## Database migrations

The schema is managed by Alembic (`migrations/`). The app applies pending migrations to `DATABASE_URL` on
startup. After changing `app/models.py`, create a migration and commit it:

```powershell
.venv\Scripts\alembic revision --autogenerate -m "describe the change"
```

## Web app

Plain HTML/CSS/JS in `app/web/`, served by FastAPI (no build step). JSON API lives under `/api`.

Zoho Books-style shell: dark sidebar, organisation (GSTIN) switcher and return period in the top bar; every
page works on the selected GSTIN and period.

- **Dashboard**: headline totals across all GSTINs and an import-status grid.
- **Imports**: upload registers / 2A / 2B / bank book, template downloads, row-level errors, import history.
- **Sales & Purchases**: review sales, purchases, 2B and 2A documents with search and totals.
- **Advances**: GST-on-advances preference, service types, advance ledger, manual adjustment rules.
- **Reports** (separate section): Reports Center by category - GST Returns (GSTR-1; GSTR-3B and 2B
  reconciliation coming), GSTR-1 details, Advance Tax (11A/11B). Reports open in a report layout with export.
- **Entities & GSTINs**: add entities and GSTINs, activate/deactivate.

## Import rules

- **All-or-nothing.** Any blocking error rejects the whole file; the response lists each error with its spreadsheet row.
  Warnings (e.g. CGST/SGST on an inter-state supply, tax not matching rate) are saved on the batch for review.
- **One batch per GSTIN + period + kind.** Re-uploading needs `replace=true`, which swaps the old batch out atomically.
- **Duplicates across periods are rejected** (same doc type, number, counterparty and financial year).
- **Registers:** one row per invoice line; rows with the same invoice number are grouped. The header row is found
  automatically in the first 30 rows, and column names are matched against aliases (see `COLUMN_ALIASES` in
  `app/ingest/registers.py`). Required: invoice no, invoice date, taxable value. Tax rate is inferred from the
  amounts when there is no rate column. For purchases, *Supplier Invoice No/Date* take priority over the voucher
  no/date. Credit notes may carry negative amounts; they are stored as positive with doc type `CRN`.
- **Advances** (template at `/api/templates/advance_register.xlsx`): one row per bank-book receipt or refund with a
  Type (Advance / Refund / Other - Other rows are skipped) and a Service Type. Amounts include GST; tax is worked
  back out, on two-thirds of the consideration for land-deduction service types. Opening balances are uploaded
  once per GSTIN with the go-live month as the period. The ledger is replayed on every request (oldest advance of
  the same customer and service type first, manual rules first), so it never goes stale.
  11A = this month's advances still unadjusted at month-end; 11B = adjustments and refunds of earlier advances.
- **Land deduction** is not in the sales register: for land-deduction service types the register's taxable value is
  the full consideration; GSTR-1 reports two-thirds at the service type's rate and keeps the third as land value.
- **Cost centres:** each GSTIN can have several cost centres (branch / project), set up under Entities. An
  optional Cost Centre column on the sales / purchase registers, bank book and opening balances tags each line;
  unknown names reject the file. Advances adjust only against invoices of the same cost centre. The GST by
  Cost Centre report splits output tax, advance tax and ITC per cost centre (untagged lines = Unassigned, so
  rows add up to the GSTIN). Returns stay at GSTIN level.
- **GSTR-1 credit notes:** B2B notes go to 9B CDNR; B2C notes are netted off the B2CS demand (Table 7), except
  inter-state notes above Rs 1 lakh and export notes, which go to CDNUR.
- **2A/2B:** the GSTIN and return period inside the JSON must match the upload. B2B invoices and credit/debit notes
  are loaded; other sections (amendments, imports, ISD) are reported as warnings.
