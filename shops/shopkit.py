"""Shared logic for the example partner shops. Each shop brings its own config, templates and CSS.

A shop keeps its own database (customers, catalog, orders). It never stores or serves reviews:
it registers each order with Reviewly, forwards the review the customer writes, and embeds
Reviewly's widget to display them.
"""
import json
import os
import sqlite3
import threading
import time
import urllib.request
from functools import wraps

from flask import Blueprint, Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from shops.reviewly_client import ReviewlyClient, ReviewlyError

REVIEWLY_URL = os.environ.get("REVIEWLY_URL", "http://127.0.0.1:5050")
ADMIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin")
CRITERIA = ("Quality", "Delivery", "Value")

SCHEMA = """
CREATE TABLE Staff (
    staff_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name      VARCHAR(100) NOT NULL,
    email          VARCHAR(150) NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL
);
CREATE TABLE Customer (
    customer_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name      VARCHAR(100) NOT NULL,
    email          VARCHAR(150) NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL
);
CREATE TABLE Product (
    sku          VARCHAR(50) PRIMARY KEY,
    name         VARCHAR(150) NOT NULL,
    category     VARCHAR(80)  NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents > 0),
    blurb        TEXT NOT NULL
);
CREATE TABLE Orders (
    order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref       VARCHAR(40) NOT NULL UNIQUE,
    customer_id     INTEGER NOT NULL REFERENCES Customer(customer_id),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewly_synced INTEGER NOT NULL DEFAULT 0 CHECK (reviewly_synced IN (0, 1))
);
CREATE TABLE Order_Item (
    order_id     INTEGER NOT NULL REFERENCES Orders(order_id) ON DELETE CASCADE,
    sku          VARCHAR(50) NOT NULL REFERENCES Product(sku),
    price_cents  INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);
-- What we sent to Reviewly and what it answered. Reviews themselves live on Reviewly.
CREATE TABLE Review_Submission (
    submission_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref      VARCHAR(40) NOT NULL,
    sku            VARCHAR(50) NOT NULL,
    customer_id    INTEGER NOT NULL REFERENCES Customer(customer_id),
    payload        TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    review_id      INTEGER,
    review_status  VARCHAR(20),
    receipt_url    VARCHAR(300),
    error          TEXT,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE Api_Call (
    call_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    method      VARCHAR(8) NOT NULL,
    path        VARCHAR(200) NOT NULL,
    status      INTEGER NOT NULL,
    summary     TEXT NOT NULL,
    body        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def create_shop(cfg, root):
    """cfg: dict from the shop's catalog.py. root: the shop's package folder (templates/, static/)."""
    app = Flask(cfg["slug"], root_path=root)
    app.secret_key = f"{cfg['slug']}-dev-secret"
    # Every app runs on 127.0.0.1, and cookies ignore ports, so each needs its own cookie name.
    app.config["SESSION_COOKIE_NAME"] = f"{cfg['slug']}_session"
    db_path = os.path.join(root, "shop.db")

    def connect():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def log_call(method, path, status, summary, body):
        with connect() as conn:
            conn.execute("INSERT INTO Api_Call (method, path, status, summary, body) VALUES (?,?,?,?,?)",
                         (method, path, status, summary, body))

    client = ReviewlyClient(REVIEWLY_URL, cfg["public_key"], cfg["secret"], on_call=log_call)

    # ---------------------------------------------------------------- database

    def init_db():
        if os.path.exists(db_path):
            return
        conn = connect()
        conn.executescript(SCHEMA)
        pw = generate_password_hash("password")
        conn.execute("INSERT INTO Staff (full_name, email, password_hash) VALUES (?,?,?)", (*cfg["staff"], pw))
        for name, email in cfg["customers"]:
            conn.execute("INSERT INTO Customer (full_name, email, password_hash) VALUES (?,?,?)", (name, email, pw))
        for p in cfg["catalog"]:
            conn.execute("INSERT INTO Product (sku, name, category, price_cents, blurb) VALUES (?,?,?,?,?)",
                         (p["sku"], p["name"], p["category"], p["price"], p["blurb"]))
        for i, s in enumerate(cfg["seed_orders"]):
            cid = conn.execute("SELECT customer_id FROM Customer WHERE email=?", (s["email"],)).fetchone()[0]
            ref = f"{cfg['order_prefix']}-{1001 + i}"
            oid = conn.execute("INSERT INTO Orders (order_ref, customer_id, created_at) VALUES (?,?, datetime('now', ?))",
                               (ref, cid, f"-{s.get('days_ago', 10)} days")).lastrowid
            price = conn.execute("SELECT price_cents FROM Product WHERE sku=?", (s["sku"],)).fetchone()[0]
            conn.execute("INSERT INTO Order_Item (order_id, sku, price_cents) VALUES (?,?,?)", (oid, s["sku"], price))
        conn.commit()
        conn.close()

    def db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close(_e=None):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def q(sql, args=(), one=False):
        cur = db().execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()

    # ---------------------------------------------------------------- Reviewly sync

    def register_order(conn, order_ref):
        o = conn.execute("SELECT o.*, c.full_name, c.email FROM Orders o JOIN Customer c ON c.customer_id=o.customer_id "
                         "WHERE order_ref=?", (order_ref,)).fetchone()
        skus = [r[0] for r in conn.execute("SELECT sku FROM Order_Item WHERE order_id=?", (o["order_id"],))]
        client.call("POST", "/api/v1/orders", {"order_reference": order_ref, "items": skus,
                                               "customer": {"email": o["email"], "name": o["full_name"]}})
        conn.execute("UPDATE Orders SET reviewly_synced=1 WHERE order_id=?", (o["order_id"],))
        conn.commit()

    def submit_review(conn, order_ref, sku, customer, rating, title, text, criteria):
        payload = {"order_reference": order_ref, "sku": sku, "customer_email": customer["email"],
                   "rating": rating, "title": title, "text": text, "criteria": criteria}
        try:
            data = client.call("POST", "/api/v1/reviews", payload)
            status, err = 201, None
        except ReviewlyError as e:
            data, status, err = e.data, e.status, e.reason
        conn.execute("INSERT INTO Review_Submission (order_ref, sku, customer_id, payload, http_status, review_id, "
                     "review_status, receipt_url, error) VALUES (?,?,?,?,?,?,?,?,?)",
                     (order_ref, sku, customer["customer_id"], json.dumps(payload), status, data.get("review_id"),
                      data.get("status"), data.get("receipt_url"), err))
        conn.commit()
        return status, data, err

    def sync_with_reviewly():
        """Push the catalog, register unsynced orders, and send the seeded reviews (idempotent)."""
        for _ in range(30):
            try:
                client.send(client.build("GET", "/api/v1/health"), remember=False)
                break
            except ReviewlyError:
                time.sleep(1)
        else:
            print(f"[{cfg['slug']}] Reviewly not reachable at {REVIEWLY_URL}; will sync orders when it is.")
            return
        conn = connect()
        try:
            site = f"http://127.0.0.1:{cfg['port']}"
            client.call("POST", "/api/v1/products", {"products": [
                {"sku": p["sku"], "name": p["name"], "category": p["category"],
                 "image_url": f"{site}/static/img/{p['sku']}.jpg"} for p in cfg["catalog"]]})
            for o in conn.execute("SELECT order_ref FROM Orders WHERE reviewly_synced=0 ORDER BY order_id").fetchall():
                register_order(conn, o["order_ref"])
            for i, s in enumerate(cfg["seed_orders"]):
                if "review" not in s:
                    continue
                ref = f"{cfg['order_prefix']}-{1001 + i}"
                done = conn.execute("SELECT 1 FROM Review_Submission WHERE order_ref=? AND sku=? AND http_status IN (201, 409)",
                                    (ref, s["sku"])).fetchone()
                if not done:
                    cust = conn.execute("SELECT * FROM Customer WHERE email=?", (s["email"],)).fetchone()
                    r = s["review"]
                    submit_review(conn, ref, s["sku"], cust, r["rating"], r["title"], r["text"],
                                  {c: max(1, min(5, r["rating"] + d)) for c, d in zip(CRITERIA, r.get("crit", (0, 0, 0)))})
            print(f"[{cfg['slug']}] synced catalog, orders and seed reviews with Reviewly")
        except ReviewlyError as e:
            print(f"[{cfg['slug']}] sync failed: {e}")
        finally:
            conn.close()

    # ---------------------------------------------------------------- helpers

    @app.before_request
    def load_user():
        g.user = None
        if "cid" in session:
            g.user = q("SELECT * FROM Customer WHERE customer_id=?", (session["cid"],), one=True)

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                flash("Log in to continue.", "error")
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return wrapper

    @app.context_processor
    def inject():
        return {"shop": cfg, "reviewly_url": REVIEWLY_URL, "ep": request.endpoint or "",
                "money": lambda c: f"{cfg['currency']}{c / 100:,.2f}"}

    def product_or_404(sku):
        p = q("SELECT * FROM Product WHERE sku=?", (sku,), one=True)
        if not p:
            abort(404)
        return p

    def submission_for(order_ref, sku):
        return q("SELECT * FROM Review_Submission WHERE order_ref=? AND sku=? AND http_status=201 "
                 "ORDER BY submission_id DESC LIMIT 1", (order_ref, sku), one=True)

    # ---------------------------------------------------------------- routes

    @app.route("/")
    def index():
        cat = request.args.get("cat", "")
        rows = q("SELECT * FROM Product WHERE (?='' OR category=?) ORDER BY rowid", (cat, cat))
        cats = [r[0] for r in q("SELECT DISTINCT category FROM Product ORDER BY category")]
        return render_template("index.html", products=rows, categories=cats, cat=cat)

    @app.route("/p/<sku>")
    def product(sku):
        p = product_or_404(sku)
        owned = None
        if g.user:
            owned = q("SELECT o.order_ref, (SELECT 1 FROM Review_Submission s WHERE s.order_ref=o.order_ref AND s.sku=? "
                      "AND s.http_status=201) AS reviewed FROM Orders o JOIN Order_Item i ON i.order_id=o.order_id "
                      "WHERE o.customer_id=? AND i.sku=? ORDER BY o.order_id DESC LIMIT 1",
                      (sku, g.user["customer_id"], sku), one=True)
        more = q("SELECT * FROM Product WHERE sku<>? AND category=? UNION ALL "
                 "SELECT * FROM Product WHERE sku<>? AND category<>? LIMIT 4", (sku, p["category"], sku, p["category"]))
        return render_template("product.html", p=p, owned=owned, more=more)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            c = q("SELECT * FROM Customer WHERE email=?", (email,), one=True)
            if c and check_password_hash(c["password_hash"], request.form.get("password", "")):
                session["cid"] = c["customer_id"]
                flash(f"Welcome back, {c['full_name'].split()[0]}.", "success")
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
            error = "Incorrect email or password."
        return render_template("login.html", error=error,
                               demo=q("SELECT full_name, email FROM Customer ORDER BY customer_id"))

    @app.route("/logout")
    def logout():
        session.pop("cid", None)
        flash("Signed out.", "success")
        return redirect(url_for("index"))

    @app.route("/buy/<sku>", methods=["POST"])
    @login_required
    def buy(sku):
        p = product_or_404(sku)
        conn = db()
        n = conn.execute("SELECT COALESCE(MAX(order_id), 0) FROM Orders").fetchone()[0]
        ref = f"{cfg['order_prefix']}-{1001 + n}"
        oid = conn.execute("INSERT INTO Orders (order_ref, customer_id) VALUES (?,?)", (ref, g.user["customer_id"])).lastrowid
        conn.execute("INSERT INTO Order_Item (order_id, sku, price_cents) VALUES (?,?,?)", (oid, sku, p["price_cents"]))
        conn.commit()
        try:
            register_order(conn, ref)
            flash(f"Order {ref} placed and registered with Reviewly. You can review it from your orders.", "success")
        except ReviewlyError as e:
            flash(f"Order {ref} placed. Reviewly sync failed ({e.reason}); we'll retry on restart.", "error")
        return redirect(url_for("orders"))

    @app.route("/orders")
    @login_required
    def orders():
        rows = q("""
            SELECT o.order_ref, o.created_at, o.reviewly_synced, i.sku, i.price_cents, p.name,
                   s.review_id, s.review_status, s.receipt_url
              FROM Orders o JOIN Order_Item i ON i.order_id=o.order_id JOIN Product p ON p.sku=i.sku
              LEFT JOIN Review_Submission s ON s.order_ref=o.order_ref AND s.sku=i.sku AND s.http_status=201
             WHERE o.customer_id=? ORDER BY o.order_id DESC""", (g.user["customer_id"],))
        return render_template("orders.html", rows=rows)

    @app.route("/orders/<ref>/review/<sku>", methods=["GET", "POST"])
    @login_required
    def review(ref, sku):
        p = product_or_404(sku)
        order = q("SELECT o.* FROM Orders o JOIN Order_Item i ON i.order_id=o.order_id "
                  "WHERE o.order_ref=? AND i.sku=? AND o.customer_id=?", (ref, sku, g.user["customer_id"]), one=True)
        if not order:
            abort(404)
        if submission_for(ref, sku):
            flash("You've already reviewed this. Edit it from your Reviewly receipt.", "error")
            return redirect(url_for("orders"))
        error, form = None, request.form
        if request.method == "POST":
            if not order["reviewly_synced"]:
                try:
                    register_order(db(), ref)
                except ReviewlyError:
                    pass
            crit = {c: form.get(c.lower(), type=int) for c in CRITERIA if form.get(c.lower())}
            status, data, error = submit_review(db(), ref, sku, g.user, form.get("rating", type=int),
                                                form.get("title", "").strip(), form.get("review_text", "").strip(), crit)
            if status == 201:
                flash("Thanks! Reviewly verified your purchase and published your review." if data.get("status") == "Approved"
                      else "Thanks! Reviewly is holding your review for a quick moderation check.", "success")
                return redirect(url_for("orders", done=data.get("review_id")))
        return render_template("review.html", p=p, ref=ref, error=error, form=form)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("base.html", not_found=True), 404

    app.register_blueprint(admin_blueprint(cfg, q, db, client, connect))
    init_db()
    threading.Thread(target=sync_with_reviewly, daemon=True).start()
    return app


# ==================================================================== store admin
# The same admin UI serves every shop, the way a hosted commerce platform's admin does.

def fetch_public(path):
    try:
        with urllib.request.urlopen(REVIEWLY_URL + path, timeout=4) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def admin_blueprint(cfg, q, db, client, connect):
    bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder=os.path.join(ADMIN_DIR, "templates"),
                   static_folder=os.path.join(ADMIN_DIR, "static"), static_url_path="/assets")

    @bp.before_app_request
    def load_staff():
        g.staff = q("SELECT * FROM Staff WHERE staff_id=?", (session["staff_id"],), one=True) if "staff_id" in session else None

    @bp.before_request
    def require_staff():
        if request.endpoint not in ("admin.login", "admin.static") and g.staff is None:
            return redirect(url_for("admin.login", next=request.path))

    def reviewly_ratings():
        skus = ",".join(r["sku"] for r in q("SELECT sku FROM Product"))
        return fetch_public(f"/api/v1/public/summary?key={cfg['public_key']}&skus={skus}")

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            s = q("SELECT * FROM Staff WHERE email=?", (request.form.get("email", "").strip().lower(),), one=True)
            if s and check_password_hash(s["password_hash"], request.form.get("password", "")):
                session["staff_id"] = s["staff_id"]
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/admin") else url_for("admin.overview"))
            error = "Incorrect email or password."
        return render_template("admin/login.html", error=error)

    @bp.route("/logout")
    def logout():
        session.pop("staff_id", None)
        return redirect(url_for("admin.login"))

    @bp.route("/")
    def overview():
        stats = q("""SELECT COUNT(DISTINCT o.order_id) AS orders, COALESCE(SUM(i.price_cents), 0) AS revenue,
                            COUNT(DISTINCT o.customer_id) AS customers,
                            SUM(o.created_at >= datetime('now', '-7 days')) AS week
                       FROM Orders o JOIN Order_Item i ON i.order_id=o.order_id""", one=True)
        ratings = reviewly_ratings()
        reviews = sum(r["total"] for r in ratings.values()) if ratings else None
        avg = (sum(r["average"] * r["total"] for r in ratings.values()) / reviews) if ratings and reviews else None
        recent = q("""SELECT o.*, c.full_name, p.name, i.price_cents FROM Orders o JOIN Customer c ON c.customer_id=o.customer_id
                        JOIN Order_Item i ON i.order_id=o.order_id JOIN Product p ON p.sku=i.sku
                       ORDER BY o.order_id DESC LIMIT 6""")
        products = q("""SELECT p.*, COUNT(i.order_id) AS sold FROM Product p LEFT JOIN Order_Item i ON i.sku=p.sku
                         GROUP BY p.sku ORDER BY p.rowid""")
        return render_template("admin/overview.html", stats=stats, ratings=ratings, reviews=reviews, avg=avg,
                               recent=recent, products=products)

    @bp.route("/orders")
    def orders():
        rows = q("""SELECT o.*, c.full_name, c.email, p.name, i.sku, i.price_cents,
                           s.review_id, s.review_status, s.receipt_url
                      FROM Orders o JOIN Customer c ON c.customer_id=o.customer_id
                      JOIN Order_Item i ON i.order_id=o.order_id JOIN Product p ON p.sku=i.sku
                      LEFT JOIN Review_Submission s ON s.order_ref=o.order_ref AND s.sku=i.sku AND s.http_status=201
                     ORDER BY o.order_id DESC""")
        return render_template("admin/orders.html", rows=rows)

    @bp.route("/reviews")
    def reviews():
        sku = request.args.get("sku") or None
        products = q("SELECT * FROM Product ORDER BY rowid")
        feed = []
        for p in products:
            if sku and p["sku"] != sku:
                continue
            data = fetch_public(f"/api/v1/public/products/{p['sku']}?key={cfg['public_key']}")
            if data is None:
                return render_template("admin/reviews.html", feed=None, products=products, sku=sku)
            feed += [{**r, "sku": p["sku"], "product": p["name"], "product_url": data["product"]["url"]} for r in data["reviews"]]
        feed.sort(key=lambda r: r["date"], reverse=True)
        return render_template("admin/reviews.html", feed=feed, products=products, sku=sku)

    @bp.route("/integration")
    def integration():
        return render_template("admin/integration.html",
                               calls=q("SELECT * FROM Api_Call ORDER BY call_id DESC LIMIT 40"),
                               subs=q("SELECT s.*, c.full_name FROM Review_Submission s JOIN Customer c "
                                      "ON c.customer_id=s.customer_id ORDER BY submission_id DESC LIMIT 15"),
                               result=session.pop("check", None))

    CHECKS = {
        "no_order": "Submit a review for an order that doesn't exist",
        "replay": "Re-send the last signed request unchanged",
        "bad_signature": "Sign a request with the wrong secret",
        "edit": "Edit an existing review (PUT /api/v1/reviews/1)",
        "delete": "Delete an existing review (DELETE /api/v1/reviews/1)",
    }

    @bp.route("/integration/check/<kind>", methods=["POST"])
    def check(kind):
        if kind not in CHECKS:
            abort(404)
        fake = {"order_reference": f"{cfg['order_prefix']}-0000", "sku": cfg["catalog"][0]["sku"],
                "customer_email": "someone@example.com", "rating": 5, "title": "Great",
                "text": "Excellent product, would buy again."}
        try:
            if kind == "no_order":
                data = client.call("POST", "/api/v1/reviews", fake)
            elif kind == "replay":
                if not client.last_request:
                    client.call("POST", "/api/v1/products", {"products": [
                        {k: cfg["catalog"][0][k] for k in ("sku", "name", "category")}]})
                data = client.send(client.last_request)
            elif kind == "bad_signature":
                data = client.send(client.build("POST", "/api/v1/reviews", fake, secret="sk_wrong_secret"))
            elif kind == "edit":
                data = client.call("PUT", "/api/v1/reviews/1", {"rating": 5, "text": "Changed text."})
            else:
                data = client.call("DELETE", "/api/v1/reviews/1")
            session["check"] = {"label": CHECKS[kind], "status": 200, "passed": False, "message": f"Accepted: {data}"}
        except ReviewlyError as e:
            session["check"] = {"label": CHECKS[kind], "status": e.status, "passed": e.status >= 400, "message": e.reason}
        return redirect(url_for("admin.integration"))

    @bp.app_context_processor
    def admin_ctx():
        return {"checks": CHECKS, "reviewly_merchant_url": f"{REVIEWLY_URL}/merchant"}

    return bp
