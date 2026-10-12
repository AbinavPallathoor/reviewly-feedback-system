# Reviewly

Customer Feedback and Review Management System. Built with Flask, plain `sqlite3` and Jinja, for a university DBMS lab.

## Install & run

```bash
pip install flask
python app.py
```

Open **http://127.0.0.1:5000**. On first start, `reviewly.db` is created from `schema.sql` and seeded. To reset, delete `reviewly.db` and start the app again.

> On macOS, AirPlay Receiver also listens on port 5000. Use `127.0.0.1` and not `localhost`, or turn AirPlay Receiver off.

## Demo logins (password for all: `password`)

| Role | Email |
|---|---|
| Admin | admin@reviewly.test |
| Merchant | northwind@reviewly.test, hearth@reviewly.test |
| Customer | priya, liam, sofia, omar, mei, jonas, aisha, diego `@reviewly.test` |

## Features → requirements

| Requirement | Where |
|---|---|
| Register / login / logout, hashed passwords, role guards | `app.py` (`register`, `login`, `role_required`) |
| Browse and search products by category | `/products`, which uses `v_product_stats` |
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

The constraints are PK, FK (CASCADE / SET NULL / RESTRICT), CHECK, UNIQUE and NOT NULL. `PRAGMA foreign_keys = ON` is set on every connection, in `db.connect()`.

## Files

`app.py` routes · `db.py` connection, init and seed · `schema.sql` DDL · `templates/` Jinja (`admin/` uses `admin_base.html`) · `static/style.css` monochrome theme · `static/app.js` Alpine components and animations.
