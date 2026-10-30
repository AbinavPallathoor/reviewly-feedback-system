# Reviewly

Reviewly is a **trusted, central review platform**, built with Flask, plain `sqlite3` and Jinja for a university DBMS lab. Companies don't host their own reviews. Their servers send each review to Reviewly through a signed API, and Reviewly accepts it only if the company registered the purchase beforehand. Reviewly stores the review in an append-only hash-chained ledger and serves it back to the company's website through an embeddable widget. The company's server never touches the displayed reviews, so it can't invent, edit, hide or reorder them.

There are two example partner shops, each a separate Flask app with its own database and its own look:

| App | URL | What it is |
|---|---|---|
| Reviewly | http://127.0.0.1:5050 | The trust authority: API, widget, public verification, merchant/admin consoles |
| KICKVAULT | http://127.0.0.1:5001 | Neo-brutalist sneaker & streetwear store |
| Fern & Fig | http://127.0.0.1:5002 | Warm, organic pantry & grocery store |

## Install & run

```bash
pip install flask
python run_all.py            # starts all three
python run_all.py --reset    # wipes all three databases first
```

On first start, `reviewly.db` is created from `schema.sql` and seeded. Each shop creates its own `shop.db`, pushes its catalog to Reviewly, registers its seeded orders and sends its seeded reviews, all through the real signed API. `python app.py` still runs Reviewly by itself.

> Reviewly uses port 5050 because macOS AirPlay Receiver takes port 5000.

## Demo logins (password for all: `password`)

| Role | Email |
|---|---|
| Admin | admin@reviewly.test |
| Merchant | northwind@reviewly.test, hearth@reviewly.test |
| Customer | priya, liam, sofia, omar, mei, jonas, aisha, diego `@reviewly.test` |
| Partner merchant (API) | kickvault@reviewly.test, fernfig@reviewly.test |
| Shop staff (store admin at `/admin` on each shop) | staff@kickvault.test (:5001), staff@fernfig.test (:5002) |

The shops use the same customer emails, so one person's reviews line up across all three sites. Shop logins also use the password `password`.

## Trust model

```
 shop server ── HMAC-signed POST /api/v1/{products,orders,reviews} ──► Reviewly
 shopper's browser ◄── widget.js + GET /api/v1/public/products/<sku> (read-only, CORS) ── Reviewly
```

1. **Purchase first.** At checkout the shop calls `POST /api/v1/orders`, and Reviewly records a `Transactions` row with its own timestamp. A customer who isn't on Reviewly yet gets an unclaimed account (`password_hash = '!'`). They can claim it later by signing up with the same email.
2. **Signed review.** `POST /api/v1/reviews` carries `X-Reviewly-Key/-Timestamp/-Nonce/-Signature`, where the signature is `HMAC-SHA256(secret, "ts.nonce.METHOD.path.body")`. Reviewly checks:
   - the key is valid
   - the timestamp is less than 5 minutes old
   - the signature matches
   - the nonce hasn't been used before (`Api_Nonce` PK)
   - the order was registered **by this company** for this customer and SKU
   - there is only one review per order
   - the content is valid

   Identical copy-pasted text, or a burst of more than 20 reviews in 10 minutes, is held as `Pending` with a `flag_reason`.
3. **No edit/delete endpoint for companies.** Only the reviewer can edit, from the private receipt link (`/r/<token>`) they get back. Edits are kept in `Review_Revision`.
4. **Tamper evidence.** Triggers append every create, edit and status change to `Review_Ledger` as a hash chain. `/verify/<id>` and **Admin → API & Integrity → Verify ledger** recompute the chain. `sha256()` is registered by `db.connect()`, so a plain `sqlite3` shell can't update a review without the trigger failing.
5. **Display.** The shop embeds `<div data-reviewly-sku data-reviewly-key>` + `widget.js`. The widget renders in a closed Shadow DOM. The shop can theme the accent, font and radius, but not the "Verified by Reviewly" footer.

Each shop has a store admin at `/admin`. It shows orders, products with their Reviewly ratings, the review feed (read-only), and a **Reviewly integration** page. That page shows the API log and has checks that try to cheat:
- fake review → **422**
- replay → **409**
- forged signature → **401**
- edit/delete a review → **405**

Every attempt appears as Rejected in the merchant's **API & widget** page and the admin request log.

## Features → requirements

| Requirement | Where |
|---|---|
| Register / login / logout, hashed passwords, role guards | `app.py` (`register`, `login`, `role_required`) |
| Company directory (home page), search across companies and products | `/companies`, which uses `v_product_stats` |
| Product page: rating bars, Quality/Delivery/Value averages, reviews sorted by helpfulness, Verified Buyer badge | `/products/<id>`, which uses `v_rating_distribution` and `v_helpfulness` |
| Write or edit one review per product, with 3 criteria scores and an optional media URL | `POST /products/<id>/review`. Backed by `UNIQUE(user_id, product_id)`, `Review_Criteria_Rating` and `Review_Media` |
| Up/down vote: one per review, clicking again toggles it off | `POST /reviews/<id>/vote` (JSON). Backed by `UNIQUE(review_id, user_id)` |
| Report a review with a reason | `POST /reviews/<id>/report`. Backed by `UNIQUE(review_id, reporter_user_id)` |
| Merchant dashboard: products, average rating, distribution, sentiment summary, one editable reply per review | `/merchant`, `POST /merchant/reply/<id>`. Uses `Merchant_Response.review_id UNIQUE` with an upsert |
| Sentiment score computed on insert | `db.sentiment()`, a small positive/negative word list |
| Admin overview: stat cards, reviews-per-day sparkline, rating histogram (inline SVG) | `/admin` |
| Moderation queue: approve or reject reports | `/admin/moderation`, which uses `v_moderation_queue` and `trg_report_resolved` |
| Reviews table: search, filter, change status | `/admin/reviews` |
| Users; Products & Categories (add either) | `/admin/users`, `/admin/catalog` |
| Force-directed graph view | `/admin/graph`, with data from `/admin/graph.json` |
| Read-only listing of the schema, triggers and views | `/schema`, read live from `sqlite_master` |
| Partner API: catalog, orders, reviews, status | `api.py`, `/api/v1/*`, backed by `Api_Client`, `Api_Nonce`, `Api_Request_Log` |
| Embeddable widget, star snippets + public JSON | `/widget.js` (`static/widget.js`), `/api/v1/public/products/<sku>?key=pk_…`, `/api/v1/public/summary?key=…&skus=…` |
| Review receipt (customer edit) and public verification | `/r/<token>`, `/verify/<id>` |
| Company trust profiles | `/companies`, `/companies/<id>` |
| Merchant API keys, embed snippet, call log | `/merchant/integration` |
| Admin API clients, request log, ledger verification | `/admin/integrity` |

## Triggers and views (all in `schema.sql`)

| Object | Purpose |
|---|---|
| `trg_review_verify_purchase` (BEFORE INSERT on Review) | If a matching `Transactions` row exists, it sets `transaction_id` and `is_verified_purchase = 1`. SQLite can't assign to `NEW`, so the trigger re-inserts the row with those values filled in and then calls `RAISE(IGNORE)` to drop the original insert. |
| `trg_review_stats_insert` / `_update` / `_delete` (AFTER on Review) | Recompute `Product.avg_rating` and `total_reviews` from **Approved** reviews only |
| `trg_report_autohide` (AFTER INSERT on Review_Report) | When a review has 3 pending reports, its status is set to `'Pending'` (hidden) |
| `trg_report_resolved` (AFTER UPDATE on Review_Report) | Approving a report sets the review to `Rejected` and closes the other reports on it. Rejecting a report changes nothing, except that once no pending reports remain, an auto-hidden review becomes visible again. |
| `v_product_stats` | Product, merchant, category, avg_rating, total_reviews, verified_count |
| `v_rating_distribution` | product_id, rating, count |
| `v_helpfulness` | review_id, up, down, score |
| `v_moderation_queue` | Pending reports joined with the review, product, author and reporter |
| `trg_ledger_review_insert` / `_update` (AFTER on Review) | Append `created` / `edited` / `status` entries to `Review_Ledger`, where `entry_hash = sha256(prev_hash|content_hash|event|review_id)` |
| `trg_ledger_no_update` / `_no_delete` | `RAISE(ABORT)`: the ledger is append-only |
| `trg_review_revision` (BEFORE UPDATE on Review) | Copies the old rating, title and text into `Review_Revision` |
| `v_client_activity` | Accepted, rejected and flagged calls, review count and average rating per API client |
| `v_review_provenance` | Review + which company sent it + order reference + latest ledger hash |

The constraints are PK, FK (CASCADE / SET NULL / RESTRICT), CHECK, UNIQUE and NOT NULL. `PRAGMA foreign_keys = ON` is set on every connection, in `db.connect()`.

## Files

`app.py` routes · `api.py` partner API and widget endpoints · `db.py` connection, init, seed, ledger verification · `schema.sql` DDL · `run_all.py` launcher · `shops/reviewly_client.py` signing client (stdlib only) · `shops/shopkit.py` shared shop logic · `shops/<shop>/` catalog, templates and CSS for each shop · `templates/` Jinja (`admin/` uses `admin_base.html`) · `static/style.css` small overrides and animations · `static/app.js` Alpine components and animations.

## UI stack

All of these load from a CDN, so there's no build step: [daisyUI 5](https://daisyui.com) on [Tailwind CSS 4](https://tailwindcss.com) (the browser build), the monochrome `lofi` theme for light mode and `black` for dark, [Alpine.js](https://alpinejs.dev) for interactivity, [Lucide](https://lucide.dev) icons, [force-graph](https://github.com/vasturiano/force-graph) for the graph view, and the Inter font.

Product photos are in `static/img/products/<sku>.jpg`. They come from Wikimedia Commons under free licences, and the authors and sources are listed in [`static/img/products/CREDITS.md`](static/img/products/CREDITS.md). Partner shops send an `image_url` with their catalog, and their photos are credited in `shops/<shop>/static/img/CREDITS.md`. A product with neither shows its initials instead.
