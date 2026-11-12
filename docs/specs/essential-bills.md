# Essential and optional recurring payments

Spec revision 2. This folds the independent review into revision 1. Where the two disagreed, this revision follows the review. The twelve open defaults in revision 1 are decided and are part of this spec.

This version marks each repeating payment as **keep paying**, **safe to cancel**, or **not set yet**, and stores a short note on how to cancel it and what goes wrong if it stops.

The reader of the notes is Jordan's wife. She does not run the finances. A later Start here page will reuse this data. This version does not build that page.

Nothing about paying, forecasting, or the amount set aside changes. The mark is a label and two notes. A safe-to-cancel payment is still paid until someone cancels it with the provider or turns it off in the app.

## What is already there

There is no Discord bot in this repo. Nothing to change for one.

Repeating payments are rows in `recurring_bills` (`app/models.py`, class `RecurringBill`). The Bills page is `/bills` (`app/routes/bills.py`, `app/templates/bills.html`). Each row has a name, provider, amount, frequency, next due date, fixed or variable type, and `paid_from`: the Bills fund (the default), a budget category, or another sinking fund. The page already lists all three. Budget and sinking-fund rows show a Confirm badge and wait to be confirmed. See `docs/recurring-payments.md`.

The page summary (`_summary` in `app/routes/bills.py`) turns each active payment into a monthly figure with `annual_multiplier` in `app/schemas.py` (weekly 52.143, fortnightly 26.071, every 28 days 13.036, monthly 12, quarterly 4, yearly 1, every 2 years 0.5), then divides the year by 12. It shows Bills-fund monthly, Bills-fund annual, and "Other recurring" for everything not paid from the Bills fund. The foreign amount is a note only and is not in the maths. The same multipliers feed the recommended Bills deposit (`app/tasks.py`). `/monthly-cost` is past spending by category and does not use them (`app/routes/monthly_cost.py`).

Bills paid from the Bills fund all use the one system category named Bills (`_bills_category`). A guess from category cannot tell the mortgage from a subscription.

A payment can point at one contract (`contract_id`) and one payment method (`payment_method_id`). Contracts exist on main. A contract has its own `notes` (500 characters) for the plan. Many payments have no contract. Ending a contract does not stop the linked payments.

Payment methods store a nickname, a kind, and the last 4 digits. One card can pay many bills. They are the wrong place for cancel instructions.

Delete on the Bills page sets `is_active` false. The list and the summary use active rows only.

The JSON API is `/api/bills` (`app/routes/bills.py`). Schemas are `RecurringBillCreate`, `RecurringBillUpdate`, and `RecurringBillResponse` in `app/schemas.py`. `serialize_bill` in `app/services/recurring_payments.py` is what the API and the MCP tools return. An omitted field on update stays as it is. Null clears a link such as `contract_id`. REST tells "leave it" from "clear it" with `model_dump(exclude_unset=True)`.

MCP stays at 18 tools (`app/mcp_server.py`, `docs/cli-and-mcp.md`). Bill tools are `list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`, `list_due_payments`, `confirm_payment`, and `skip_payment`. `update_bill` uses an omit sentinel (`_OMIT`) so null can clear a payment method. A default of `None` cannot tell "leave it" from "clear it". There are no contract tools. `create_bill` does not set `contract_id`.

The `glow` CLI (`app/cli/commands/bills.py`) has `list`, `pay`, `add`, `delete`, `due`, `confirm`, and `skip`. There is no edit command. `list` prints a table. `--json` prints the API body. The existing `add` command prompts for the required fields (`prompt=True`). New optional flags do not.

The audit log (`app/audit.py`) writes one JSON line for a successful JSON or MCP create, update, or delete. An update's `changes` object is every mapped column that changed, except `updated_at`. `snapshot_columns` copies mapped columns, so a new column is included with no extra audit code. Creates and deletes store a short summary. For a bill that is id, name, amount, and next due date (`SUMMARY_FIELDS`). HTML form saves are not audited. Passwords, API keys, `last4`, and email are never written. Notes are not redacted.

HTML edits are a compare-and-set on the due date (`cas_save_bill`). Only columns listed in `_BILL_EDIT_COLUMNS` in `app/services/recurring_payments.py` are written. `bills_update` always builds `pending_bill_values` and calls `cas_save_bill`. That expires the object and writes only the tuple, plus the due date. `update_bill_fields` uses the same tuple whenever a due date is sent. A new column left off that tuple is dropped on an HTML save and on a dated JSON or MCP update.

Creates do not use the tuple. `bills_create`, `api_create_bill`, and MCP `create_bill` pass a fixed argument list to `RecurringBill(...)`. `serialize_bill` only shapes the response. The three fields have to be copied in each constructor.

On the edit row the `<form>` only wraps Save. Every other input sets `form="edit-form-{{ bill.id }}"`. A control without that attribute is not submitted.

In `bills_update`, a blank name is ignored (`if name:`). A blank note must become null, and a blank select must clear the mark. A missing key (an older form that does not send the field) leaves the stored value.

The summary bar refreshes only when amount, frequency, where it is paid from, or active status changes (`bills_update`). The event payload is `monthly`, `annual`, and `other`. The listener in `app/templates/bills.html` writes `#bills-monthly-cost`, `#bills-annual-cost`, and `#bills-other-cost`. Leave the toast listener in `app/templates/base.html` as it is.

HTML create and update parse the form by hand. They never call the bill schemas. A validator on `RecurringBillCreate` / `RecurringBillUpdate` covers PUT and MCP only. `api_create_bill` and `api_update_bill` copy `loc`, `msg`, and `type` inline, which strips the submitted value. MCP `create_bill` and `update_bill` currently return `f"Validation error: {exc}"`, and that string includes the input. `_payment_validation` already returns the safe shape (`{"detail": public_validation_errors(exc)}`). The bill tools must use that shape for a `ValidationError`.

Free-text fields elsewhere reject a run of 6 digits after spaces, hyphens, and dots are removed (`reject_account_number` in `app/schemas.py`, regex `_BETWEEN_DIGITS` and `_LONG_DIGIT_RUN`). That rule would reject a phone number in a cancel note ("call 13 13 19" collapses to 6 digits). Do not call `reject_account_number` for these notes.

Pytest builds the schema from the model (`Base.metadata.create_all` in `tests/conftest.py`), not from Alembic. A check that exists only in the revision never runs in the suite.

Current migration head on main is `n4o5p6q7r8s9` (`alembic/versions/n4o5p6q7r8s9_add_occurrence_due_date.py`). SQLite changes use `batch_alter_table`. The paid-from revision `j1k2l3m4n5o6` is the shape to copy: batch alter, check, and on the way down drop the check then the columns.

`GET /api/bills` has no query filter. The CLI calls it with no query. MCP `list_bills` queries the database. The page filter is not that list.

## Decided defaults

1. Three choices, not a yes or no tick: Keep paying, Safe to cancel, and Not set yet.
2. Do not guess a mark from the category, the name, or the contract. Every current payment stays Not set yet.
3. The notes sit on the payment, not on the contract and not on the payment method.
4. Two optional notes: "How to cancel" and "What happens if this stops".
5. A note is not required in order to save the mark.
6. Marking a payment safe to cancel does not stop it. It keeps being paid until someone cancels it with the provider or turns it off in the app.
7. Count every repeating payment in the monthly totals: the Bills fund, budgets, and other savings pots. The new figures are a split of the monthly averages already shown (Bills-fund monthly plus Other recurring), not extra money.
8. Keep unmarked payments out of the keep-paying total, and show them as their own monthly figure.
9. No "pay this first" order and no rank.
10. On the list, under the name, show one short line for a marked payment, and show a drafted note on an unmarked payment so a note written before the mark is visible.
11. Allow a phone number. Reject a run of 12 or more digits. The error sentence is "Don't put a card number here." It does not claim to block account numbers. "BSB 062-000 account 12345678" is a 6-digit run and an 8-digit run, so it is stored.
12. No new command-line edit for an existing payment. The Bills page is where the list gets marked. The assistant tools can change one too. The typed commands show the mark, and can set it when a payment is first added.

## Data

Three new columns on `recurring_bills`. No new table. Null means not set yet. Do not store the word "unset".

| Column | Stored values | On screen |
| --- | --- | --- |
| `importance` | `essential`, `optional`, or null | Keep paying, Safe to cancel, Not set yet |
| `cancel_note` | text or null, 500 characters | How to cancel |
| `if_stopped_note` | text or null, 500 characters | What happens if this stops |

Check constraint: `importance IS NULL OR importance IN ('essential', 'optional')`. Name it `ck_recurring_bills_importance`. Put that check on `RecurringBill.__table_args__` as well as in the migration. The model already has `ck_recurring_bills_paid_from`.

Both notes sit on the payment, not on the contract and not on the payment method.

- A contract is optional, and its `notes` field is already the plan's own notes. Mortgage, rates, and streaming often have no contract. Two payments on one contract (a phone plan and a handset) can need different marks.
- A card is shared, and only the last 4 digits are stored.
- Start here can list payments without joining contracts. It may still show the linked contract's name as context. It must not treat `contracts.notes` as the cancel instructions.

No priority and no separate reason. "What happens if this stops" is the reason. A rank can wait for Start here.

Notes are allowed on any mark, including Not set yet, so a note can be drafted before the mark. Empty text is stored as null.

### What Start here should read

Active rows only. Fields: `id`, `name`, `debtor_provider`, `amount`, `frequency`, `paid_from`, `paid_from_fund_id`, `importance`, `cancel_note`, `if_stopped_note`, `contract_id`. Monthly figure for one payment: `amount * annual_multiplier(frequency) / 12`, local currency, quantized to cents for that one payment. Do not use `foreign_amount`. Do not add a second classification. The page summary does not quantize each payment before adding. See Summary bar.

## Existing payments

Every current row stays null. Do not guess from the category, the name, or the contract service. New payments also start as Not set yet. The add form must not require a mark.

Nudge, on the Bills page only, and only while at least one active payment is unmarked:

- Summary line: "No keep-or-cancel mark: N", linking to `/bills?importance=unset`. It sits beside "No payment method: N".
- One sentence above the table: "Mark each payment as keep paying or safe to cancel. The keep-paying total leaves out anything still unmarked."

Hide that sentence when the count is zero, including after a create, update, or delete that brings the count to zero. The sentence has an id so the summary listener can hide it. Same idea as the existing "No payment method" link. No dashboard banner, no email, and no block on other pages. Deactivating a payment does not clear the mark or the notes. Inactive rows stay out of the list, the counts, and the totals.

## Bills page

Add form and edit row (`app/templates/bills.html`):

- A select, label "If something happens to me", options Keep paying (`essential`), Safe to cancel (`optional`), and Not set yet (blank value). Default Not set yet.
- Two optional text areas, maxlength 500, always rendered and always submitted, including when the mark is Not set yet: "How to cancel" and "What happens if this stops". A hidden or disabled field is not posted, so Save would wipe a note the person can no longer see. "Both stay available either way" means both notes can be filled for Keep paying and for Safe to cancel.
- Helper under the select: both boxes are optional on every payment, where to cancel it, and what goes wrong if it stops. Do not assign one box to each mark.
- On the edit row, put the select and the two boxes on a second `<tr>` with `colspan="9"`. The table has nine columns. They do not fit in the name cell. The confirm row already uses a full-width row. Every new control sets `form="edit-form-{{ bill.id }}"`.
- The second edit row has its own id (`bill-notes-{{ bill.id }}`). Saving and cancelling the edit remove that row (an out-of-band delete is enough). The display row stays one `<tr id="bill-row-{{ bill.id }}">`.

List:

- A badge on the name, not a new column. The table stays Name, Provider, Paid with, Paid from, Amount, Frequency, Type, Next due, Actions. Do not group the table. Grouping would fight the existing name and date sort.
- Keep paying uses `gw-badge--paid` (already green). Safe to cancel gets its own quiet style, not `gw-badge--pending`. That class is the amber warning badge used by Confirm, Ended, and card expiry, so an amber "Safe to cancel" next to an amber "Confirm" would look like the same warning. Not set yet has no badge.
- Under the name, a muted line when that note exists. Safe to cancel shows "How to cancel: …". Keep paying shows "If this stops: …". The other note stays in the edit form. On an unmarked row, show each drafted note that exists ("How to cancel: …" and "If this stops: …"), so a note written before the mark is visible without opening the form.

Filter, same pattern as `?payment_method=none`:

- `?importance=essential`, `optional`, or `unset`.
- The two page filters are ANDed. The table shows a payment only when it matches both. An unknown `importance` shows the full table, same as a bad `payment_method`.
- The summary totals stay the whole active list. `bills_page` builds the summary before the filter. Keep the filter out of `_active_bills`, because the summary and the API list both use it.
- "Show all" is shown when either filter is set. It links to `/bills`, which clears both.
- Saving a row that no longer matches the filter removes it from the table. The edit form posts the current filters. `hx-swap="outerHTML"` would otherwise leave a payment just marked still sitting under `?importance=unset`. Send `HX-Reswap: delete` for that row, and still delete the notes row. A save that still matches the filter returns the display row.

Summary bar, beside the figures already there. All active payments, every `paid_from`, local amount. Sum the annual amounts for the pile, divide by 12, then quantize to cents once per pile. Quantizing each payment and then adding drifts by cents. An unknown frequency still raises `UnknownFrequencyError`. Do not drop that payment out of the total.

Label the three new lines as a split of those monthly averages (Bills-fund monthly plus Other recurring). A yearly bill is spread across the year. The forecast still puts the full amount in the due month, and this work leaves that alone.

- Keep paying: $X a month
- Safe to cancel: $Y a month
- Not marked: $Z a month
- One sentence on the bar: "Safe to cancel is still paid until someone cancels it with the provider."

Give the new amounts and the unmarked count ids. Refresh that bar when the mark changes, not only when the amount or the source changes. Add `importance` to the `before` / `after` comparison in `bills_update`. Put the three new monthly figures and the unmarked count on the `gwBillSuccess` payload from `bills_update`, `bills_create` (add already always fires the event), and `bills_delete`. The listener updates the new elements. Notes do not change the totals, so a note-only save does not need the event.

## Payments outside the Bills fund

The columns are on `RecurringBill`, so a budget payment and a sinking-fund payment get the same mark, notes, badge, filter, and totals. Confirm, skip, pay, auto-pay, budget spent, and fund balances are unchanged.

The recommended Bills deposit, the forecast, bills cover, and the scheduler keep using every active Bills-fund payment. They ignore `importance`. A safe-to-cancel Bills-fund bill stays inside `bills_fund_bills` and the recommended deposit. `/monthly-cost` is unchanged.

## API, MCP, and CLI

No new route and no new MCP tool. The API count stays 58 and the tool count stays 18.

JSON, on create, get, list, and update, through `RecurringBillResponse` and `serialize_bill`:

- `importance`: `essential`, `optional`, or null
- `cancel_note`: string or null
- `if_stopped_note`: string or null

`GET /api/bills` stays a list of active payments with no `importance` query. That query has no caller in this version. Do not add it. The CLI and MCP list are unchanged.

On `PUT /api/bills/{id}`:

- Omit a field and it stays. `model_dump(exclude_unset=True)` already does this. Do not put `_OMIT` on the Pydantic model.
- Null clears `importance`. For the notes, null or a blank string clears, through the shared cleaner.

On MCP `update_bill`:

- Keep `_OMIT` on the MCP arguments for `importance`, `cancel_note`, and `if_stopped_note`, the same way as `payment_method_id`. Omit leaves the value. Null clears it. A blank string still clears a note, through the shared cleaner.
- Leave `_OMIT` off `RecurringBillUpdate`.

`create_bill` and `glow bills add` take the three fields as optional. Omitted means null. Do not start setting `contract_id` from MCP in this work. MCP create does not need `_OMIT`, because omit and null are the same on create.

CLI:

- `glow bills list` adds a column named Mark. Print "Keep paying", "Safe to cancel", or "Not set yet". A missing `importance` prints "Not set yet".
- `glow bills add` accepts `--importance` (`essential` or `optional`), `--cancel-note`, and `--if-stopped-note`. Leave `prompt=True` off these options.
- `--json` already prints the API body.
- No new edit subcommand.

Validation, one shared cleaner used by the form, the Pydantic schemas, and MCP:

- `importance` is only `essential`, `optional`, or null. Anything else is a 422. The form treats a blank select as null.
- Notes are stripped. Blank becomes null. Max 500 characters.
- Separators between digits on a note are space, hyphen, dot, and slash. The 6-digit rule keeps its class (space, hyphen, and dot only). A payment-method nickname such as `11/2026` must still be saved, and that test is what leaves the slash hole on `reject_account_number`. Do not call `reject_account_number` for these notes. One function, `clean_bill_note`, is what the form, the schemas, and MCP call.
- After those separators are removed, reject a run of 12 or more digits with "Don't put a card number here." A phone number such as "Call 13 13 19" is saved.
- Do not echo the note text back in the error body. The bills API already returns loc, msg, and type only. The form shows the message only. MCP `create_bill` and `update_bill` return `_payment_validation(exc)` for a `ValidationError`, not `str(exc)`.

`_BILL_EDIT_COLUMNS`, and the writes in `cas_save_bill` and `update_bill_fields`, include `importance`, `cancel_note`, and `if_stopped_note`. `bills_create`, `api_create_bill`, and MCP `create_bill` pass the three fields into `RecurringBill(...)`.

## Audit log

REST `PUT` and MCP `update_bill` already diff mapped columns. Once the three columns exist, a real change appears in `changes` as `importance`, `cancel_note`, or `if_stopped_note`, with `old` and `new`. Null is JSON null. Do not redact the notes. Do not add them to the short create and delete summary (`SUMMARY_FIELDS` for `bill` stays id, name, amount, next due date). The web form stays unaudited, same as every other HTML save.

A test in `tests/test_audit.py` must show a bill update that changes the mark and a note, both keys present, `updated_at` absent, and the note not replaced with `[redacted]`.

## Migration

One Alembic revision. `down_revision` is `n4o5p6q7r8s9`, which is the head of main. If main has moved, rebase the chain onto the new head. `batch_alter_table("recurring_bills")` so SQLite can add the columns and the check. All three columns nullable. No server default and no backfill. Downgrade drops the check, then the columns. No data rewrite and no balance rewrite.

## Tests

Write them first (`tests/test_bills.py`, `tests/test_audit.py`, `tests/test_mcp.py`, `tests/test_cli.py`):

- A new payment and every existing fixture payment is null, with both notes null.
- HTML and JSON create and update set and clear the mark and both notes. Omit on PUT leaves them. Null clears them. A blank note clears. A blank select clears the mark.
- The HTML edit controls carry `form="edit-form-{{ bill.id }}"`, and both text areas are in the edit response.
- A 12-digit run is rejected, including when slashes, spaces, dashes, or dots sit between the digits. A phone number such as "Call 13 13 19" is saved. "BSB 062-000 account 12345678" is saved. The error text is "Don't put a card number here." The rejected digits are not in the HTML body, the API error, or the MCP result.
- The keep-paying, safe-to-cancel, and not-marked monthly figures use the annual multiplier, summed per pile, divided by 12, and quantized once. Two weekly payments of $1 land on $8.69 together, not $8.70. A yearly payment lands in the right pile. A budget payment and a sinking-fund payment are included. An inactive payment is not. The foreign amount is not. An unknown frequency still raises `UnknownFrequencyError`.
- `/bills?importance=unset` filters the table and leaves the summary as the full list. The two page filters AND. "Show all" appears when either is set. An unknown `importance` shows the full table. Changing only the mark refreshes the summary payload, including the three figures and the unmarked count. Create and delete send that same payload.
- Marking a row on `?importance=unset` removes it (`HX-Reswap: delete`). A row that still matches stays.
- The badge and the note line render for each mark. An unmarked row shows a drafted note. Keep paying uses `gw-badge--paid`. Safe to cancel does not use `gw-badge--pending`.
- The page copy includes "No keep-or-cancel mark", the split label, and "Safe to cancel is still paid until someone cancels it with the provider."
- MCP create and update, including omit versus null, and a blank note clearing. MCP validation for a card number is the `_payment_validation` dict, and the digits are absent. Tool count stays 18.
- CLI list column Mark, printing "Not set yet", and the add flags. The new add options are not prompted.
- The audit case above.
- A safe-to-cancel Bills-fund bill is still inside `bills_fund_bills` and the recommended deposit.
- The model check rejects a value other than `essential`, `optional`, or null (the suite uses `create_all`).
- `GET /api/bills?importance=unset` still returns every active payment. The query is not a filter.
- Upgrade and downgrade of the new revision against SQLite. Existing rows stay null. Head still upgrades. `tests/test_migrations.py` is the place.

If detect-secrets flags the 12-digit fixture, add a `# pragma: allowlist secret` comment, the same way `tests/test_audit.py` does. Prefer an obvious fake.

## Docs to update with the feature

- `docs/recurring-payments.md`: the mark, the two notes, and that confirm, skip, pay, the forecast, cover, and the recommended deposit ignore them. A safe-to-cancel payment is still paid.
- `docs/bills-and-forecast.md`: one sentence that the mark is not read by the deposit, the forecast, or cover.
- `docs/cli-and-mcp.md`: the new fields on the bill tools and on `glow bills`. `_OMIT` covers the mark and both notes. The tool count stays 18. `GET /api/bills` has no importance filter.
- `README.md`: one clause on the recurring-payments feature line. Mention `docs/specs/` in the project structure, because that directory is new.
- Do not add a feature topic file. Do not change `AGENTS.md`. No new endpoint, so the API count stays. No new command flag belongs in the `AGENTS.md` command list.

## Out of scope

- The Start here page, logins for her, printed packs, and email or SMS reminders.
- A Discord bot.
- Ranking, a review date, or a "she has read this" tick.
- Putting the notes on the contract or the card, and any MCP or CLI contract commands.
- Stopping, skipping, or deactivating a payment because it is safe to cancel.
- Changing the Bills deposit, the forecast, bills cover, the scheduler, or `/monthly-cost`.
- Guessing marks for payments that already exist.
- A command-line edit for an existing payment.
- Auditing HTML form saves.
- Editing the notes from the due-to-confirm card.
- A `GET /api/bills?importance=` filter.
