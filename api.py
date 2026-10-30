"""Reviewly public API: companies push catalog, orders and reviews; websites read reviews back.

Signed requests carry X-Reviewly-Key / -Timestamp / -Nonce / -Signature, where
Signature = hex(HMAC-SHA256(secret, f"{timestamp}.{nonce}.{METHOD}.{path}.{body}")).
"""
import hmac
import hashlib
import json
import re
import sqlite3
import time
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request, send_from_directory, url_for

from db import CRITERIA, get_db, new_token, q, sentiment, validate_review

bp = Blueprint("api", __name__)

MAX_SKEW = 300          # seconds a signed request stays valid
BURST_LIMIT = 20        # reviews per client per 10 minutes before new ones are held for moderation
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def log_call(client_id, status, outcome, reason=None):
    db = get_db()
    db.execute("INSERT INTO Api_Request_Log (client_id, method, endpoint, status_code, outcome, reason) "
               "VALUES (?,?,?,?,?,?)", (client_id, request.method, request.path, status, outcome, reason))
    db.commit()


def authenticate():
    key = request.headers.get("X-Reviewly-Key", "")
    client = q("SELECT * FROM Api_Client WHERE public_key=?", (key,), one=True)
    if not client or not client["is_active"]:
        raise ApiError(401, "Unknown or disabled API key.")
    g.client = client
    ts, nonce = request.headers.get("X-Reviewly-Timestamp", ""), request.headers.get("X-Reviewly-Nonce", "")
    sig = request.headers.get("X-Reviewly-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW:
        raise ApiError(401, "Request timestamp is missing or outside the 5 minute window.")
    if not 8 <= len(nonce) <= 64:
        raise ApiError(401, "Missing nonce.")
    msg = f"{ts}.{nonce}.{request.method}.{request.path}.{request.get_data(as_text=True)}"
    expect = hmac.new(client["api_secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise ApiError(401, "Signature does not match. The request was altered or signed with the wrong secret.")
    db = get_db()
    try:
        db.execute("INSERT INTO Api_Nonce (client_id, nonce) VALUES (?,?)", (client["client_id"], nonce))
    except sqlite3.IntegrityError:
        raise ApiError(409, "Replay detected: this nonce was already used.")
    db.execute("UPDATE Api_Client SET last_used_at=CURRENT_TIMESTAMP WHERE client_id=?", (client["client_id"],))


def signed(fn):
    """Authenticate, run, commit, and log the outcome. Any ApiError rolls back and is logged as Rejected."""
    @wraps(fn)
    def wrapper(*a, **kw):
        g.client, g.outcome = None, "Accepted"
        db = get_db()
        try:
            authenticate()
            db.commit()  # the nonce stays burned even if the request is rejected below
            if request.method == "POST" and not isinstance(request.get_json(silent=True), dict):
                raise ApiError(400, "Body must be a JSON object.")
            body, status = fn(*a, **kw)
            db.commit()
            log_call(g.client["client_id"], status, g.outcome, body.get("flag_reason"))
            return jsonify(body), status
        except ApiError as e:
            db.rollback()
            log_call(g.client["client_id"] if g.client else None, e.status, "Rejected", e.reason)
            return jsonify(error=e.reason), e.status
    return wrapper


def own_product(sku):
    p = q("SELECT * FROM Product WHERE sku=?", (sku,), one=True)
    if not p or p["merchant_id"] != g.client["merchant_id"]:
        raise ApiError(422, f"Unknown SKU '{sku}' for this company. Sync the catalog first.")
    return p


def line_ref(order_reference, sku):
    """Transactions holds one product per row, so each order line gets its own reference."""
    return f"{order_reference}:{sku}"


def review_json(r, full=False):
    out = {"review_id": r["review_id"], "status": r["status"],
           "receipt_url": url_for("receipt", token=r["receipt_token"], _external=True),
           "verify_url": url_for("verify_review", rid=r["review_id"], _external=True)}
    if full:
        out.update(rating=r["rating"], title=r["title"], text=r["review_text"], flag_reason=r["flag_reason"])
    return out


# ------------------------------------------------------------------ signed endpoints

@bp.route("/api/v1/products", methods=["POST"])
@signed
def sync_products():
    items = request.get_json().get("products")
    if not isinstance(items, list) or not items:
        raise ApiError(400, "Send {\"products\": [{sku, name, category}, ...]}.")
    db, mid, created, updated = get_db(), g.client["merchant_id"], 0, 0
    for it in items:
        sku, name, cat = (str(it.get(k, "")).strip() for k in ("sku", "name", "category"))
        image = str(it.get("image_url") or "").strip() or None
        if image and not image.startswith(("http://", "https://")):
            raise ApiError(422, f"Product '{sku}': image_url must be an absolute http(s) URL.")
        if len(sku) < 3 or len(name) < 2 or len(cat) < 2:
            raise ApiError(422, f"Product '{sku or '?'}' needs sku (3+), name and category.")
        db.execute("INSERT OR IGNORE INTO Category (category_name) VALUES (?)", (cat,))
        cid = q("SELECT category_id FROM Category WHERE category_name=?", (cat,), one=True)[0]
        existing = q("SELECT merchant_id FROM Product WHERE sku=?", (sku,), one=True)
        if existing and existing["merchant_id"] != mid:
            raise ApiError(409, f"SKU '{sku}' belongs to another company.")
        if existing:
            db.execute("UPDATE Product SET product_name=?, category_id=?, image_url=? WHERE sku=?", (name, cid, image, sku))
            updated += 1
        else:
            db.execute("INSERT INTO Product (merchant_id, category_id, product_name, sku, image_url) VALUES (?,?,?,?,?)",
                       (mid, cid, name, sku, image))
            created += 1
    return {"created": created, "updated": updated}, 200


@bp.route("/api/v1/orders", methods=["POST"])
@signed
def register_order():
    data = request.get_json()
    ref = str(data.get("order_reference", "")).strip()
    cust = data.get("customer") or {}
    email, name = str(cust.get("email", "")).strip().lower(), str(cust.get("name", "")).strip()
    skus = data.get("items")
    if len(ref) < 3 or not EMAIL_RE.match(email) or len(name) < 2 or not isinstance(skus, list) or not skus:
        raise ApiError(400, "Send order_reference, customer {email, name} and a non-empty items list of SKUs.")
    db, cid = get_db(), g.client["client_id"]
    user = q("SELECT * FROM Users WHERE email=?", (email,), one=True)
    if user and user["role"] != "Customer":
        raise ApiError(422, "That email belongs to a staff account and can't place orders.")
    if not user:
        # Shadow account: password '!' never matches a hash, until the person claims it by signing up.
        uid = db.execute("INSERT INTO Users (full_name, email, password_hash, role) VALUES (?,?,'!','Customer')",
                         (name, email)).lastrowid
    else:
        uid = user["user_id"]
    registered = 0
    for sku in skus:
        p = own_product(str(sku))
        lref = line_ref(ref, p["sku"])
        t = q("SELECT * FROM Transactions WHERE order_reference=?", (lref,), one=True)
        if t:
            if t["source_client_id"] != cid or t["user_id"] != uid:
                raise ApiError(409, f"Order line {lref} already exists with different details.")
            continue
        db.execute("INSERT INTO Transactions (user_id, product_id, order_reference, source_client_id) VALUES (?,?,?,?)",
                   (uid, p["product_id"], lref, cid))
        registered += 1
    return {"order_reference": ref, "lines_registered": registered, "lines_total": len(skus)}, 201 if registered else 200


@bp.route("/api/v1/reviews", methods=["POST"])
@signed
def submit_review():
    data = request.get_json()
    ref, sku = str(data.get("order_reference", "")).strip(), str(data.get("sku", "")).strip()
    email = str(data.get("customer_email", "")).strip().lower()
    title, text = str(data.get("title") or "").strip(), str(data.get("text") or "").strip()
    rating = data.get("rating")
    crit = data.get("criteria") or {}
    scores = {c: crit.get(c) for c in CRITERIA if c in crit}
    problems = validate_review(rating if isinstance(rating, int) else None, title, text, scores)
    if problems:
        raise ApiError(422, "Invalid review: " + ", ".join(problems) + ".")

    p = own_product(sku)
    t = q("SELECT t.*, u.email FROM Transactions t JOIN Users u ON u.user_id=t.user_id "
          "WHERE t.order_reference=? AND t.source_client_id=?", (line_ref(ref, sku), g.client["client_id"]), one=True)
    if not t or t["email"] != email or t["product_id"] != p["product_id"]:
        raise ApiError(422, "No matching order: this company never registered a purchase of this product "
                            "by this customer, so the review can't be verified.")
    prior = q("SELECT * FROM Review WHERE transaction_id=? OR (user_id=? AND product_id=?)",
              (t["transaction_id"], t["user_id"], p["product_id"]), one=True)
    if prior:
        raise ApiError(409, f"Already reviewed: review #{prior['review_id']} exists for this customer and product. "
                            "Customers edit their own review from their Reviewly receipt.")

    flags = []
    if q("SELECT 1 FROM Review WHERE review_text=? AND user_id<>?", (text, t["user_id"]), one=True):
        flags.append("identical text already posted by another reviewer")
    burst = q("SELECT COUNT(*) FROM Review WHERE source_client_id=? AND created_at >= datetime('now', '-10 minutes')",
              (g.client["client_id"],), one=True)[0]
    if burst >= BURST_LIMIT:
        flags.append(f"more than {BURST_LIMIT} reviews from this company in 10 minutes")
    flag_reason = "; ".join(flags) or None
    status = "Pending" if flags else "Approved"

    db = get_db()
    rid = db.execute(
        "INSERT INTO Review (user_id, product_id, transaction_id, rating, title, review_text, is_verified_purchase, "
        "sentiment_score, status, source_client_id, receipt_token, flag_reason) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
        (t["user_id"], p["product_id"], t["transaction_id"], rating, title or None, text,
         sentiment(f"{title} {text}"), status, g.client["client_id"], new_token(), flag_reason)).lastrowid
    for c, s in scores.items():
        db.execute("INSERT INTO Review_Criteria_Rating (review_id, criteria_name, score) VALUES (?,?,?)", (rid, c, s))
    review = q("SELECT * FROM Review WHERE review_id=?", (rid,), one=True)
    entry = q("SELECT entry_hash FROM Review_Ledger WHERE review_id=? ORDER BY ledger_id DESC LIMIT 1", (rid,), one=True)
    if flags:
        g.outcome = "Flagged"
    return {**review_json(review), "entry_hash": entry[0], "flag_reason": flag_reason}, 201


@bp.route("/api/v1/reviews/<int:rid>")
@signed
def review_status(rid):
    r = q("SELECT * FROM Review WHERE review_id=? AND source_client_id=?", (rid, g.client["client_id"]), one=True)
    if not r:
        raise ApiError(404, "No review with that id was submitted by this company.")
    return review_json(r, full=True), 200


@bp.app_errorhandler(405)
def method_not_allowed(e):
    if not request.path.startswith("/api/"):
        return e
    client = q("SELECT client_id FROM Api_Client WHERE public_key=?", (request.headers.get("X-Reviewly-Key", ""),), one=True)
    reason = ("Reviewly has no endpoint for companies to edit or delete reviews. Only the reviewer can edit, "
              "from their private receipt." if "/reviews" in request.path else "Method not allowed.")
    log_call(client[0] if client else None, 405, "Rejected", reason)
    return jsonify(error=reason), 405


# ------------------------------------------------------------------ public, read-only

@bp.after_request
def cors(resp):
    if request.path.startswith("/api/v1/public") or request.path in ("/widget.js", "/api/v1/health"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@bp.route("/api/v1/health")
def health():
    return jsonify(ok=True, service="reviewly")


@bp.route("/widget.js")
def widget():
    resp = send_from_directory(current_app.static_folder, "widget.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/api/v1/public/summary")
def public_summary():
    """Star snippets for product grids: ?key=pk_…&skus=A,B,C → {sku: {average, total}}."""
    if not q("SELECT 1 FROM Api_Client WHERE public_key=? AND is_active=1", (request.args.get("key", ""),), one=True):
        return jsonify(error="Unknown public key."), 401
    skus = [s for s in request.args.get("skus", "").split(",") if s][:100]
    rows = q(f"SELECT sku, avg_rating, total_reviews FROM Product WHERE sku IN ({','.join('?' * len(skus))})", skus) if skus else []
    return jsonify({r["sku"]: {"average": r["avg_rating"], "total": r["total_reviews"]} for r in rows})


def short_name(full):
    parts = full.split()
    return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")


@bp.route("/api/v1/public/products/<sku>")
def public_product(sku):
    if not q("SELECT 1 FROM Api_Client WHERE public_key=? AND is_active=1", (request.args.get("key", ""),), one=True):
        return jsonify(error="Unknown public key."), 401
    p = q("SELECT * FROM v_product_stats WHERE sku=?", (sku,), one=True)
    if not p:
        return jsonify(error="Product not found on Reviewly."), 404
    pid = p["product_id"]
    dist = {r["rating"]: r["count"] for r in q("SELECT rating, count FROM v_rating_distribution WHERE product_id=?", (pid,))}
    crit = {r[0]: r[1] for r in q(
        "SELECT c.criteria_name, ROUND(AVG(c.score),1) FROM Review_Criteria_Rating c JOIN Review r ON r.review_id=c.review_id "
        "WHERE r.product_id=? AND r.status='Approved' GROUP BY c.criteria_name", (pid,))}
    rows = q("""
        SELECT r.review_id, r.rating, r.title, r.review_text, r.created_at, r.is_verified_purchase,
               u.full_name, h.up, h.down, pv.source_name, pv.entry_hash,
               mr.response_text, mr.responded_at,
               (SELECT COUNT(*) FROM Review_Revision rv WHERE rv.review_id=r.review_id) AS revisions
          FROM Review r
          JOIN Users u ON u.user_id=r.user_id
          JOIN v_helpfulness h ON h.review_id=r.review_id
          JOIN v_review_provenance pv ON pv.review_id=r.review_id
          LEFT JOIN Merchant_Response mr ON mr.review_id=r.review_id
         WHERE r.product_id=? AND r.status='Approved'
         ORDER BY h.score DESC, r.created_at DESC LIMIT 50""", (pid,))
    return jsonify(
        product={"sku": p["sku"], "name": p["product_name"], "seller": p["company_name"],
                 "url": url_for("product_detail", pid=pid, _external=True),
                 "company_url": url_for("company", mid=p["merchant_id"], _external=True)},
        summary={"average": p["avg_rating"], "total": p["total_reviews"], "verified": p["verified_count"],
                 "distribution": {str(s): dist.get(s, 0) for s in range(5, 0, -1)}, "criteria": crit},
        reviews=[{"id": r["review_id"], "rating": r["rating"], "title": r["title"], "text": r["review_text"],
                  "author": short_name(r["full_name"]), "date": r["created_at"][:10],
                  "verified": bool(r["is_verified_purchase"]), "source": r["source_name"],
                  "edited": r["revisions"] > 0, "helpful": r["up"], "hash": r["entry_hash"],
                  "verify_url": url_for("verify_review", rid=r["review_id"], _external=True),
                  "reply": {"text": r["response_text"], "date": r["responded_at"][:10]} if r["response_text"] else None}
                 for r in rows],
        issued_by="Reviewly", issued_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
