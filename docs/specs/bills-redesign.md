# Bills page redesign — implementation spec

Revision 2. This folds the independent review of revision 1 into the contract. Where revision 1 and the review disagreed, this revision follows the review. The eight open defaults in revision 1 are decided. Default 7 uses the review’s wording, not “Paid automatically”.

Status of this file: the contract. Slice 1 (off-canvas navigation) is the only application change that lands with it. Slices 2–4 are separate pull requests, in the order in the rollout section. Do not merge them together.

Read these before editing the area:

- `docs/recurring-payments.md` — confirm, skip, pay, `paid_from`, and the mark
- `docs/specs/essential-bills.md` — how the mark, the notes, the filters, and the summary event were built
- `docs/contracts-and-payment-methods.md` — contract stamping and payment-method badges
- `app/templates/bills.html`, `app/templates/_due_to_confirm.html`, `app/templates/base.html`
- `app/routes/bills.py`, `app/services/recurring_payments.py` (`due_to_confirm`, `needs_confirm`, `cas_save_bill`)

The design proposal (`glow-worm-bills-page-redesign`) and the direction C mockups, plus direction A’s side editor, are the visual source. Where this spec and the proposal disagree, this spec wins. Those disagreements are the owner decisions below.

## What changed from revision 1

The review’s blockers and should-fix items, and the nits that were worth keeping. The rest of this file is the corrected contract, not a diff.

1. **Drawer lifecycle.** `#bill-drawer` stays outside `#bills-list` for the life of the page. Create swaps an inner `#bills-rows`. An empty drawer swap calls `closeEditor()` and does not run the open steps. Update and delete include `billId`. Focus returns after the response has settled.
2. **One form, then the other.** The edit form contains the fields, the mark section, Save, Cancel, and Delete. The confirm form is the next sibling, after `</form>`. Skip lives in the confirm form and posts `due_date`. It does not post the edit form.
3. **Off-canvas and a collapsed rail.** The `max-width: 800px` rules override `.gw-sidebar.collapsed`, the label rule, the centered links, the hidden user and Sign out, the collapsed logo padding, and the collapsed user-profile centering. Menu and the backdrop are not inside `#gw-sidebar`. While the menu is open the Menu button moves clear of the logo. `.gw-main` gains top padding so Menu does not cover the page title.
4. **Errors have a real target.** Every chip has `#due-soon-errors-{{ id }}`, including the idle chip and the confirm view. Every `bills_update` 4xx uses `_editor_error` (retarget, no `gwError`). The route sends `HX-Focus` for the field it was parsing, and a too-long note opens the mark section. The 409 expected-date input does not use `form=`.
5. **Each slice is green on its own.** Tests are split by the slice that changes the behaviour. Slice 2 does not assert the quiet view, `marks=1`, or the 303. Slice 4 adds those. Slice 2 must not be the last pull request: between slice 2 and slice 4 the notes are only inside the collapsed editor.
6. **`from` defaults to `"bills"`.** The bills-page path is `origin not in {"dashboard", "card", "strip"}`, replacing the old `else`. A missing `from` is not a separate value.
7. **Bill type calls `closest('form')`.** `gwPaidFromChanged` is `window.gwPaidFromChanged` from a classic `bills.js`. The inline bills script is deleted so `gwBillSuccess` is not registered twice. The `base.html` listener stays.
8. **Create `PaidFromError` does not double-report.** Same helper as the other create failures: paragraph, status, no `gwError`.
9. **Focus, overflow, and Esc.** The focusable list skips controls inside a closed `<details>` except the `<summary>`. Crossing 800px restores overflow, and a drawer close does not unlock the page while the menu is open. Crossing above 800px moves focus into the dialog. One Esc closes the menu first. `closeNav()` moves focus before it sets `inert`. Above 800px the open drawer sets `inert` on `main.gw-main` and `#gw-sidebar`.
10. **`today` is passed into every render** that colours an overdue date.
11. **The quiet view does not emit a `#due-soon` swap.**
12. **Nits kept.** Fixed-chip copy (decided below). A variable chip that is not yet due gets one muted line. A missing Bills fund links its own sentence. An empty `importance` is ignored. The 409 swap is `outerHTML`. The edit form keeps both contract sentences. Add new does not gain `[data-overdue-note]`. Client sort ties break on next due, then id. At exactly 800px the layout is the narrow one.

## Owner decisions

These are settled. Do not reopen them in the slices below.

1. **Direction C for the daily page.** A due-soon strip with Confirm and Skip, then one compact line per payment, sorted by next due. The other current sort keys live in a select.
2. **Direction A’s drawer for editing.** At a viewport wider than 800px the editor is a side drawer. At 800px and below the same form sits under that row. One form partial either way. Exactly 800px is the narrow layout (`max-width: 800px` for the nav and the drawer). Do not retune the 768px grid breakpoint.
3. **Confirm and Skip stay in the editor**, including when the date is still in the future, so an early confirm still works. The closed line does not repeat them.
4. **Off-canvas navigation under 800px, on every page** that extends `base.html`. A Menu control opens it. At exactly 800px the menu is the off-canvas one.
5. **The keep-paying / safe-to-cancel feature is quiet.** It is for Jordan’s wife if he passes away. It is not a day-to-day control. On `/bills`, remove:
   - the Keep paying, Safe to cancel, and Not marked summary figures
   - the “No keep-or-cancel mark: N” link and the sentence “Mark each payment as keep paying or safe to cancel…”
   - the mark badges, the legend, and the note lines on rows
6. **Those pieces move to `/bills/if-something-happens`.** That view shows the three totals, the unmarked count, each bill’s mark and both notes, and the existing `?importance` filters. A small muted link on `/bills` opens it. The mark and the two notes stay editable in the drawer, inside a collapsed section labelled “If something happens to me”. The link is added in slice 4, with the route. Slice 2 does not point at a missing page.
7. **A fixed Bills-fund chip has no Confirm and no Skip.** It never says the payment was already paid. When `next_due_date` is today or still ahead, one muted line reads “Pays from the Bills fund”. When `next_due_date` is before today (the date has passed and this occurrence is not logged yet), the line reads “Not logged yet. It pays from the Bills fund.” The word Fixed stays off the chip and off the closed line. A variable Bills-fund chip inside the ten-day window whose date is still after today is not in `due_to_confirm`, so it has no buttons; one muted line reads “Not due for a decision yet”.
8. **The daily summary is the three cards that remain:** Bills fund monthly, Bills fund annual, Other recurring. Keep the existing element ids and the `gwBillSuccess` refresh.
9. **Other defaults from the proposal:**
   - one compact line per closed row
   - the word Fixed is absent from closed rows
   - Add new stays its own collapsed panel, and it keeps the start date
   - a missing budget is explained once
   - `gw-badge--pending` (the amber pill) is only for card warnings and an ended contract
   - the button stays labelled Delete, and the confirm copy stays “Deactivate this bill?”
   - both notes, in full, on the quiet view, whenever they are non-null
   - sort key in the select, ascending, Reverse toggles, nothing in `localStorage`
   - no discard prompt when a second Edit replaces the form
   - no focus trap on the inline form

Direction B’s “group by paid from” toggle is out of scope. Do not build it in these slices.

## What does not change

- No Alembic revision, no new column, no data rewrite, no balance rewrite. `importance`, `cancel_note`, and `if_stopped_note` already exist (`RecurringBill` in `app/models.py`, check `ck_recurring_bills_importance`, revision `o5p6q7r8s9t0_add_bill_importance.py`).
- Confirm, skip, pay, auto-pay, the forecast, bills cover, the recommended deposit, and `/monthly-cost` ignore the mark. A safe-to-cancel payment is still paid until someone cancels it with the provider or deactivates it.
- REST stays 58 endpoints. `GET /api/bills` still has no `importance` filter, and a query on that path is not redirected. `PUT` omit-versus-null is unchanged. `tests/test_bills.py` calls `/api/bills?importance=unset` and expects 200.
- MCP stays 18 tools. CLI `glow bills` is unchanged, including the Mark column and the add flags. There is still no `bills edit` command.
- The audit log is unchanged. HTML saves stay unaudited. `SUMMARY_FIELDS` for a bill stays id, name, amount, and next due date. Notes are still not redacted on JSON and MCP updates.
- `app/services/recurring_payments.py` payment rules, `cas_save_bill`, `_BILL_EDIT_COLUMNS`, and `pending_bill_values` stay as they are. This work adds one read function, `due_soon`, beside `due_to_confirm`.
- The dashboard Due to confirm card stays on `_due_to_confirm.html` with `id="due-to-confirm"` and `from=dashboard`. `GET /bills/due-card` stays for that card’s Cancel. Leave the doubled “Set it on the Budgets page” wording on that card so `tests/test_dashboard.py` `TestDueToConfirmCard` stays green.
- Login (`app/templates/login.html`) has no sidebar and does not change.
- `.gw-summary-bar` stays in `app/static/styles.css`. Sinking funds, budgets, transactions, shares, and the forecast panel still use it. Only `/bills` stops using it.
- No Pay control on this page. `>Pay<` and `/bills/{id}/pay` stay off the HTML.
- Other pages keep their own `form=` attributes (`sinking_funds.html`, `transactions.html`, `users.html`). This work does not touch them.
- Unknown-frequency confirm and skip stay on `_unknown_frequency_html`: status 400, body “Unknown bill frequency: …”, `HX-Trigger: gwError`. They are not retargeted and they are not turned into 422.

## Pages and routes

| URL | Handler | Role |
| --- | --- | --- |
| `GET /bills` | `bills_page` | Daily page. Optional `payment_method`. |
| `GET /bills/if-something-happens` | new `bills_if_something_happens` | The quiet mark view. Slice 4. |
| `POST /bills` | `bills_create` | Add. Returns the rows, plus an out-of-band strip. |
| `GET /bills/due-soon` | new `bills_due_soon` | Strip partial. Same idea as `GET /bills/due-card`. |
| `GET /bills/due-card` | `bills_due_card` | Unchanged. Dashboard only. |
| `GET /bills/{bill_id}` | `bills_get_row` | One compact line. |
| `GET /bills/{bill_id}/edit` | `bills_edit_form` | The editor partial, into `#bill-drawer`. |
| `GET /bills/{bill_id}/confirm` | `bills_confirm_form` | Amount form. `from` picks the wrapper. |
| `POST /bills/{bill_id}` | `bills_update` | Compare-and-set save. |
| `POST /bills/{bill_id}/confirm` | `bills_confirm` | Unchanged service call. New HTML shape. |
| `POST /bills/{bill_id}/skip` | `bills_skip` | Same. |
| `DELETE /bills/{bill_id}` | `bills_delete` | Deactivate. Also refreshes the strip when the page has one, and clears the drawer. |

Register `GET /bills/if-something-happens` and `GET /bills/due-soon` **above** `GET /bills/{bill_id}`. FastAPI would otherwise try to parse the path segment as an int. `GET /bills/due-card` is already above that route. Follow that order.

`GET /bills` with a non-empty `importance` query (including an unknown value) responds **303** to `/bills/if-something-happens` and keeps the rest of the query (`payment_method` included). This redirect is slice 4. Until that slice, `/bills?importance=` still filters the daily page, which is what the existing tests call. An empty `importance` (`?importance=` or a missing key) is not a value: `_page_query` already drops it with `if importance:`, and the redirect ignores it too. `GET /bills?payment_method=none` stays on the daily page. Do not redirect `GET /api/bills`.

The Bills nav item stays active on the new path. `base.html` already treats a nav href as active when `location.pathname.startsWith(href)` and the href is not `/`. `/bills/if-something-happens` starts with `/bills`. Do not add a sidebar item for the quiet view.

`bills_confirm` and `bills_skip` default `from` to the string `"bills"` (`Query("bills", alias="from")`). There is no request with no `from`. Write the bills-page branch as `origin not in {"dashboard", "card", "strip"}` and replace today’s `else`. The same condition covers GET. `"bills"`, `"editor"`, and any other unknown value share that branch.

- Success: `#bill-row-{id}` `outerHTML`, strip replaced (when this page has the strip), drawer cleared, `gwBillSuccess` with `message`, `billId`, `active_count`, `waiting_count`, and no money keys.
- `GET` with `from` absent (the default `"bills"`) or `from=editor` renders `_bill_confirm.html` alone, without the chip wrapper. `tests/test_bills.py` gets `/bills/{id}/confirm` with no query and expects “Due 1 Feb 2026”, `name="amount"`, `name="due_date"`, and “From Bills fund”.
- `GET ?from=strip` renders the chip wrapper (`id="due-soon-{{ id }}"`) around that partial, including `#due-soon-errors-{{ id }}`.
- `from=dashboard` and `from=card` stay on today’s card path.

## Templates and partials

Split the one `bills.html` fragment switch. The `{% extends "base.html" %}` branch stays the daily page. New files:

| File | What it renders |
| --- | --- |
| `app/templates/bills.html` | Daily page only. Extends `base.html`. |
| `app/templates/bills_if_something.html` | The quiet view. Extends `base.html`. Slice 4. |
| `app/templates/_bill_line.html` | One `<article id="bill-row-{{ bill.id }}">`. |
| `app/templates/_bill_list.html` | The lines, no wrapper. Create swaps this into `#bills-rows`. |
| `app/templates/_bill_editor.html` | The drawer / inline form. |
| `app/templates/_bill_confirm.html` | Amount, hidden `due_date`, Confirm, Cancel. Shared by the strip chip and the editor. |
| `app/templates/_due_soon.html` | `<div id="due-soon">` and its chips. Always rendered, even when empty. |
| `app/templates/_due_to_confirm.html` | Unchanged. Dashboard only. `/bills` stops including it. |
| `app/static/bills.js` | Sort, drawer, focus, `window.gwPaidFromChanged`, and the `gwBillSuccess` listener that updates the money and the counts. A classic script, not `type="module"`. |
| `app/static/styles.css` | Layout rules. Reuse the `gw-*` classes below. |

`app/templating.py` already exposes `source_label`, `source_is_broken`, `needs_confirm`, `contract_label`, `contract_choice_label`, `choice_label`, and the `money` and `due_label` filters. The new partials use those. Do not reimplement them in the template.

`_render_table_body`, `_render_bill_row`, `_render_edit_row`, and `_render_confirm_row` in `app/routes/bills.py` switch to the new templates. The fragment names `table_body`, `edit_row`, and `confirm_row` go away. `bill_row` becomes `_bill_line.html`.

Every render that colours an overdue date receives `today`, the ISO string from the same `_today()` the strip uses. That is `_render_bill_row`, the list partial, the due-soon partial, and both full pages. Swapped lines and the create response use the same helper. Do not rely on a page-level variable that a partial render never sets.

Both pages include `bills.js` once. Delete the inline script at the bottom of `bills.html` (the current `gwPaidFromChanged` and the money `gwBillSuccess` listener) in the same change. Leaving it registers that listener twice. The `base.html` listener is a different one: it only paints a strip when `#bills-message` is absent. Leave it alone.

Drawer shell, outside the list, on both pages:

```html
<div id="bills-stack">
  <div id="bills-list">
    <div id="bills-rows" class="gw-bill-list">
      <!-- articles only. Create swaps this node's innerHTML. -->
    </div>
  </div>
  <div id="bill-drawer" hidden></div>
</div>
<div id="bill-drawer-backdrop" class="gw-drawer-backdrop" hidden></div>
```

`#bill-drawer` is a sibling of `#bills-list`. It is never a descendant of `#bills-list` or `#bills-rows`, including while it is open. `placeEditor()` must not call `row.after(drawer)` or otherwise move the node into the list. A later Add replaces `#bills-rows` and the drawer survives.

The name of a payment is the entire text of one element, so the existing `>Rent<` / `>Internet<` assertions still match: `<strong>{{ bill.name }}</strong>`.

### Daily page (`bills.html`)

Order, top to bottom:

1. Header row. `h2.gw-page-header` “Recurring payments”. Forecast link, unchanged: `/sinking-funds/{{ bills_fund_id }}/forecast`, class `gw-text-muted`, only when `bills_fund_id` is set.
2. Subtitle `<p id="bills-count" class="gw-text-muted">`. Unfiltered active count, then “waiting to confirm” (`len(due_to_confirm)`). Example: `27 active payments · 7 waiting to confirm`. Both numbers ignore `payment_method`. The filtered list can be shorter; Show all explains that.
3. `#bills-message` (kept, `aria-live="polite"`). Success text and failed creates land here.
4. Add new. The existing `<details class="gw-details gw-section gw-add-form">`. Same fields, including start date. See the field map. The mark and the two notes move into a nested `<details class="gw-details">` whose summary is “If something happens to me”, closed by default. The inputs stay enabled and keep their `name`s so they still post. Add new has `[data-confirm-help]` and does not have `[data-overdue-note]`. Do not add that node.
5. Three stat cards in `.gw-grid-3`. See Summary.
6. The payment-method chip, still a link: `No payment method: {{ unset_count }}` to `/bills?payment_method=none`. When `payment_filter` is set, a “Show all” link to `/bills`. This chip has no id today and is not in the event payload. Leave that gap. Do not add the keep-or-cancel chip here.
7. One muted link, about `0.85rem`, class `gw-text-muted`, href `/bills/if-something-happens`, text “If something happens to me”. It is not a button, not a stat, and not a nav item. Slice 4 only.
8. `{% include "_due_soon.html" %}`.
9. Sort select, then `#bills-stack` as drawn above.

The add form’s `hx-target` is `#bills-rows` and `hx-swap` is `innerHTML`. It is not `#bills-list`.

Drop `#bills-unmarked-note` entirely. Drop the listener branch that toggles it.

### Due-soon strip

`due_soon(db, today=None, days=10)` lives next to `due_to_confirm` in `app/services/recurring_payments.py`. `today` defaults to `clock.today()` via the existing `_today()`, so a test that patches `_today` covers both.

A chip is included when the bill is active, `next_due_date` is on or before `today + 10 days`, and the occurrence is still inside `end_date` (same end check as `due_to_confirm`: skip when `end_date` is set and `next_due_date` is after it). Overdue bills are included. Order is `next_due_date`, then `bill.id`.

`DUE_SOON_DAYS = 10` is the only copy of the window.

Each chip is `<div id="due-soon-{{ bill.id }}" class="gw-card gw-due-chip">` inside `<div id="due-soon" class="gw-due-soon gw-section">`. The outer div is **always in the DOM**. When there are no chips it has the `hidden` attribute. An out-of-band `outerHTML` swap can then show the strip after a create without the target having to be inserted from nothing. HTMX will not create a missing id.

The first child of every chip, idle and confirm view, is an empty error target:

```html
<div id="due-soon-errors-{{ bill.id }}" tabindex="-1" aria-live="assertive"></div>
```

`GET /bills/{id}/confirm?from=strip` replaces the chip with `outerHTML` and that replacement includes the same error node. Idle Skip and a 422 on the amount form both have a node to land in.

Chip contents, after the error node:

- Name, amount (`|money`), `source_label`, and the date through the `due_label` filter (`26 Oct 2026`). A date before `today` uses `gw-text-negative`.
- `+ N more` when the bill is also in `due_to_confirm` and `more_due > 0`. Same meaning as the dashboard card.
- Confirm and Skip only when the bill is in `due_to_confirm` (needs a decision, and `next_due_date <= today`). A fixed Bills-fund bill has neither button. Its muted line is the one in owner decision 7.
- A variable Bills-fund bill that is in the window and not yet in `due_to_confirm` has neither button. Its muted line is “Not due for a decision yet”.
- When `problem` is set on a chip that would otherwise have buttons, the same rule as the dashboard card: no Confirm button, Skip stays. The problem is shown once. `problem.detail` already contains the instruction. Do not add a second anchor whose text repeats that sentence. Link rules:
  - `code == "no_budget"`: the words “Budgets page” link to `problem.href`. The detail is “There's no {name} budget for {month}. Set it on the Budgets page first.” The phrase “Set it on the Budgets page” occurs once in the chip.
  - The detail contains “Change where this is paid from” (`not_budget`, and a deleted sinking fund): those words are the link to `problem.href`.
  - The detail is “The Bills fund is missing.”: do not invent a second sentence. The whole sentence is the link, and `problem.href` is `/bills`.
- A fixed chip with a problem still has no Confirm and no Skip. Show the problem with the same link rules, plus the decision-7 line.

Confirm on a chip: `hx-get="/bills/{{ id }}/confirm?from=strip"` targeting `#due-soon-{{ id }}` with `outerHTML`. The response is that same id wrapping `_bill_confirm.html` (amount prefilled, hidden `due_date`, “Due {{ date|due_label }}”, “From {{ source }}”, month-year in parentheses for a budget, Confirm, Cancel) and the error node. Cancel is `hx-get="/bills/due-soon"` targeting `#due-soon` with `outerHTML`.

Skip on a chip: `hx-post="/bills/{{ id }}/skip?from=strip"`, `hx-target="#due-soon"`, `hx-swap="outerHTML"`, `hx-vals` with `due_date`, `hx-confirm="Skip the {{ date|due_label }} payment?"`, `hx-disabled-elt="this"`, class `gw-action-edit`. Confirm’s submit button keeps `hx-disabled-elt="this"` and class `gw-action-pay` on the first press, then `gw-btn gw-btn-success gw-btn-sm` on the amount form (same as today’s card).

The strip is a horizontal flex row, `overflow-x: auto`, `flex-wrap: nowrap`, so at 390px it scrolls sideways and the list below does not.

### Compact line

`_bill_line.html` takes `show_marks` (default false).

```html
<article id="bill-row-{{ bill.id }}" class="gw-bill-row"
         data-sort-name="{{ bill.name }}"
         data-sort-provider="{{ bill.debtor_provider }}"
         data-sort-paid-with="{{ paid_with[bill.id].sort }}"
         data-sort-paid-from="{{ source_label(bill) }}"
         data-sort-amount="{{ bill.amount }}"
         data-sort-frequency="{{ frequency_labels.get(bill.frequency, bill.frequency) }}"
         data-sort-next-due="{{ bill.next_due_date }}">
```

One line, then one muted meta line:

- Line: `<strong>` name. Then the warning badges listed below. Then the type word (see field map). Then the amount, `${{ bill.amount|money }}`, aligned to the end. Then the Edit button.
- Meta: provider, frequency label, source (`gw-text-negative` when `source_is_broken`), and `<time datetime="{{ bill.next_due_date }}">` with `due_label`. The time is `gw-text-negative` when `next_due_date < today`. ISO strings compare in order.

Edit:

```html
<button id="bill-edit-{{ bill.id }}" type="button" class="gw-action-edit"
        hx-get="/bills/{{ bill.id }}/edit{{ query }}"
        hx-target="#bill-drawer" hx-swap="innerHTML"
        aria-expanded="false" aria-controls="bill-drawer">Edit</button>
```

`query` is the current page query. On the daily page that is `payment_method` only. On the quiet view it includes `importance`, `payment_method`, and `marks=1` (see that view). The closed line has no Confirm, Skip, or Delete.

While the drawer is open for this id, `bills.js` sets the button’s text to “Close” and `aria-expanded` to true. Close runs the same client function as Cancel. It does not POST.

`show_marks` true adds, on the name line, the mark treatment in the field map, and under the meta line both notes in full when they are non-null:

- `How to cancel: {{ bill.cancel_note }}`
- `If this stops: {{ bill.if_stopped_note }}`

Class `gw-text-muted` on each. No truncation. The daily page passes `show_marks` false, so a saved note is invisible until the quiet view or the drawer.

### Sort

`#bills-sort`, class `gw-select`, `aria-label="Sort payments"`. Options, in this order, with these values (the values replace the old `colMap` keys):

| Value | Label |
| --- | --- |
| `next_due_date` | Sort by next due |
| `name` | Sort by name |
| `amount` | Sort by amount |
| `paid_from` | Sort by paid from |
| `paid_with` | Sort by paid with |
| `provider` | Sort by provider |
| `frequency` | Sort by frequency |

Default is `next_due_date`, which matches `_active_bills` (`order_by(RecurringBill.next_due_date)`). Changing the select sorts ascending. Dates and text use `localeCompare` on the matching `data-sort-*` attribute. Amount uses `parseFloat`. Equal keys then compare `data-sort-next-due`, then the numeric id from `bill-row-{{ id }}`, so a reload does not reshuffle rows that share a due date. When the key is already `next_due_date`, the tie-break is the id only. `#bills-sort-reverse` (class `gw-btn gw-btn-ghost gw-btn-sm`, text “Reverse”) toggles direction and sets `aria-pressed`. Nothing is written to `localStorage`. A reload returns to next due, ascending.

Sort these nodes only: `#bills-rows > .gw-bill-row`. The drawer is not one of them. After appending the rows, if `#bill-drawer` is open, run `placeEditor()` so the reserved gap follows its row. After any swap that replaces a row or `#bills-rows` (`htmx:afterSwap`), re-apply the current key and direction so a confirm that advances the date moves the line.

The old `sortBills` column indexes and `.gw-sort-indicator` go away. An open editor is not a child of the list, so it cannot be left behind the way `bill-notes-{id}` was.

### Quiet view (`bills_if_something.html`)

Slice 4. Title block: “If something happens to me — Glow Worm”. `h2` “If something happens to me”. A muted “Recurring payments” link back to `/bills`.

One sentence, the existing copy, moved here with the totals:

> Safe to cancel is still paid until someone cancels it with the provider.

One more sentence under the three cards, so the figures are not read as the forecast:

> Keep paying, safe to cancel, and not marked add up to every active payment. Bills fund monthly on the recurring payments page is only what the forecast uses.

Then the three pile cards (see Summary), then the filter links:

- Keep paying → `?importance=essential`
- Safe to cancel → `?importance=optional`
- Not set yet → `?importance=unset`, with `<span id="bills-unmarked-count">{{ unmarked_count }}</span>` inside the link
- Show all → `/bills/if-something-happens`, rendered when `payment_filter` or `importance_filter` is set, including when `importance` is an unknown value

`_page_query` keeps building these so a `payment_method` that arrived via the redirect stays on the links. The two filters still AND. An unknown `importance` leaves the list unfiltered and still shows Show all. `_summary` still runs on the full active list **before** `_filter_bills`, so the three cards do not shrink when a filter is on.

No Add new, no forecast link, no due-soon strip, no “No payment method” chip, no three forecast cards. `#bills-message` is present so the shared success listener has a home. The list is `#bills-rows` inside the same `#bills-stack`, with `show_marks` true, plus the same sort select. A legend sits above the list: `gw-badge gw-badge--paid` “Keep paying”, `gw-badge gw-badge--quiet` “Safe to cancel”, and `gw-text-muted` “Not set yet”. The legend is not amber.

Empty filter: one muted line in `#bills-rows`, “No payments match.”

Edit on this page uses the same drawer partial. The form’s action, the edit URL, the Delete URL, and the confirm and skip URLs carry `marks=1` plus the current filters. `bills_update` treats `marks=1` as “render the line with `show_marks`”. It is not a filter and it must not trip the `HX-Reswap: delete` branch. That branch stays exactly: when `payment_method` or `importance` is set and the saved bill fails `_filter_bills`, respond with `HX-Reswap: delete` and an empty primary body (plus the drawer-clear out-of-band, and the summary event when the compared fields changed).

Do not include a `#due-soon` out-of-band fragment on a request that came from this page. htmx 2.0.4 drops a missing out-of-band target and fires `htmx:oobErrorNoTarget`. `marks=1` is the signal: the quiet view sends it on edit, save, delete, confirm, and skip, and the daily page never does. When `marks=1` is present, skip the strip fragment. The row swap still happens.

## Summary cards and the refresh event

`_summary` and `_summary_trigger` stay. The payload keys stay:

`monthly`, `annual`, `other`, `essential`, `optional`, `unmarked`, `unmarked_count`, `message`.

`monthly`, `annual`, and `other` are still `str(Decimal)` (no thousands separator). The three pile figures are still `_money_format` (commas, two decimals). `unmarked_count` is still an int. Messages stay `"Bill added."` and `"Saved."`.

Add `active_count` and `waiting_count` on `_summary_trigger` and on the confirm/skip payload. Add `billId` on the update payload and on the delete payload (the bill’s id). Confirm and skip already gain `billId` with the counts. Existing tests look for substrings, not a closed set of keys. Create does not need `billId` for the drawer; it does not clear the drawer.

### Where each id lives

| Id | Page | What the listener writes |
| --- | --- | --- |
| `bills-monthly-cost` | `/bills` only | `'$' + detail.monthly`, replacing the element’s text. Initial HTML is `${{ total_monthly\|money }}` **inside** the element, so `tests/test_every_2_years.py` still sees `id="bills-monthly-cost">$10.00`. |
| `bills-annual-cost` | `/bills` only | `'$' + detail.annual`. Same shape. |
| `bills-other-cost` | `/bills` only | `detail.other` with no dollar sign. The `$` sits outside the span, as it does today. |
| `bills-essential-cost` | quiet view only | `detail.essential`, dollar sign outside the span. |
| `bills-optional-cost` | quiet view only | `detail.optional`. |
| `bills-unmarked-cost` | quiet view only | `detail.unmarked`. |
| `bills-unmarked-count` | quiet view only | `String(detail.unmarked_count)`. |
| `bills-count` | `/bills` only | `"{active_count} active payments · {waiting_count} waiting to confirm"` when both keys are present. |
| `bills-unmarked-note` | nowhere | Delete the element and the listener lines. |

The listener updates an id only when that element is on the page. A save on `/bills` still sends `essential` and `unmarked_count`; the quiet view is not open, so those writes no-op. Opening the quiet view later renders the numbers from `_summary`.

Daily cards, reusing `gw-stat`:

| Card | Classes | Label | Value id | Caption |
| --- | --- | --- | --- | --- |
| Bills fund monthly | `gw-stat gw-stat--amber` | Bills fund monthly | `bills-monthly-cost` | Feeds the forecast |
| Bills fund annual | `gw-stat` | Bills fund annual | `bills-annual-cost` | Same bills, over a year |
| Other recurring | `gw-stat` | Other recurring | `bills-other-cost` | Budgets and other funds, a month |

Labels are `gw-stat-label`. Values are `gw-stat-value`. The caption is `gw-text-muted`.

Quiet-view cards:

| Card | Classes | Label | Value |
| --- | --- | --- | --- |
| Keep paying | `gw-stat gw-stat--green` | Keep paying | `$<span id="bills-essential-cost">{{ essential_monthly\|money }}</span> a month` |
| Safe to cancel | `gw-stat` | Safe to cancel | `$<span id="bills-optional-cost">…</span> a month` |
| Not marked | `gw-stat` | Not marked | `$<span id="bills-unmarked-cost">…</span> a month` |

Do not use `gw-stat--amber` on Not marked. Amber on a stat reads as a warning. The top border on the monthly card is the existing `gw-stat--amber` rule (a 2px line, not a pill).

`gwBillSuccess` is still `HX-Trigger-After-Swap`, so the nodes exist before the listener runs. The money listener moves into `bills.js`. Leave the `base.html` listener alone.

The event fires in the same cases as today:

- Create always fires, message “Bill added.”
- Delete always fires, message “Saved.”, plus `billId`.
- Update fires only when the `before` / `after` tuple changes. That tuple stays `(amount, frequency, paid_from, paid_from_fund_id, is_active, importance)`. A note-only save does not fire. Do not add the notes to the tuple. When it fires, the payload includes `billId`.
- Confirm and skip on the bills-page branch fire `{"message": result.message, "billId": bill.id, "active_count": …, "waiting_count": …}` and **do not** send the money keys. The money listener already ignores a payload without `monthly`.
- Confirm and skip `from=dashboard` and `from=card` stay message only, as today.

Delete always clears the drawer, including when the tuple did not change. The empty-drawer swap is what closes it, not the event. `billId` is still on the delete payload so a test can see it, and so the client’s `billId` check agrees with the swap.

## HTMX targets

`_oob` in `app/routes/bills.py` today stamps `hx-swap-oob` onto a leading `<tr` or onto `id="due-to-confirm"`. Extend it so a leading `<article`, `<div`, or any other element gets the attribute on its first tag. Keep the `<tr` behaviour so nothing else that still returns a row breaks. Callers pass the swap (`outerHTML` or `innerHTML`). Use that explicit value. Do not use `hx-swap-oob="true"`.

`#due-soon` is refreshed by replacing the whole container (`outerHTML`), including the `hidden` attribute when the chip list is empty. Do not `hx-swap-oob="delete"` it. Skip this fragment when the request has `marks=1`. The dashboard path in `_after_payment` (`origin == "dashboard"` and `origin == "card"`) is unchanged, including the empty-card delete of `#due-to-confirm`.

| Action | Primary target | Also in the body | Event |
| --- | --- | --- | --- |
| Create 200 | `#bills-rows` `innerHTML` = `_bill_list.html` (server order). The drawer is not in this subtree. | `#due-soon` out-of-band `outerHTML` | Full payload, “Bill added.” No `billId`. |
| Create validation | no swap | — | no `gwBillSuccess` |
| Update, row still listed | `#bill-row-{id}` `outerHTML` = the line (`show_marks` when `marks=1`) | `#bill-drawer` out-of-band `innerHTML` empty; `#due-soon` out-of-band `outerHTML` unless `marks=1` | Full payload, including `billId`, only when the tuple changed, “Saved.” |
| Update, row falls out of the filter | `HX-Reswap: delete` on the row | drawer cleared; strip replaced unless `marks=1` | Same event rule, including `billId` when it fires |
| Delete | `#bill-row-{id}` `delete` (empty primary body; the button already asks for `delete`) | drawer cleared; strip replaced unless `marks=1` | Full payload, “Saved.”, `billId` |
| Confirm or skip `from=strip` | `#due-soon` `outerHTML` | `#bill-row-{id}` out-of-band `outerHTML`. Does not clear the drawer. | message, `billId`, counts |
| Confirm or skip, `origin not in {dashboard, card, strip}` | `#bill-row-{id}` `outerHTML` | strip replaced unless `marks=1`; drawer cleared | message, `billId`, counts |
| Confirm or skip `from=dashboard` | `#due-to-confirm`, as today | no bills-page out-of-band | message only, as today |
| Confirm or skip `from=card` | unchanged card path | unchanged | unchanged |
| `GET …/edit` | `#bill-drawer` `innerHTML` | — | — |
| `GET …/confirm?from=strip` | `#due-soon-{id}` `outerHTML` | the error node is inside that chip | — |
| `GET …/confirm` default or `from=editor` | the caller’s target | `_bill_confirm.html` only, no chip wrapper | — |
| `GET /bills/due-soon` | `#due-soon` `outerHTML` | — | — |

After a response settles (`htmx:afterSettle`), `bills.js` closes the drawer when the drawer’s HTML no longer contains a `form[data-bill-id]`, or when `detail.billId` equals the drawer’s `data-bill-id` **and** this response cleared the drawer. Strip confirm of bill A leaves bill B’s drawer alone: it does not send an empty drawer, and a `billId` that does not match the open drawer does nothing. `closeEditor()` is idempotent. It clears `data-bill-id` on the shell so a later event cannot match a stale id.

The client re-applies the current sort after these swaps, then `placeEditor()` if the drawer is still open.

### Create errors

Several create failures currently return **200** with a `<p class="text-red-600 text-sm">`. The form targets the rows, so a 200 replaces `#bills-rows` with that paragraph. Change every create validation failure to **422** with the same paragraph (name and provider, amount, frequency, start date, next due date, note cleaner, contract). `PaidFromError` is already 400. Use one helper for every create failure, including `PaidFromError` and the note cleaner: the paragraph, the status, and no `HX-Trigger`. The form’s `after-request` handler already copies an unsuccessful body into `#bills-message`. A `gwError` trigger would show the sentence twice.

`tests/test_bills.py` `test_error_on_missing_required_fields` and `test_error_on_invalid_amount` assert status 200. Update them to 422 in slice 2. The body text stays (“required”, “Invalid amount”).

### Save errors (the drawer stays open)

HTMX 2 will not swap a 400, 409, or 422 unless `htmx:beforeSwap` sets `shouldSwap`. `HX-Retarget` alone does not show the message. Out-of-band swaps run only after `shouldSwap` is true, so the 409 hidden-date input is swapped only when this handler runs and `#edit-due-was-{{ id }}` is already in the form.

`bills.js` does this:

```javascript
document.body.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (!xhr) return;
    var retarget = xhr.getResponseHeader("HX-Retarget");
    if (!retarget) return;
    if (retarget !== "#bill-editor-errors" && retarget.indexOf("#due-soon-errors-") !== 0) {
        return;
    }
    var node = document.querySelector(retarget);
    if (!node) return;
    event.detail.shouldSwap = true;
    event.detail.target = node;
});
```

If the node is missing, leave `shouldSwap` false. Do not swap the error into whatever the form was targeting.

Editor and strip validation errors use `_editor_error(message, status, focus=None)`:

- body is `<p class="gw-msg-error">…</p>` (the note text is still not echoed)
- `HX-Retarget: #bill-editor-errors` for a drawer error, or `#due-soon-errors-{{ id }}` for a strip error
- `HX-Reswap: innerHTML`
- no `HX-Trigger`, so `gwError` does not also paint the strip
- optional `HX-Focus: #some-id` when the route knows the field
- status stays 400, 409, or 422 as today

`#bill-editor-errors` is an empty `<div tabindex="-1" aria-live="assertive">` at the top of `_bill_editor.html`, outside both forms. `#due-soon-errors-{{ id }}` is the first child of the chip, in the idle chip and in the confirm view.

`from=dashboard` and `from=card` keep today’s error bodies and statuses. Do not retarget those. Unknown-frequency confirm and skip keep `_unknown_frequency_html`.

These `bills_update` returns all go through `_editor_error`. Today the four bare `HTMLResponse`s are invisible under htmx 2, and `_html_error` / `_note_html_error` would show the sentence twice if the body were also retargeted:

| Return | Status | Focus |
| --- | --- | --- |
| `BillFieldError` while reading importance, cancel note, or stopped note | 422 | `#importance-{{ id }}`, `#cancel-note-{{ id }}`, or `#stopped-note-{{ id }}`. The `except` already knows which key it was parsing. Both notes share the sentences “Don't put a card number here.” and “Keep this to 500 characters.”, so the sentence is not a field name. |
| Bad next due date | 400 | none |
| Unknown frequency (“Choose a frequency.”) | 400 | none |
| Payment method | 422, or `exc.status` | none |
| Contract | 422, or `exc.status` | none |
| `PaidFromError`, including the `ValueError` branch that builds one | 400 | none |
| `PaymentConflict` | 409 | none. Also the expected-date swap below. |

Confirm amount, non-positive amount, and due-date failures are 200 today (`test_confirm_invalid_amount`). For `from=strip`, and for `origin not in {"dashboard", "card", "strip"}`, return **422** through `_editor_error`. Strip retargets `#due-soon-errors-{{ id }}`. The other bills-page origins retarget `#bill-editor-errors`. Update that one status assertion in slice 2. The body still contains “Invalid amount”. Dashboard and card confirm errors stay 200.

A 409 from `cas_save_bill` (`PaymentConflict` “This schedule changed. Reload the page.”) keeps the drawer open, shows the message, and does not commit the other fields (the route already rolls back). Also out-of-band replace the hidden expected date so the next save is not an immediate second conflict. The input is already inside the form. htmx replaces it by id. Do not set `form=`.

```html
<input id="edit-due-was-{{ id }}" hx-swap-oob="outerHTML"
       type="hidden" name="next_due_date_was" value="{{ bill.next_due_date }}">
```

Reload the bill after rollback before reading `next_due_date`. Leave the visible date input as the person typed it. The next save’s expected date is the database cursor, and the posted `next_due_date` is their typed date. That is the compare-and-set they already have.

Focus after an error swap: if `HX-Focus` names an element inside the open `<details id="bill-mark-{{ id }}">`, set `open` on that details (this covers the importance sentence, the card-number sentence, and “Keep this to 500 characters.”) and focus the named control. Otherwise focus the retarget node (`#bill-editor-errors` or `#due-soon-errors-{{ id }}`).

## Drawer, compare-and-set, focus

“Optimistic” here means the compare-and-set that is already in `cas_save_bill`: one `UPDATE … WHERE id = ? AND next_due_date = expected_due`. Zero rows raises `PaymentConflict` and writes nothing. The page does not paint the new values before the response. There is no client-side optimistic render.

### Load

`GET /bills/{id}/edit` returns `_bill_editor.html` only. The line stays in the list. `bills.js` on `htmx:afterSwap` when the target is `#bill-drawer`:

- If the drawer has no `form[data-bill-id]`, call `closeEditor()` and stop. Do not read a missing id, do not unhide, do not focus.
- Otherwise:

1. Remember the bill id from the form’s `data-bill-id` onto `#bill-drawer`.
2. Set every other `[aria-expanded="true"]` back to “Edit” / `false`. Set this row’s `#bill-edit-{{ id }}` to “Close” / `true`.
3. `placeEditor()`.
4. Remove `hidden` from `#bill-drawer`. Above 800px, also unhide `#bill-drawer-backdrop` and set `role="dialog"`, `aria-modal="true"`, `aria-labelledby="bill-editor-title-{{ id }}"` on the drawer. Set `inert` on `main.gw-main` and on `#gw-sidebar`. At 800px and below, remove those dialog attributes and the `inert` on `main`, and leave the backdrop hidden. The nav’s own rule still sets `inert` on the sidebar while the menu is closed.
5. Focus the name field `#edit-name-{{ id }}`.
6. `syncPageOverflow()`: `document.body.style.overflow = "hidden"` while the drawer is `aria-modal` or the nav is open. Otherwise clear it.

`placeEditor()`:

- In-flow (800px and below, and at every width during slice 2): do not move the node. `#bills-stack` is `position: relative`. The drawer is `position: absolute; left: 0; width: 100%` and `bills.js` sets its `top` to the bottom of `#bill-row-{{ id }}` relative to `#bills-stack`. Set that row’s `margin-bottom` to the drawer’s `offsetHeight` plus an 8px gap, and clear `margin-bottom` on every other `.gw-bill-row`. The following rows move down. The form occupies the gap, under that row, and the drawer is still a sibling of `#bills-list`. Remeasure after sort, after a row or `#bills-rows` swap, on `resize`, and on `toggle` of a `<details>` inside the drawer.
- Fixed (above 800px, slice 3): clear the inline `top` and every row `margin-bottom`. CSS positions the drawer `fixed` on the viewport. DOM position does not matter. Leave the node where the page put it.

On `resize` across 800px, call `placeEditor()` again and add or remove the dialog attributes, the backdrop, and `inert` on `main`. Crossing above 800px starts the focus trap and moves focus to `#edit-name-{{ id }}`, so focus is inside the dialog it just became. Crossing below 800px stops the trap, removes `inert` from `main`, and calls `syncPageOverflow()` so a modal lock does not stick. Leave focus where it is on the way down. If the nav is open, overflow stays hidden.

Opening Edit on another bill replaces `#bill-drawer`’s HTML. Unsaved edits in the previous form are discarded. There is no “discard changes?” prompt.

### One form, then the confirm form

Wrap the edit fields in one form. The inputs do **not** use the `form="edit-form-…"` attribute. That attribute existed because the notes lived in a second `<tr>` outside the `<form>`. Tests that require the attribute are updated in slice 2.

The confirm form is the next sibling, after the edit form’s `</form>`. It is never nested. Save stays in the edit form. Do not put Save in the confirm form with `form=`.

```html
<div class="gw-drawer">
  <h3 id="bill-editor-title-{{ id }}">Edit {{ bill.name }}</h3>
  <p class="gw-text-muted">…provider, frequency, source…</p>
  <div id="bill-editor-errors" tabindex="-1" aria-live="assertive"></div>
  <form id="edit-form-{{ bill.id }}" data-bill-id="{{ bill.id }}"
        hx-post="/bills/{{ bill.id }}{{ query }}"
        hx-target="#bill-row-{{ bill.id }}" hx-swap="outerHTML">
    <!-- fields, collapsed mark section, Save, Cancel, Delete -->
  </form>
  <!-- only when needs_confirm(bill) -->
  <form id="confirm-form-{{ bill.id }}"
        hx-post="/bills/{{ bill.id }}/confirm?from=editor{{ marks_query }}"
        hx-target="#bill-row-{{ bill.id }}" hx-swap="outerHTML">
    <!-- This occurrence: amount, hidden due_date, Confirm, Skip -->
  </form>
</div>
```

Skip is `type="button"` inside the confirm form, with `hx-post="…/skip?from=editor"`, `hx-vals` set to the stored `due_date`, and `hx-confirm="Skip the {{ date|due_label }} payment?"`. htmx 2.0.4 includes the closest form on a non-GET. Inside the confirm form, Skip posts that form’s `due_date` (and its amount, which `bills_skip` ignores). It does not post the edit form’s `name` or anything else in the edit form. Do not put Skip in the edit form.

Delete is `type="button"` `hx-delete="/bills/{{ bill.id }}{{ query }}"` `hx-target="#bill-row-{{ bill.id }}"` `hx-swap="delete"` `hx-confirm="Deactivate this bill?"` class `gw-action-delete`. It sits next to Save and Cancel, inside the edit form, so it does not submit that form. On the quiet view the delete URL includes `marks=1`.

Confirm and Skip use the **stored** occurrence. They do not save the rest of the form first. The amount in the confirm form is its own input, prefilled from `bill.amount`, independent of the edit form’s amount. If the person edited the amount and then presses Confirm, the payment uses the confirm form’s amount and the other edits are dropped when the drawer closes. The section is labelled “This occurrence”. When the date is after today, the line reads “Confirm or skip the {{ date|due_label }} payment” so an early confirm is obvious.

Render the confirm form only when `needs_confirm(bill)`. A fixed Bills-fund bill does not show it. Posting confirm for that bill is still the service’s 409 “This bill is paid automatically from the Bills fund.” That 409, on a bills-page origin, goes through `_editor_error` and stays in the drawer.

### Editor fields

Title: `<h3 id="bill-editor-title-{{ id }}">Edit {{ bill.name }}</h3>`. Under it, muted: provider, frequency label, `source_label`.

Then `#bill-editor-errors`. Then a `.gw-grid-2` inside the edit form. Inside the fixed drawer it stays two columns. At 768px and below, the page rule stacks it to one column, which is the phone form and also the fixed drawer’s grid only if the viewport is under 768 — the fixed drawer exists only above 800, so it stays two columns:

| Field | Control | Notes |
| --- | --- | --- |
| Name | `name="name"` `id="edit-name-{{ id }}"` `required` | Blank name is still ignored server-side (`if name:`). Do not “fix” that. |
| Provider | `name="debtor_provider"` | Blank provider is still ignored. |
| Contract | `name="contract_id"` select, full width (`grid-column: 1 / -1`) | “No contract” plus `contract_choice_label` (ended contracts show `(Ended)`). The visible help is “Earlier payments stay off the plan.” The long sentence stays the select’s `title`: “Only payments from now on are tied to this contract. Earlier ones stay off the plan.” The add form keeps the long sentence as a paragraph. |
| Paid with | `name="payment_method_id"` select, full width | “Not set” plus `choice_label`. Same choice list as today (`payment_method_choices`). |
| Paid from | `name="paid_from"` select | `paid_from_options` macro, moved with the editor. `onchange="gwPaidFromChanged(this)"`. |
| Amount | `name="amount"` | Help stays on the add form only (“Amount each time it's due.”). |
| Category | `name="category_id"` inside `[data-fund-category]` | Shown and enabled only when paid from a sinking fund. Same options as today. |
| Foreign amount | `name="foreign_amount"` | Optional. Still display-only in the maths. |
| Currency | `name="foreign_currency"` `maxlength="3"` | Uppercase. Cleared when the foreign amount is empty, as today. |
| Frequency | `name="frequency"` | `frequency_labels`, plus the raw value if it is unknown, so a bad stored value is visible. |
| Bill type | `name="bill_type"` inside `[data-bill-type]` | Fixed / Variable. Shown only for the Bills fund. `onchange="gwPaidFromChanged(this.closest('form').querySelector('[name=paid_from]'))"`. There is no `<tr>` in the drawer. `closest('tr')` throws and leaves the overdue sentence stale. |
| Next due | `name="next_due_date"` `type="date"` | |
| Expected due | `<input id="edit-due-was-{{ id }}" type="hidden" name="next_due_date_was" value="{{ bill.next_due_date }}">` | The compare-and-set token. A missing or invalid date is still 400 and saves nothing. |

`[data-overdue-note]` and `[data-confirm-help]` stay, driven by `gwPaidFromChanged`. Copy stays exact:

- Bills fund, variable: “Overdue payments will wait in Due to confirm.”
- Bills fund, fixed: “Overdue payments will then be paid from the Bills fund automatically.”
- Anything else: “You'll confirm each one before it's logged.” (`[data-confirm-help]`)

`gwPaidFromChanged` resolves its root with `select.closest("form") || select.closest("#bill-drawer") || select.closest("tr")`. The add form is already a `<form>`, so that path keeps working. Assign it as `window.gwPaidFromChanged` from `bills.js`. The bill-type handler above uses `closest('form')` and does not depend on a `<tr>`.

Then the collapsed mark section, still inside the edit form. Then `</form>`. Then the “This occurrence” confirm form when `needs_confirm`. Save (`gw-btn gw-btn-success gw-btn-sm`), Cancel (`gw-btn gw-btn-ghost gw-btn-sm`, `type="button"`), and Delete are the last controls inside the edit form, before the confirm form.

### Collapsed mark section

`<details class="gw-details" id="bill-mark-{{ id }}">` with `<summary>If something happens to me</summary>`. Closed on both pages, including when a note is already stored. Contents, all enabled so they post:

- Select `name="importance"` `id="importance-{{ id }}"`. Options: blank “Not set yet”, `essential` “Keep paying”, `optional` “Safe to cancel”.
- The existing helper: “Both notes are optional on every payment. Write where to cancel it, and what goes wrong if it stops.”
- Textarea `name="cancel_note"` `id="cancel-note-{{ id }}"` `maxlength="500"` label “How to cancel”.
- Textarea `name="if_stopped_note"` `id="stopped-note-{{ id }}"` `maxlength="500"` label “What happens if this stops”.

A missing key still leaves the stored value. The section is always in the form, so the keys are always posted and a blank select or a blank note still clears. Do not disable the textareas when the mark is “Not set yet”. A closed `<details>` still submits enabled inputs.

### Close, Esc, focus trap

`closeEditor()` reads `data-bill-id` first, then empties `#bill-drawer`, sets `hidden` on the drawer and the backdrop, removes `role`, `aria-modal`, and `aria-labelledby`, removes `inert` from `main.gw-main`, clears `data-bill-id`, clears every row `margin-bottom` and the drawer’s inline `top`, sets the Edit button back to “Edit” / `aria-expanded="false"`, and calls `syncPageOverflow()`. It does not clear overflow itself when the nav is still open.

Focus: if this close came from a click, Esc, or the backdrop, focus `#bill-edit-{{ id }}` immediately, or `#bills-sort` if that button is gone. If this close came from a swap (empty drawer, or a `billId` match on a response that cleared the drawer), wait for `htmx:afterSettle` of that response so the replacement Edit button is in the DOM, then focus it, or `#bills-sort` if the row was deleted or filtered out.

Cancel, the row’s Close button, the backdrop click, and Esc all call `closeEditor()`. Esc does not save. Listen on `keydown` while the drawer has a form. Esc inside a textarea still closes (it is not a newline). A native `hx-confirm` dialog swallows Esc before the page sees it, so Skip and Delete confirms are safe.

When the menu is open, one Esc closes the menu only, even if the inline form is also open. A second Esc closes the form. Above 800px the menu is not open, so Esc closes the drawer.

Focus trap, only while `aria-modal="true"`:

- Tab on the last focusable inside `#bill-drawer` wraps to the first.
- Shift+Tab on the first wraps to the last.
- Focusables: `a[href]`, `button:not([disabled])`, `input:not([disabled])`, `select:not([disabled])`, `textarea:not([disabled])`, `[tabindex]:not([tabindex="-1"])`. Skip any of those that sit inside `details:not([open])`, except the `<summary>`. Skip anything inside `[hidden]`. The same filter is used for the nav trap.
- `inert` on `main.gw-main` and `#gw-sidebar` while the dialog is open, removed in `closeEditor()` (the nav rule may put `inert` back on the sidebar if the viewport is narrow and the menu is closed). `aria-modal` alone does not keep browse-mode out.
- Vanilla JS in `bills.js`. No new dependency.

At 800px and below there is no trap, no `aria-modal`, and no `inert` on `main`. Tab can leave the form. Esc still closes and returns focus to Edit. Opening the form still focuses the name field.

After a successful save, the row swap plus the empty-drawer out-of-band runs the settle focus return. After a 4xx, focus stays in the drawer, on the error or the field named by `HX-Focus`.

## Off-canvas navigation

All of this is in `app/templates/base.html` and `app/static/styles.css`. Every page that extends `base.html` gets it. Login does not. Slice 1 is this section and nothing else.

Markup added to `base.html`, as siblings of `#gw-sidebar`, not descendants. The collapse button is already outside the nav. Copy that. A Menu button inside the sidebar cannot be pressed: the closed sidebar is `inert`.

```html
<button type="button" id="gw-menu-btn" class="gw-menu-btn"
        aria-controls="gw-sidebar" aria-expanded="false">Menu</button>
<div id="gw-nav-backdrop" class="gw-nav-backdrop" hidden></div>
```

Place both after `</nav>` and before `<main>`. `#gw-sidebar` and `#gw-collapse-btn` stay, including the `localStorage` key `sidebar-collapsed` and the `collapsed` class. Desktop (801px and wider) is the sidebar as it is today: 240px, or 60px when collapsed, collapse button on the edge, `.gw-main` margin follows. Menu is not displayed.

At `max-width: 800px` the panel is the full menu, not the icon rail. `.gw-sidebar.collapsed` is more specific than a bare `.gw-sidebar` rule, and it is restored from `localStorage` on every page. The media query has to override these rules (they live in `styles.css` today):

- `.gw-sidebar.collapsed { width: var(--sidebar-collapsed-width); }` — `--sidebar-collapsed-width` is 60px. The narrow rule sets width to `var(--sidebar-width)` (240px) on `.gw-sidebar` and on `.gw-sidebar.collapsed`.
- `.gw-sidebar.collapsed .gw-sidebar-label { max-width: 0; opacity: 0; }` — restore `max-width: 160px` and `opacity: 1`.
- `.gw-sidebar.collapsed .gw-nav a { justify-content: center; padding: 10px; }` — restore `justify-content: flex-start` and `padding: 10px 14px`.
- `.gw-sidebar.collapsed .gw-user-info, .gw-sidebar.collapsed .gw-signout-btn { display: none; }` — show them again (`.gw-user-info` as `block`, `.gw-signout-btn` as `flex`). Sign out lives inside `#gw-sidebar`. Without this override a desktop collapse hides Sign out on every narrow page.
- `.gw-sidebar.collapsed .gw-logo { padding: 20px 14px; }` — restore the open logo padding `24px 20px 20px`.
- `.gw-sidebar.collapsed .gw-user-profile { justify-content: center; }` — restore `flex-start`.

The override block comes after those rules in the file, so equal specificity still wins. Selectors inside the media query repeat `.gw-sidebar.collapsed …`. A rule on `.gw-signout-btn` alone does not beat `.gw-sidebar.collapsed .gw-signout-btn`.

Also at `max-width: 800px`:

- `.gw-sidebar` is `position: fixed` (it already is), `transform: translateX(-100%)`, including when it has `.collapsed`.
- `.gw-sidebar.is-open` and `.gw-sidebar.collapsed.is-open` set `transform: none`.
- `.gw-main` and `.gw-sidebar.collapsed ~ .gw-main` have `margin-left: 0` and `padding-top: 56px`. The 768px rule sets `.gw-main { padding: 20px 16px }`. The 800px block is later in the file and sets `padding-top` only, so horizontal padding stays. 56px clears the fixed Menu button. Do not retune 768 to 800. Between 769px and 800px the desktop horizontal padding remains and only the top padding changes.
- `.gw-collapse-btn` is `display: none` (out of tab order). Do not delete the button or the click listener. `collapseBtn.addEventListener` runs on every base page today and the button is still in the DOM.
- `.gw-menu-btn` is `display: inline-flex`, `position: fixed`, `top: 12px`, `left: 12px`, `z-index: 110`. Give it a surface background, a border, and padding so it is visible on the dark page.
- While `.gw-sidebar.is-open`, `#gw-menu-btn` moves to `left: calc(var(--sidebar-width) + 12px)`. It stays a second press of Menu and it does not cover the logo. The selector is `.gw-sidebar.is-open ~ #gw-menu-btn`, which is why the button follows the sidebar in the document.
- `#gw-nav-backdrop` is `position: fixed; inset: 0; z-index: 90; background: rgba(0,0,0,0.35)`.

`transform` does not remove links from the tab order. On `DOMContentLoaded` and on `matchMedia("(max-width: 800px)")` changes:

- Under 800px, while the menu is closed, set `inert` and `aria-hidden="true"` on `#gw-sidebar`.
- When it opens, remove both, set `aria-expanded="true"` on the button, unhide the backdrop, call `syncPageOverflow()`, and focus the first `.gw-nav a`.
- Esc, backdrop click, and a second press of Menu call `closeNav()`.
- `closeNav()` moves focus to `#gw-menu-btn` **before** it sets `inert` and `aria-hidden`, when focus is inside the sidebar or the close was a user action (Esc, backdrop, Menu). Then it removes `is-open`, restores `inert` and `aria-hidden` if the viewport is still narrow, hides the backdrop, sets `aria-expanded="false"`, and calls `syncPageOverflow()`. Setting `aria-hidden` on the focused element is the bug this order avoids.
- While open, trap Tab inside `#gw-sidebar` with the same focusable filter as the drawer (nav links, the sign-out button).
- When the viewport grows past 800px, `closeNav()` removes `inert` and `aria-hidden` so the visible sidebar is tabbable, and does not move focus. The sidebar is still on screen, including when focus is already inside it. `localStorage` `sidebar-collapsed` is left as the person set it. Narrowing into 800px closes an open menu and applies `inert`. If focus is inside the sidebar, move it to `#gw-menu-btn` first, then set `inert`. If focus is elsewhere, leave it.

Z-index: nav backdrop 90, sidebar 100 (already), collapse button 101 (already), menu button 110, drawer backdrop 140, drawer 150. The open menu button sits just to the right of the 240px panel, above the backdrop. The drawer covers the menu button when both could show; under 800px the drawer is the in-flow form, so they do not overlap. `body::before` (the noise overlay, `z-index: 9999`, `pointer-events: none`) stays above the paint and does not take clicks.

`syncPageOverflow()` sets `overflow: hidden` when the narrow menu is open or the drawer is `aria-modal`. Otherwise it clears the inline overflow. `closeNav()` and `closeEditor()` both use it. Slice 1 has no drawer; the helper still checks for `aria-modal` so slice 3 does not grow a second copy.

Existing grid breakpoints stay. `.gw-grid-2/3/4` still stack at `max-width: 768px`.

## CSS

Put the new rules in `app/static/styles.css`. Do not add a second stylesheet. The nav rules land in slice 1. The bills rules land with the slice that first needs them.

Move the three rules that today live in a `<style>` block in `bills.html` into `styles.css` in slice 2, so the strip, the drawer, and the dashboard share them:

- `.gw-badge-variable` — blue italic badge, as written today (`#dbeafe` / `#1d4ed8`)
- `.gw-action-pay` — green confirm button, as written today
- `.gw-foreign-amount`

Moving `.gw-action-pay` also styles the dashboard card’s Confirm button, which does not have those rules today. Glance at that card when slice 2 moves them.

`gw-contract-label` has no rule today. Leave it that way. The editor uses a select, not that class.

New classes, and only these:

| Class | Role |
| --- | --- |
| `gw-menu-btn` | Menu button. Hidden from 801px up. Slice 1. |
| `gw-nav-backdrop` | Off-canvas scrim. Slice 1. |
| `gw-sidebar.is-open` | Slides the existing sidebar in. State class, not a new component. Slice 1. |
| `gw-due-soon` | Horizontal scroller for the chips. |
| `gw-due-chip` | One chip. `flex: 0 0 auto`, min-width about 220px. Use `gw-card` as well. |
| `gw-bill-list` | The rows. No table, no horizontal scroll. |
| `gw-bill-row` | One payment. `border-bottom: 1px solid var(--border-dim)`, padding about 10px 0. |
| `gw-bill-line` | Flex row: name block grows, amount, Edit. `gap: 12px`, `align-items: baseline`. |
| `gw-bill-meta` | The second line. `gw-text-muted` as well. |
| `gw-drawer` | The editor shell. |
| `gw-drawer-backdrop` | Light scrim `rgba(0,0,0,0.35)`, fixed, only above 800px. |

Reuse, do not restyle globally: `gw-stat`, `gw-stat-label`, `gw-stat-value`, `gw-stat--amber`, `gw-stat--green`, `gw-card`, `gw-badge`, `gw-badge--paid`, `gw-badge--pending`, `gw-badge--quiet`, `gw-btn`, `gw-btn-success`, `gw-btn-ghost`, `gw-btn-sm`, `gw-action-edit`, `gw-action-delete`, `gw-action-pay`, `gw-details`, `gw-input`, `gw-select`, `gw-edit-input`, `gw-label`, `gw-text-muted`, `gw-text-dim`, `gw-text-negative`, `gw-grid-2`, `gw-grid-3`, `gw-page-header`, `gw-section`, `gw-msg-success`, `gw-msg-error`.

Drawer, `min-width: 801px` and `:not([hidden])` (slice 3):

- `position: fixed; top: 0; right: 0; height: 100vh; overflow: auto; z-index: 150`
- `width: min(420px, 100vw)`
- `background: var(--surface-1); border-left: 1px solid var(--border-subtle); padding: 24px 20px 40px`
- `.gw-grid-2` inside `.gw-drawer` stays two columns

Drawer, `max-width: 800px` (slice 2 applies this placement at every width; slice 3 scopes it to this breakpoint):

- `position: absolute; left: 0; width: 100%; z-index: 1` inside `#bills-stack`
- the card look of `gw-card` (border, radius, padding)
- the 768px rule stacks `.gw-grid-2` to one column

`[hidden]` on `#bill-drawer`, `#bill-drawer-backdrop`, and `#gw-nav-backdrop` is `display: none !important` so an open-state `display` cannot override the attribute. Slice 1 adds the backdrop rule. Slice 2 adds the drawer rules.

The daily page content keeps `max-width: 1200px`. The fixed drawer is on the viewport, not inside that box.

## Field and action map

Every current field is still reachable. `show_marks` is false on `/bills` and true on the quiet view.

| Today | Daily closed line | Quiet closed line | Editor | Add new |
| --- | --- | --- | --- | --- |
| View Forecast | Header link | Absent | — | — |
| Add new, including start date | — | Absent | — | Collapsed panel. Start date stays here only. |
| Name | `<strong>` | `<strong>` | Text input | Text input |
| Provider | Meta line | Meta line | Text input | Text input |
| Contract, and `(Ended)` in the option | Absent | Absent | Select. Visible “Earlier payments stay off the plan.” Long sentence is the `title`. | Select. Long sentence is the paragraph. |
| Ended badge | `gw-badge gw-badge--pending` “Ended” when `contract.ended_on` | Same | The option label | — |
| Paid with label | Absent | Absent | Select | Absent, as today. The add form has no payment method. |
| Expires soon, Expired, To move | `gw-badge--pending` with that text, when `paid_with[id].badge` is set | Same | — | — |
| Not set | Absent. The chip counts these. | Absent | “Not set” option | — |
| Paid from | Meta line, negative when broken | Same | Select, plus category when it is a sinking fund | Select, plus category |
| Amount | End of the line | Same | Number input | Number input, with “Amount each time it's due.” |
| Foreign amount and currency | Absent | Absent | The two inputs | The two inputs, with the “note only” help |
| Frequency | Meta line | Same | Select | Select |
| Fixed | Absent | Absent | Select option, Bills fund only | Select option |
| Variable | `gw-badge-variable` “Variable” when Bills fund and `bill_type == "variable"` | Same | Select option | Select option |
| Confirm (the type word) | `gw-text-muted` “Confirm” when `paid_from != 'bills_fund'` | Same | The confirm help sentence | The confirm help sentence. No overdue note. |
| Next due | `<time>` with `due_label`, negative when overdue | Same | Date input, plus the hidden expected date | Start date and next due date |
| Keep paying | Absent | `gw-badge gw-badge--paid` | Collapsed select | Collapsed select |
| Safe to cancel | Absent | `gw-badge gw-badge--quiet` | Collapsed select | Collapsed select |
| Not set yet | Absent | `gw-text-muted` “Not set yet” | Collapsed select | Collapsed select, the default |
| How to cancel | Absent | Full text when non-null | Textarea in the collapsed section | Textarea in the collapsed section |
| What happens if this stops | Absent | Full text when non-null | Textarea in the collapsed section | Textarea in the collapsed section |
| Overdue / confirm help | — | — | `gwPaidFromChanged` copy, unchanged | Confirm help only. Leave the overdue node off. |
| Confirm this occurrence | Strip, only if it is in `due_to_confirm` and there is no problem | Absent on the line. Present in the editor when `needs_confirm`. | Amount form, any date, including a future one. Sibling form, after Save. | — |
| Skip this occurrence | Strip, same condition, including when there is a problem. A fixed chip has no Skip. | Same as Confirm | Button inside the confirm form, posts `due_date` | — |
| Fixed chip line | “Pays from the Bills fund”, or “Not logged yet. It pays from the Bills fund.” when the date is before today | — | — | — |
| Variable chip, not yet due | “Not due for a decision yet” | — | — | — |
| `+ N more` | Strip chip only | — | — | — |
| Missing-budget / deleted-source / missing Bills fund | Strip, the detail once, one link, per the link rules | — | — | — |
| Edit | Button on the line | Button on the line | — | — |
| Save | — | — | Submit, inside the edit form | “Add Bill” |
| Cancel | Close on the line while open, and Cancel in the form. Client-side. No `close_edit`. | Same | Button | — |
| Delete | — | — | “Delete”, confirm “Deactivate this bill?” | — |
| Column sort | Select plus Reverse | Same select | `placeEditor` reserves the gap under the row. The drawer is not inside the list. | — |
| `?payment_method=` | Filters the list. Summary cards stay global. | Honoured and ANDed with `importance` | Posted back on the form action | — |
| `?importance=` | Slice 4: 303 to the quiet view. Until then it still filters `/bills`. Empty is ignored. | Filters the list. Cards stay global. | Posted back. A save that no longer matches deletes the line. | — |
| No payment method chip | On the page | Absent | — | — |
| No keep-or-cancel chip and the nudge sentence | Absent | The unmarked count is the “Not set yet” filter link. The nudge sentence is not brought back. | — | — |

`end_date` is not on the HTML form today. Do not add it.

## Keyboard and accessibility

Covered in the drawer and nav sections. Checklist for the implementer:

- Menu: `aria-controls="gw-sidebar"`, `aria-expanded`, `inert` and `aria-hidden` on the sidebar while it is off-canvas and closed, Esc closes the menu before the drawer, Tab cycles inside the open sidebar, focus moves to `#gw-menu-btn` before `inert`.
- Edit: `aria-expanded`, `aria-controls="bill-drawer"`. The button name toggles Edit / Close.
- Drawer above 800px: `role="dialog"`, `aria-modal="true"`, `aria-labelledby` the title, `inert` on `main.gw-main` and `#gw-sidebar`, Tab trapped, Esc closes without saving, focus starts on the name field and returns to that row’s Edit button (or `#bills-sort` if the row is gone). Crossing up into this mode moves focus into the dialog.
- Drawer at 800px and below: no dialog role, no trap, no `inert` on `main`, Esc still closes, focus still starts on the name and returns to Edit. Crossing down restores overflow unless the menu is open.
- Focusables skip descendants of a closed `<details>` except `<summary>`.
- `#bill-editor-errors` and `#due-soon-errors-{{ id }}` are `tabindex="-1"` and `aria-live="assertive"`. `#bills-message` is `aria-live="polite"`.
- The sort select has `aria-label="Sort payments"`. Reverse has `aria-pressed`.
- Dates use `<time datetime="YYYY-MM-DD">`.
- Do not rely on colour alone for overdue: the date text is the date, and the negative colour is extra. The mark on the quiet view is a word, not only a dot.

## Tests

There is no Playwright, Selenium, or browser suite in `pyproject.toml`. Do not add one. Pytest covers the HTML contract. The 1280 and 390 checks are manual, once per slice that changes layout.

Run, from the repo root, after each slice: `uv run pytest` on the files that slice touches, then `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pyrefly check app/`. Each slice also runs the full `uv run pytest` before its pull request. A slice does not rely on a later slice’s tests, and it updates the existing tests its own behaviour breaks. The suite is green at the end of every slice.

`tests/test_bills.py` `variable_bill` is `next_due_date="2026-02-01"`, which is overdue on 2026-11-14, so `test_variable_row_has_confirm_and_skip_not_pay` still sees Confirm on the strip. Do not move that fixture to a future date to “fix” it. The warning applies only if a fixture’s next due is in the future: the strip buttons `due_to_confirm` only. Point that fixture at yesterday, or open the edit form and assert Confirm there. Do not put Confirm back on the closed line to save the test.

### Slice 1 — `tests/test_nav.py`

No bills assertions change.

- `GET /bills` and `GET /` contain `id="gw-sidebar"`, `id="gw-menu-btn"`, `id="gw-nav-backdrop"`, and `id="gw-collapse-btn"`.
- The menu button and the backdrop are not inside `#gw-sidebar` (the button markup follows `</nav>`, or the sidebar’s inner HTML does not contain `gw-menu-btn`).
- `GET /login` contains none of `gw-menu-btn`.
- The menu button has `type="button"` and `aria-controls="gw-sidebar"`.
- A page that extends `base.html` still contains `gw-collapse-btn` and the sign-out control (`gw-signout-btn` or `hx-post="/logout"`).
- `app/static/styles.css` has a `max-width: 800px` block that overrides `.gw-sidebar.collapsed .gw-signout-btn` and `.gw-sidebar.collapsed .gw-user-info` so they are not `display: none`, and sets `.gw-main` `padding-top` inside that block. A collapsed-rail screenshot cannot be done in pytest. The manual check collapses on desktop first, then narrows, and presses Sign out.

### Slice 2 — list, strip, cards, in-flow editor

Update these existing tests in this slice. Do not leave them failing for slice 4.

| Test | What slice 2 asserts |
| --- | --- |
| `tests/test_bills.py::TestPaidWith::test_column_badge_and_sort_map` | `/bills` shows the Expired badge (`>Expired<`) and `No payment method: 1`. `GET /bills/{id}/edit` shows “Paid with”, “Everyday Visa ending 4321”, and a “Not set” option. The sort select has `value="paid_with"`, `value="paid_from"`, and `value="next_due_date"`. |
| `tests/test_bills.py::test_amount_help_summary_and_success_event` | “Amount each time it's due.” stays. “Bills fund monthly” and “Other recurring” are on the page. “Feeds the forecast” is on the page. The trigger still contains “Bill added.”, `monthly`, `annual`, and `other`. “Choose a frequency.” stays, now with status 422. |
| `TestBillsPagePost::test_error_on_missing_required_fields` and `test_error_on_invalid_amount` | Status 422. Body still contains “required” / “Invalid amount”. No `HX-Trigger`. |
| `test_html_and_json_set_and_clear_the_mark` | A save without `marks=1` returns the line and the name, and does not contain “Keep paying” or `bill-notes-`. JSON omit and clear are unchanged. The `marks=1` half is slice 4. |
| `test_page_splits_averages_and_filters` | Split the test. Slice 2 keeps the `/bills` half: the page does not contain “Split of the monthly averages”, the “still paid until” sentence, “No keep-or-cancel mark”, `bills-essential-cost`, `bills-optional-cost`, `bills-unmarked-cost`, `bills-unmarked-count`, `bills-unmarked-note`, `gw-badge--paid`, `gw-badge--quiet`, “If this stops: Mortgage stops”, or “How to cancel:”. The editor GET has `<form id="edit-form-{{ id }}"`, both textareas, the summary “If something happens to me”, and the helper sentence. It does not have `close_edit=1`, `colspan="9"`, or `form="edit-form-`. The trigger on a mark-only change still has `essential`, `optional`, `unmarked`, and `unmarked_count`. A note-only save of a row that stays listed still has no `hx-trigger-after-swap`. Filtering by `?importance=` still works on `/bills` in this slice. Assert which rows remain and assert `HX-Reswap: delete` by the header. Do not assert `"Keep paying" in stayed.text`. Do not assert `"Loose" not in marked.text`: Loose is an overdue fixed bill and its name is in the strip. The quiet-view sentences, the three pile ids, and both note lines move to slice 4. |
| `test_create_and_delete_send_the_mark_totals` | The trigger payload assertions stay, including `optional` and `unmarked_count` on create and delete. `/bills` does not contain `bills-unmarked-note`. The quiet-view count is slice 4. |
| `TestBillsConfirmRow::test_confirm_invalid_amount` | Status 422, body contains “Invalid amount”, `HX-Retarget` is `#bill-editor-errors` (the default `from` is `"bills"`). |
| `tests/test_contracts.py` around the `colspan="9"` confirm assertion | The confirm GET still contains the amount input, `due_date`, and “From”. `>Ended<` stays on `/bills`. `(Ended)` stays on the edit GET. |
| `tests/test_recurring_payments.py::TestOccurrenceTransitions::test_stale_html_edit_saves_nothing` | `<form id="edit-form-{id}"` and `name="next_due_date_was"` are in the edit GET. The 409 and 400 statuses, and the “nothing was written” database checks, stay. |
| `TestBillsDelete::test_returns_empty_response` | The body is not `""`. It includes an out-of-band `#due-soon` and an out-of-band `#bill-drawer`. The delete still removes the row. |

Assertions that should stay green without edits, checked in slice 2:

- `TestBillsPageGet::test_renders_page_with_table` — “Name”, “Provider”, “Amount”, “Frequency”, and “Next Due” remain on the add form (“Next Due Date” contains “Next Due”).
- `test_lists_active_bills`, `test_excludes_inactive_bills` (`>Rent<` / `>Internet<`), `test_shows_add_form`, the forecast-link tests.
- `test_returns_updated_table_body` — the rows partial still contains the new name and “Rent”. Its target is `#bills-rows`.
- `TestBillsConfirmRow::test_variable_row_has_confirm_and_skip_not_pay` — a variable bill that is due or overdue is on the strip, so “Confirm” and “Skip” are on `/bills`. `>Pay<` and `/pay` stay absent.
- `test_confirm_form_shows_due_date_and_source` — `_bill_confirm.html` keeps “Due 1 Feb 2026”, `name="amount"`, `name="due_date"`, “From Bills fund”, and not “Record Payment”.
- `tests/test_every_2_years.py::test_bills_page_shows_amount_over_24` — “Every 2 Years” is the frequency label on the new line. The trigger substrings and `id="bills-monthly-cost">$10.00` / `id="bills-annual-cost">$120.00` stay if the dollar sign remains inside those two ids.
- `tests/test_every_2_years.py` frequency save (about line 395) stays **400** and “Choose a frequency.” Slice 2 adds `HX-Retarget: #bill-editor-errors` and no `HX-Trigger` on that response. The test’s status assertion stays.
- `tests/test_every_2_years.py` unknown-frequency confirm and skip (about lines 411–425) stay **400**, “Unknown bill frequency: nope”, and not “Invalid due date”. They keep `gwError`. Do not retarget them.
- `tests/test_dashboard.py::TestDueToConfirmCard` — both tests. They read `/` and `id="due-to-confirm"`. Do not change `_due_to_confirm.html`.
- `tests/test_cli.py` Mark column. `tests/test_sinking_funds.py` `gw-summary-bar`. Service tests for `due_to_confirm`, confirm, skip, and the deposit. Audit tests. MCP tool count.
- `test_picker_filter_and_api_clear` uses `>Rent<` and “No payment method: 0”. Both survive if the name element and the chip stay.

New tests in slice 2, in `tests/test_bills.py` unless noted:

- `/bills` contains `id="due-soon"`, `id="bills-rows"`, `id="bills-list"`, `id="bill-drawer"`, `id="bills-sort"`, `id="bills-count"`, and the three forecast ids. `#bill-drawer` is not inside `#bills-rows`. The add form’s `hx-target` is `#bills-rows`. It does not contain `id="due-to-confirm"` or a `<table>`. It does not yet have to contain the quiet-view link.
- The page’s `#bills-rows` and `#bill-drawer` are both present, so a create that swaps `#bills-rows` does not remove the drawer. Assert that structure. A following `GET /bills/{id}/edit` is not a substitute.
- A fixed Bills-fund bill due within 10 days is inside `#due-soon`, that chip has no Confirm button, and the chip text is “Pays from the Bills fund” when the date is today or later, or “Not logged yet. It pays from the Bills fund.” when the date is before today. The line does not contain the word Fixed. The chip contains `id="due-soon-errors-{{ id }}"`.
- A budget bill due yesterday is a chip with Confirm and Skip. A budget bill due in 30 days is not a chip and the closed line has no Confirm button. `GET` edit of that future bill contains a confirm form after the edit form, Skip inside the confirm form, and `name="due_date"` with that future date. The edit form’s `name="name"` input is not inside the confirm form.
- A budget bill with no budget shows the detail sentence once inside the chip: the text “Set it on the Budgets page” occurs once in that chip.
- `gw-badge--pending` on `/bills` is only Expired / Expires soon / To move / Ended. A needs-confirm bill does not wrap the word Confirm in `gw-badge--pending`.
- The row carries `data-sort-next-due="{{ date }}"`. Do not import a JS runtime.
- Create with `next_due_date` yesterday returns the rows and a `#due-soon` element that includes the new name (out-of-band markup is in the same response body). The response is not a replacement of `#bill-drawer`.
- Delete response includes an out-of-band `#due-soon` and an out-of-band `#bill-drawer`, and the `gwBillSuccess` payload includes `billId`.
- A successful save payload includes `billId` when the tuple changed. The response includes an empty `#bill-drawer` out-of-band swap. The returned line’s Edit button reads “Edit”.
- Editor Skip markup posts `due_date` and is inside `confirm-form-`, not inside `edit-form-`.
- Stale edit returns 409, `HX-Retarget` is `#bill-editor-errors`, there is no `HX-Trigger`, and the body contains an `edit-due-was-` input whose value is the stepped date and which does not have `form="edit-form-`. The name was not saved.
- A 400 frequency save and a 422 contract save on the HTML edit include `HX-Retarget: #bill-editor-errors` and no `HX-Trigger`.
- `from` absent and `from=bills` on confirm and skip refresh the strip and the row, include `billId`, and do not mention `due-to-confirm`.
- `due_soon` includes an overdue fixed bill and a bill due on `today + 10`, and excludes `today + 11` and a bill past `end_date`. `due_to_confirm` is unchanged: fixed Bills-fund bills are still absent. `GET /api/bills/due` still matches `due_to_confirm`. This can live in `tests/test_recurring_payments.py` or `tests/test_bills.py`.

Slice 2 does not add the quiet view, `marks=1`, or the 303 test. Those wait for slice 4.

### Slice 3 — drawer above 800px

CSS and `bills.js` only. No new route behaviour, so no new status assertions. The focus trap, the resize overflow, and `inert` are manual. Do not add a browser runner.

### Slice 4 — quiet view

New and moved assertions:

- `GET /bills?importance=unset` is 303 to `/bills/if-something-happens?importance=unset`. `GET /bills?importance=` stays on the daily page. `GET /api/bills?importance=unset` stays 200.
- `/bills` contains the link to `/bills/if-something-happens` and still has none of the mark chrome from the slice 2 list.
- The quiet view has the three pile ids, the unmarked count, both notes in full (including a note the daily page used to hide), both badge classes, and “Not set yet” with no `gw-badge--pending` on that phrase. “Hidden on the list” is present when that is the stored note. `10,012.00` stays absent (inactive).
- `?importance=unset` hides marked rows and leaves `bills-essential-cost` at the full-list figure.
- `payment_method=none` ANDs with `importance=unset`.
- Unknown `importance` shows every row and Show all.
- Marking a row on `?importance=unset` returns `HX-Reswap: delete`. Assert the header. The body does not need to hide the name, and it must not contain a `#due-soon` swap.
- `show_marks` / `marks=1` round-trip: the returned line contains the badge. A save without `marks=1` does not.
- After the delete in `test_create_and_delete_send_the_mark_totals`, the quiet view’s `#bills-unmarked-count` is `0`.
- A quiet-view save or delete response has no `#due-soon` out-of-band fragment.

Update any slice 2 filter test that still calls `GET /bills?importance=…` so it follows the redirect or calls the new path. That update happens in this slice, which is the one that changes the URL.

### Manual checks at 1280 and 390

Do these in a browser. There is no Playwright spec to extend.

**1280×800, after slice 1.** Dashboard, Bills, and Budgets. The sidebar is 240px. Collapse still shrinks it to 60px and survives a reload. Menu is not visible. Login has no sidebar.

**390×844, after slice 1.** The same three pages. The sidebar is off-screen, the page uses the full width, and the title is not under the Menu button. Menu opens the labelled sidebar, including the username and Sign out. Esc, the backdrop, and a second press of Menu close it, and focus returns to Menu. Collapse the rail at 1280 first, reload, then narrow: the opened menu is still the 240px labelled menu, and Sign out works. Widen past 800px and the docked sidebar comes back, collapsed or not as it was before.

**1280×800, after slices 2 and 3.** `/bills` with a mix of due, overdue, future, variable, and fixed bills. Three cards. The strip shows Confirm and Skip for the ones that need a decision, no Confirm on a fixed chip, and the fixed chip does not say the bill was already paid. The list does not scroll sideways. Edit opens a drawer on the right, the list stays put, Tab does not leave the drawer, Esc closes it and focus is back on Edit. Save updates the line and the monthly card and closes the drawer. A stale `next_due_date_was` shows the error in the drawer and leaves the form in place. Confirm on the strip advances the date on the line and removes the chip. Delete asks “Deactivate this bill?” and removes the line and the drawer. Add new still asks for a start date, and adding a bill does not destroy an open drawer. The mark section in the drawer is closed. The page does not show Keep paying, the nudge, or a legend. The dashboard Due to confirm card still shows its Confirm button after `.gw-action-pay` moves into `styles.css`.

**390×844, after slices 2 and 3.** Menu works on `/bills`. The strip scrolls sideways. The list does not. Edit opens the form under that row, full width, one column, and the drawer node is still outside `#bills-rows`. Esc closes the menu first if it is open, then the form. Confirm and Skip in the form work, and Skip does not save the name. Save and Cancel are on screen without a horizontal scroll. An Add while the form is open leaves the form in place.

**1280 and 390, after slice 4.** The muted link opens the quiet view. The three pile totals, the unmarked count, both notes, and the filters are there. The daily page still has none of the badges. Saving a mark on `?importance=unset` removes the line and the totals update without a reload, and the console does not report a missing `#due-soon` target. Resize from 1280 to 390 with the drawer open: it becomes the inline form, overflow is not stuck, and widening again traps focus and moves it into the dialog.

## Migration

No migration.

- Do not add a file under `alembic/versions/`.
- Do not change `RecurringBill`, `_BILL_EDIT_COLUMNS`, or `ck_recurring_bills_importance`.
- Do not backfill marks. Null still means not set yet.
- Pytest builds the schema from the model (`Base.metadata.create_all` in `tests/conftest.py`). No new column means no schema drift.
- `uv run alembic upgrade head` is a no-op for this work. Do not run a revision generator.

## Docs to touch in the last slice

`docs/recurring-payments.md`, the paragraph that says the Bills page splits the monthly averages into keep paying, safe to cancel, and not marked. Say that `/bills` shows Bills fund monthly, Bills fund annual, and Other recurring, and that the three-way split, the marks, and both notes are on `/bills/if-something-happens`. The mark is still ignored by confirm, skip, pay, the forecast, cover, and the recommended deposit.

`docs/specs/essential-bills.md`, one line at the top of “Bills page”: the HTML layout in that section is superseded by this spec. Leave the rest of that spec (data, API, MCP, CLI, audit, migration) as the record of how the columns were added.

Do not change `AGENTS.md`, the API count, the MCP tool count, or `docs/cli-and-mcp.md`. The README feature line already says a payment can be marked keep paying or safe to cancel. That is still true.

## Rollout

Four pull requests, in this order. Each one leaves the suite green on its own. Slice 2 must not be the last of them. After slice 2 the marks, both notes, and the `?importance=` bookmarks are gone from the daily page and the quiet view does not exist yet. Slice 4 puts them back. Merge slice 2 and slice 4 back to back. Do not stop after slice 2.

### 1. Off-canvas nav

`base.html`, `styles.css`, `tests/test_nav.py`, and this spec. No bills behaviour. Desktop collapse still works. The narrow rules override `.gw-sidebar.collapsed`, including Sign out. Manual 1280 and 390 on Dashboard, Bills, and Budgets, including collapse-on-desktop then narrow, then Sign out.

### 2. List, due strip, three cards, in-flow editor

The new partials, `bills.js`, `due_soon`, the route render changes, create status 422 (including Paid from, with no `gwError`), `_editor_error` on every `bills_update` 4xx, the real strip error node, `HX-Focus`, the collapsed mark section, Confirm and Skip on the strip and in the sibling confirm form. The daily page already hides badges, notes, and the three pile cards. `?importance=` still filters `/bills`. The editor is in-flow at every width. The drawer stays outside `#bills-rows`. Esc closes it. Empty drawer swaps call `closeEditor()`. Update and delete send `billId`. Tests listed for slice 2, and the existing tests that slice breaks. Not the quiet-view tests, not `marks=1`, not the 303. `due_to_confirm` and the dashboard card stay.

### 3. Drawer above 800px

CSS and `bills.js` only: fixed panel, backdrop, dialog role, focus trap, `inert` on `main` and the sidebar, `placeEditor` on resize, overflow restored on the way down, focus moved into the dialog on the way up. The form partial and the routes do not change. Manual trap and resize checks.

### 4. Quiet view

`bills_if_something.html`, the route, the 303, `marks=1`, `show_marks`, moving the pile ids and the “still paid” sentence, skipping the `#due-soon` swap when `marks=1`, the muted link on `/bills`. Docs. Move the filter tests onto the new path. Full pytest. Manual check that `/bills` has no mark chrome and the new page has the totals, both notes, and the filters.

## Out of scope

- Grouping by paid from.
- A Pay button, a Start here page, a second login, email, or a printed pack.
- Changing confirm, skip, pay, auto-pay, the forecast, bills cover, the scheduler, or `/monthly-cost`.
- REST, MCP, CLI, and the audit log.
- A `GET /api/bills?importance=` filter.
- Editing notes from the dashboard card.
- Refreshing the “No payment method” chip from `gwBillSuccess`.
- A Playwright dependency.
- Any migration.
- A list wrapper (`<ul>`) around the articles. Articles are enough for the behaviour.
