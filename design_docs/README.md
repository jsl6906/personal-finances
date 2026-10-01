# Handoff: Ledger — household personal-finance app

## Overview
Ledger is a self-hosted web app for a two-person household: transactions imported from spreadsheets, statement PDFs and a Tiller feed; AI-assisted duplicate review, category suggestion and bill/usage extraction (Gemini); budgets with spread rules; a chat interface over the data; email alerts; and a bulk backfill of ~2,000 historical documents. Target stack per product owner: Python (FastAPI) backend, Azure Postgres (`personal_storage` DB, `personal_finances` schema, Entra ID auth, files stored as `bytea`), Docker image for a home server, `GEMINI_KEY` in env.

## About the design files
`Ledger.dc.html` is a **design reference built in HTML** — a clickable prototype showing intended look and behavior. It is not production code. Recreate it in the app's front-end environment (recommendation: React + TypeScript served by FastAPI, or any framework the team prefers) using that codebase's own component patterns. The prototype's data is fictional sample data.

Open `Ledger.dc.html` in a browser (needs `support.js` and `_ds/` alongside) to click through all eight screens.

## Fidelity
**High-fidelity.** Layout, type, color, spacing and states follow the Industry design system exactly (`_ds/…/styles.css` + `readme.md`). Recreate closely; take every value from the CSS variables listed under Design tokens rather than hard-coding.

## Application shell
- Grid: `212px` sidebar + `minmax(0,1fr)` main. Sidebar sticky, full viewport height, right hairline border `--color-divider`, padding `--space-4 --space-3`.
- Brand: "Ledger" in `--font-heading` 600 22px, followed by a 10px uppercase "HOUSEHOLD" label in `--color-accent-700`.
- Nav items (8): 14px body font, Lucide icon 16px stroke 1.5, optional right-aligned 11px count badge in `--color-accent-700`. Active item: background `--color-accent-100`, border `--color-accent-300`, text `--color-accent-900`. Square corners.
- Sidebar footer: sync status lines (12px, `--color-neutral-700`) with an 8px square indicator (filled accent = ok; hollow, pulsing 1.6s = running); household member initials in 24px hairline squares; "Signed in via Entra ID".
- Main: padding `--space-6 --space-8 --space-8`, sections stacked with `--space-6` gaps. Every screen header: `h2` (32px condensed) + 13px muted subtitle, actions right-aligned.

## Components (from the design system, used throughout)
- **Card**: `.card.blueprint` — transparent, 1px `--color-divider` border, four `+` registration marks (`<i class="corner tl|tr|bl|br">`), padding `--space-3`, `.card-kicker` (10px uppercase accent), `.card-title` (17px condensed), `.card-body` (13px, 80% opacity), `.card-meta` (11px, 50% ink).
- **Buttons**: `.btn-primary.blueprint` (solid `--color-accent`, text `--color-bg`, hover `--color-accent-600`, active `--color-accent-700`, corner marks) for the single main action per view; `.btn-secondary` (hairline border); `.btn-ghost` (accent text). Font: condensed 600 14px. Square corners.
- **Tags**: `.tag-accent` (bg `--color-accent-100`, text `--color-accent-800`), `.tag-neutral`, `.tag-outline`. 11px.
- **Segmented control**: `.seg` / `.seg-opt`, selected = accent fill with `--color-bg` text.
- **Inputs**: `.input` 36px min height, bg `--color-surface`, hairline border, accent focus border. `.field > label` 12px.
- **Table**: `.table` — 11px uppercase headers, 8%-ink row rules, 4%-ink row hover, 14px body. Numeric cells `font-variant-numeric: tabular-nums`, right-aligned.
- **Progress bar**: 8px tall hairline-bordered box, fill `--color-accent-400`; over-budget fill `--color-accent-700`.
- **Big number**: condensed 600, 28–32px, line-height 1.
- **Info note (AI/extraction)**: border `--color-accent-300`, bg `--color-accent-100`, text `--color-accent-900`, 13px.
- Charts: inline SVG — bars `--color-accent-300` (income) / `--color-accent-700` (expenses), gridlines `--color-divider`, labels 10px `--color-neutral-600`; line charts 1.5px `--color-accent-700` with 3px hollow dots and dashed average line `--color-accent-500`.

## Screens

### 1. Dashboard
Purpose: month-at-a-glance for the household.
- Header "September 2026" + "Household overview · 13 accounts · last transaction 26 Sep"; actions: secondary "Import transactions", primary "Ask the ledger".
- Stat row: `repeat(auto-fit, minmax(180px,1fr))` cards — Net worth $956,392; Spent · Sep $4,612; Income · Sep $6,825; Budget left $388. Kicker / 30px number / meta line.
- Row 2 `2fr 1fr`: "Income vs expenses · trailing 12 months" grouped bar chart (12 months, legend top-right); "Out of norm — 3 items flagged this month" list (title, amount, one-line reason, tag + ghost "Review" link), meta "Email alert sent to 2 recipients".
- Row 3 `1fr 1fr`: Accounts balances table (Account / Institution / Balance, 13 rows incl. TSP, IRA, home, two vehicles); Budget progress list (category, bar, `spent / budget`), ghost "All budgets".

### 2. Transactions
- Header "Transactions" + count line; search input (260px), secondary "Scan for duplicates", primary "Import".
- Filter row: segmented All / Flagged / With statements; filter tags (period, account, category). After "Scan for duplicates": accent tag "Duplicate scan: 0 new candidates · 14 confirmed-separate pairs kept".
- Grid `minmax(0,1fr) minmax(240px,300px)` when a row is selected (`0px` otherwise). Table card has `padding:0; overflow-x:auto`.
- Table columns: Date (muted, nowrap) · Description (500) · Category (neutral tag) · Account (muted) · marks (11px accent-700: "statement", "flagged", "spread") · Amount (right; credits in `--color-accent-700`, debits ink; credits prefixed "+", debits "−").
- Row click selects (row bg `--color-accent-100`) and opens the sticky detail panel: kicker "Transaction", title, "date · added …", 32px amount, read-only Category/Account inputs, Notes textarea; "Budget treatment" radios (Count in September / Spread over 12 months ($X/mo)); "Statement" block — attached file row (icon, name, extracted meta, ghost "Open" → Bills) or secondary block button "Attach statement (PDF, image)"; "Duplicate check" note.

### 3. Import (5-step wizard)
Stepper: 5 equal columns, 2px top rule (accent = current, accent-400 = done, divider = upcoming), "01" condensed 18px + label. Footer: secondary "Back" (disabled on step 1), primary "Continue" (or "Skip remaining, continue" while duplicates pending; "Commit N transactions" on step 5).
1. **Source**: two dashed-border cards (Spreadsheet: .xlsx/.csv/Tiller tab; Document: statement PDF/scan, Gemini extracts table) each with 28px accent icon, title, body, secondary "Choose file" — choosing sets the source and advances. Below: "Recent imports" table (File / Type / Rows / Duplicates removed / When).
2. **Map columns**: `3fr 2fr`. Left card: kicker (source type), file name, tag "N rows detected"; table Source column / Sample / Maps to (select: Transaction date, Posted date, Description, Amount, Category, Account, Notes, Check number, — ignore —); meta "Mapping saved as preset “…” · reused automatically next time". Right: "Preview — first 4 rows as they'll land"; for documents an info note with extraction summary (period, account ending, line items, confidence, rows needing a look).
3. **Defaults**: `1fr 1fr`. Left card: Account/institution, Category when unmatched, Household member, Notes. Right: (documents only) "Read from the document" card with accent-400 border — Institution, Account, Period, Sign convention; plus "Category suggestions" card explaining AI categorisation using the category hierarchy.
4. **Duplicates**: one pair at a time, max-width 920px. Kicker "Potential duplicate i of n", h3 title, tag "96% match". Two cards side by side: Incoming (file) vs Already in ledger — title, 28px amount, Date/Account/Row or Added. Info note with the reasoning. Actions: primary "Same transaction — skip incoming", secondary "Different — keep both", muted note "“Keep both” is remembered; this pair won't be flagged again." When all decided: summary card with counts and ghost "Start over".
5. **Review**: card with kicker "Ready to commit", file name, three big numbers (rows to insert / duplicates skipped / kept as separate), body about bytea + sha-256 storage and batch rollback; after commit an accent tag "Committed · batch #418".

### 4. Bills & statements
- Header + primary "Upload statement".
- Grid `200px 1fr`. Left: series list buttons (Water, Electric, Natural gas, Internet, Auto insurance) with statement count; active state as nav.
- Right, top (dismissible): **AI suggestion card** with accent-400 border — kicker "Suggested assignment · needs approval", file name, tag "Uploaded 2 min ago"; four fields (Link to transaction, Series, Period, Usage + unit price); primary "Approve", secondary "Edit fields", ghost "Dismiss".
- Usage card: kicker (category › vendor), title "Usage · unit per statement period", right-aligned "12-mo avg" / "Latest"; 12-point line chart with dashed average line.
- Statements table: Statement / Period / Usage / Linked transaction (link → Transactions) / Amount.

### 5. Budgets
- Header with period label; segmented Month / Quarter / Year (recomputes rows).
- Grid `2fr 1fr`. Table: Category (+ 11px note e.g. "spread from $1,284 annual") / Progress bar / Spent / Budget / Left (negative and `--color-accent-700` when over).
- Right: Total card (30px spent "of" budget, meta over/remaining); "Spread rules" card listing annual payments → monthly equivalents, ghost "Add rule".

### 6. Ask the ledger (chat)
- Grid rows `auto 1fr auto`, min-height viewport. Thread max-width 820px.
- Message: 11px uppercase author label; bubble is a `.blueprint` box (corner marks), padding `--space-3 --space-4`, 14px, `white-space: pre-wrap`. User messages right-aligned, bg `--color-accent-900`, text `--color-bg`; assistant left-aligned, transparent. Optional outline tag for an attached document; optional action row (primary "Attach to 25 Sep transaction", secondary "Not now").
- Composer: icon button "+" (attach), input, primary "Send". Enter sends.

### 7. Alerts
- Card with four rule rows (`1fr auto`): title, 13px description, On/Off segmented control. Rules: Budget overspend; Out-of-norm spending (2σ above trailing 12-month norm); Large single transaction (> $500, non-recurring); Weekly digest.
- Below `1fr 1fr`: Recipients (masked email tags + "Add address" input); Last sent list.

### 8. Sources & backfill
- Three connection cards: Tiller (tag "Connected"), Plaid (tag-outline "Evaluating"), Manual uploads & watch folder (tag-neutral "Always on") — each kicker, status tag, title, body, meta.
- Backfill card: title "1,240 of 2,014 files processed · 2006 – 2026", secondary "Pause", ghost "Review 38 flagged"; 10px progress bar (accent fill, 61.6%); six stat cells; table File / Detected / Result / Status (tags: Parsed, Needs review, Skipped).

## Interactions & state
- `screen`: dashboard | transactions | import | bills | budgets | chat | alerts | sources (sidebar + cross-links: Import, Ask the ledger, Review → Transactions, All budgets, Open → Bills).
- Transactions: `selectedId`, `filter` (all|flag|bills), `scanDone`.
- Import: `step` 1–5, `source` (sheet|doc), `dupIndex`, `decisions[]` (dup|keep), `committed`.
- Bills: `series`, `aiSuggestionVisible`.
- Budgets: `period` (Month|Quarter|Year).
- Chat: `input`, `messages[]`.
- Alerts: per-rule boolean.
- No animation beyond the 1.6s opacity pulse on the "running" indicator. Hover/active/focus states come from the design-system CSS (accent ramp tints, 2px accent focus ring).
- Responsive: main content uses `minmax(0,1fr)` tracks and `auto-fit` card grids; tables scroll horizontally inside their card.

## Design tokens (from `_ds/…/styles.css`)
- Ground `--color-bg #f2f2f3`, surface `#e9e9ea`, text `#1d1f20`, accent `#5980a6`, divider = text at 16%.
- Accent ramp: 100 `#eef6ff`, 200 `#d6ebff`, 300 `#b5d9fd`, 400 `#94bce3`, 500 `#749dc4`, 600 `#597ea3`, 700 `#416180`, 800 `#2c455d`, 900 `#1d2d3d`.
- Neutral ramp: 100 `#f5f5f8` … 600 `#7a7a7d`, 700 `#5d5d60`, 800 `#424244`, 900 `#2b2b2d`.
- Fonts: headings "Barlow Condensed" 600; body "Barlow" 400/500/700 (Google Fonts). Body 15px/1.55; h2 32px; h3 25px; letter-spacing −0.015em on headings.
- Spacing: `--space-1` 3.4px, `-2` 6.8px, `-3` 10.2px, `-4` 13.6px, `-6` 20.4px, `-8` 27.2px.
- Radius: 0 on all components (blueprint rule). Shadows `--shadow-sm/md/lg` (unused in this design; cards are flat).
- Icons: Lucide, 16px in nav, 13–18px inline, stroke-width 1.5.

## Assets
No raster images. Icons are Lucide (MIT). Charts are inline SVG generated from data.

## Files
- `Ledger.dc.html` — the clickable prototype (template + logic in one file).
- `support.js` — runtime the prototype needs to render; ignore for implementation.
- `_ds/industry-…/styles.css` — the design-system stylesheet (source of all tokens and component classes).
- `_ds/industry-…/readme.md` — the design-system guide.
