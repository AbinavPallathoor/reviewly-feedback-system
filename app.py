"""Reviewly - Customer Feedback and Review Management System (Flask + sqlite3)."""
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from api import bp as api_bp
from db import (CRITERIA, close_db, content_string, get_db, init_db, new_token, q, sentiment, sha256,
                validate_review, verify_ledger)

app = Flask(__name__)
app.secret_key = "reviewly-dev-secret"
app.teardown_appcontext(close_db)
app.register_blueprint(api_bp)


# ------------------------------------------------------------------ helpers

@app.before_request
def load_user():
    g.user = None
    if "uid" in session:
        g.user = get_db().execute("SELECT * FROM Users WHERE user_id=?", (session["uid"],)).fetchone()
        if g.user is None:
            session.clear()


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                flash("Please log in first.", "error")
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


login_required = role_required()


def current_merchant():
    return q("SELECT * FROM Merchant WHERE user_id=?", (g.user["user_id"],), one=True)


def distribution(where="", args=()):
    rows = q(f"SELECT rating, SUM(count) AS n FROM v_rating_distribution {where} GROUP BY rating", args)
    counts = {r["rating"]: r["n"] for r in rows}
    total = sum(counts.values())
    return [{"rating": s, "count": counts.get(s, 0),
             "pct": round(100 * counts.get(s, 0) / total) if total else 0} for s in range(5, 0, -1)]


def sentiment_label(score):
    if score is None:
        return "Neutral"
    return "Positive" if score > 0.1 else "Negative" if score < -0.1 else "Neutral"


def product_img(p):
    """Local static/img/products/<sku>.jpg, else the image URL the company synced, else None (initials)."""
    name = f"img/products/{p['sku']}.jpg"
    if os.path.exists(os.path.join(app.static_folder, name)):
        return url_for("static", filename=name)
    return p["image_url"] if "image_url" in p.keys() else None


app.jinja_env.globals.update(sentiment_label=sentiment_label, CRITERIA=CRITERIA, product_img=product_img,
                             pending_count=lambda: q("SELECT COUNT(*) FROM Review_Report WHERE report_status='Pending'", one=True)[0])


@app.context_processor
def inject_endpoint():
    return {"ep": request.endpoint or ""}


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, message="You don't have access to this page."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="That page doesn't exist."), 404


# ------------------------------------------------------------------ auth

@app.route("/")
def index():
    if g.user and g.user["role"] == "Admin":
        return redirect(url_for("admin_overview"))
    if g.user and g.user["role"] == "Merchant":
        return redirect(url_for("merchant_dashboard"))
    return redirect(url_for("companies"))


@app.route("/login", methods=["GET", "POST"])
def login():
    errors = {}
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = q("SELECT * FROM Users WHERE email=?", (email,), one=True)
        if not email:
            errors["email"] = "Email is required."
        elif not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            errors["password"] = "Incorrect email or password."
        else:
            session.clear()
            session["uid"] = user["user_id"]
            flash(f"Welcome back, {user['full_name'].split()[0]}.", "success")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
    return render_template("login.html", errors=errors)


@app.route("/register", methods=["GET", "POST"])
def register():
    errors, form = {}, request.form
    if request.method == "POST":
        name, email, pw = form.get("full_name", "").strip(), form.get("email", "").strip().lower(), form.get("password", "")
        if len(name) < 2:
            errors["full_name"] = "Enter your full name."
        if "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Enter a valid email address."
        if len(pw) < 6:
            errors["password"] = "Password must be at least 6 characters."
        if pw != form.get("confirm", ""):
            errors["confirm"] = "Passwords do not match."
        if not errors:
            db = get_db()
            shadow = q("SELECT user_id FROM Users WHERE email=? AND password_hash='!'", (email,), one=True)
            try:
                if shadow:
                    # Created when a partner shop registered an order: the shopper now claims it.
                    db.execute("UPDATE Users SET full_name=?, password_hash=? WHERE user_id=?",
                               (name, generate_password_hash(pw), shadow["user_id"]))
                    uid = shadow["user_id"]
                else:
                    uid = db.execute("INSERT INTO Users (full_name, email, password_hash, role) VALUES (?,?,?, 'Customer')",
                                     (name, email, generate_password_hash(pw))).lastrowid
                db.commit()
                session.clear()
                session["uid"] = uid
                flash("Account claimed. Reviews from partner shops are now under your name." if shadow
                      else "Account created. Welcome to Reviewly!", "success")
                return redirect(url_for("companies"))
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists."
    return render_template("register.html", errors=errors, form=form)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


# ------------------------------------------------------------------ customer

@app.route("/products/<int:pid>")
def product_detail(pid):
    product = q("SELECT * FROM v_product_stats WHERE product_id=?", (pid,), one=True)
    if not product:
        abort(404)
    uid = g.user["user_id"] if g.user else -1
    reviews = q("""
        SELECT r.*, u.full_name, h.up, h.down, h.score, pv.source_name, pv.entry_hash,
               mr.response_text, mr.responded_at, rm.media_url, rm.media_type,
               (SELECT COUNT(*) FROM Review_Revision rv WHERE rv.review_id=r.review_id) AS revisions,
               (SELECT vote_type FROM Review_Vote v WHERE v.review_id=r.review_id AND v.user_id=?) AS my_vote,
               (SELECT GROUP_CONCAT(criteria_name || ':' || score) FROM Review_Criteria_Rating c
                 WHERE c.review_id=r.review_id) AS criteria
          FROM Review r
          JOIN Users u ON u.user_id=r.user_id
          JOIN v_helpfulness h ON h.review_id=r.review_id
          JOIN v_review_provenance pv ON pv.review_id=r.review_id
          LEFT JOIN Merchant_Response mr ON mr.review_id=r.review_id
          LEFT JOIN Review_Media rm ON rm.review_id=r.review_id
         WHERE r.product_id=? AND (r.status='Approved' OR r.user_id=?)
         GROUP BY r.review_id
         ORDER BY h.score DESC, r.created_at DESC""", (uid, pid, uid))
    crit = {r["criteria_name"]: r["avg"] for r in q(
        "SELECT c.criteria_name, ROUND(AVG(c.score),1) AS avg FROM Review_Criteria_Rating c "
        "JOIN Review r ON r.review_id=c.review_id WHERE r.product_id=? AND r.status='Approved' "
        "GROUP BY c.criteria_name", (pid,))}
    mine = None
    if g.user:
        mine = q("SELECT r.*, (SELECT media_url FROM Review_Media m WHERE m.review_id=r.review_id) AS media_url "
                 "FROM Review r WHERE user_id=? AND product_id=?", (uid, pid), one=True)
        if mine:
            mine = dict(mine)
            mine["criteria"] = {r["criteria_name"]: r["score"] for r in q(
                "SELECT criteria_name, score FROM Review_Criteria_Rating WHERE review_id=?", (mine["review_id"],))}
    purchased = bool(g.user and q("SELECT 1 FROM Transactions WHERE user_id=? AND product_id=?", (uid, pid), one=True))
    return render_template("product.html", p=product, reviews=reviews, crit=crit, mine=mine,
                           purchased=purchased, dist=distribution("WHERE product_id=?", (pid,)))


@app.route("/products/<int:pid>/review", methods=["POST"])
@role_required("Customer")
def save_review(pid):
    if not q("SELECT 1 FROM Product WHERE product_id=?", (pid,), one=True):
        abort(404)
    f = request.form
    rating = f.get("rating", type=int)
    title, text, media = f.get("title", "").strip(), f.get("review_text", "").strip(), f.get("media_url", "").strip()
    scores = {c: f.get(c.lower(), type=int) for c in CRITERIA}
    problems = validate_review(rating, title, text, scores)
    if media and not media.startswith(("http://", "https://")):
        problems.append("media URL must start with http(s)://")
    if problems:
        flash("Please " + ", ".join(problems) + ".", "error")
        return redirect(url_for("product_detail", pid=pid) + "#write")

    db, uid = get_db(), g.user["user_id"]
    score = sentiment(f"{title} {text}")
    try:
        existing = q("SELECT review_id FROM Review WHERE user_id=? AND product_id=?", (uid, pid), one=True)
        if existing:
            rid = existing["review_id"]
            db.execute("UPDATE Review SET rating=?, title=?, review_text=?, sentiment_score=? WHERE review_id=?",
                       (rating, title, text, score, rid))
        else:
            db.execute("INSERT INTO Review (user_id, product_id, rating, title, review_text, sentiment_score, receipt_token) "
                       "VALUES (?,?,?,?,?,?,?)", (uid, pid, rating, title, text, score, new_token()))
            # lookup by natural key: the verify trigger may re-insert the row
            rid = q("SELECT review_id FROM Review WHERE user_id=? AND product_id=?", (uid, pid), one=True)[0]
        for c, s in scores.items():
            db.execute("INSERT INTO Review_Criteria_Rating (review_id, criteria_name, score) VALUES (?,?,?) "
                       "ON CONFLICT(review_id, criteria_name) DO UPDATE SET score=excluded.score", (rid, c, s))
        db.execute("DELETE FROM Review_Media WHERE review_id=?", (rid,))
        if media:
            kind = "video" if media.lower().endswith((".mp4", ".webm")) else "image"
            db.execute("INSERT INTO Review_Media (review_id, media_url, media_type) VALUES (?,?,?)", (rid, media, kind))
        db.commit()
        flash("Review updated." if existing else "Thanks! Your review is live.", "success")
    except sqlite3.IntegrityError as e:
        db.rollback()
        flash("That media URL is already used by another review." if "media_url" in str(e) else f"Could not save: {e}", "error")
    return redirect(url_for("product_detail", pid=pid))


@app.route("/reviews/<int:rid>/vote", methods=["POST"])
@login_required
def vote(rid):
    review = q("SELECT * FROM Review WHERE review_id=?", (rid,), one=True)
    if not review:
        abort(404)
    kind = (request.form.get("type") or (request.get_json(silent=True) or {}).get("type") or "").capitalize()
    if kind not in ("Up", "Down"):
        return jsonify(error="Invalid vote."), 400
    if review["user_id"] == g.user["user_id"]:
        return jsonify(error="You can't vote on your own review."), 400
    db, uid = get_db(), g.user["user_id"]
    cur = q("SELECT vote_type FROM Review_Vote WHERE review_id=? AND user_id=?", (rid, uid), one=True)
    if cur and cur["vote_type"] == kind:
        db.execute("DELETE FROM Review_Vote WHERE review_id=? AND user_id=?", (rid, uid))
        mine = None
    elif cur:
        db.execute("UPDATE Review_Vote SET vote_type=?, voted_at=CURRENT_TIMESTAMP WHERE review_id=? AND user_id=?",
                   (kind, rid, uid))
        mine = kind
    else:
        db.execute("INSERT INTO Review_Vote (review_id, user_id, vote_type) VALUES (?,?,?)", (rid, uid, kind))
        mine = kind
    db.commit()
    h = q("SELECT up, down, score FROM v_helpfulness WHERE review_id=?", (rid,), one=True)
    return jsonify(up=h["up"], down=h["down"], score=h["score"], mine=mine)


@app.route("/reviews/<int:rid>/report", methods=["POST"])
@login_required
def report(rid):
    review = q("SELECT * FROM Review WHERE review_id=?", (rid,), one=True)
    if not review:
        abort(404)
    reason = request.form.get("reason", "").strip()
    if review["user_id"] == g.user["user_id"]:
        flash("You can't report your own review.", "error")
    elif len(reason) < 5:
        flash("Please give a reason (at least 5 characters).", "error")
    else:
        db = get_db()
        try:
            db.execute("INSERT INTO Review_Report (review_id, reporter_user_id, report_reason) VALUES (?,?,?)",
                       (rid, g.user["user_id"], reason))
            db.commit()
            flash("Report submitted. Our moderators will take a look.", "success")
        except sqlite3.IntegrityError:
            flash("You've already reported this review.", "error")
    return redirect(url_for("product_detail", pid=review["product_id"]))


# ------------------------------------------------------------------ merchant

@app.route("/merchant")
@role_required("Merchant")
def merchant_dashboard():
    m = current_merchant()
    if not m:
        abort(403)
    mid = m["merchant_id"]
    prods = q("SELECT * FROM v_product_stats WHERE merchant_id=? ORDER BY product_name", (mid,))
    agg = q("SELECT COUNT(*) AS n, ROUND(AVG(r.rating),2) AS avg, ROUND(AVG(r.sentiment_score),2) AS sent, "
            "SUM(r.sentiment_score > 0.1) AS pos, SUM(r.sentiment_score < -0.1) AS neg, "
            "SUM(r.is_verified_purchase) AS verified "
            "FROM Review r JOIN Product p ON p.product_id=r.product_id WHERE p.merchant_id=? AND r.status='Approved'",
            (mid,), one=True)
    recent = q("""
        SELECT r.*, u.full_name, p.product_name, mr.response_text, mr.responded_at
          FROM Review r JOIN Product p ON p.product_id=r.product_id
          JOIN Users u ON u.user_id=r.user_id
          LEFT JOIN Merchant_Response mr ON mr.review_id=r.review_id
         WHERE p.merchant_id=? AND r.status='Approved'
         ORDER BY (mr.response_id IS NULL) DESC, r.created_at DESC LIMIT 25""", (mid,))
    dist = distribution("WHERE product_id IN (SELECT product_id FROM Product WHERE merchant_id=?)", (mid,))
    return render_template("merchant.html", m=m, products=prods, agg=agg, recent=recent, dist=dist)


@app.route("/merchant/reply/<int:rid>", methods=["POST"])
@role_required("Merchant")
def merchant_reply(rid):
    m = current_merchant()
    ok = q("SELECT 1 FROM Review r JOIN Product p ON p.product_id=r.product_id WHERE r.review_id=? AND p.merchant_id=?",
           (rid, m["merchant_id"] if m else -1), one=True)
    if not ok:
        abort(403)
    text = request.form.get("response_text", "").strip()
    if len(text) < 5:
        flash("Reply must be at least 5 characters.", "error")
    else:
        db = get_db()
        db.execute("INSERT INTO Merchant_Response (review_id, merchant_id, response_text) VALUES (?,?,?) "
                   "ON CONFLICT(review_id) DO UPDATE SET response_text=excluded.response_text, "
                   "responded_at=CURRENT_TIMESTAMP", (rid, m["merchant_id"], text))
        db.commit()
        flash("Reply published.", "success")
    return redirect(url_for("merchant_dashboard"))


@app.route("/merchant/integration")
@role_required("Merchant")
def merchant_integration():
    m = current_merchant()
    if not m:
        abort(403)
    client = q("SELECT * FROM v_client_activity WHERE merchant_id=?", (m["merchant_id"],), one=True)
    secret = q("SELECT api_secret FROM Api_Client WHERE merchant_id=?", (m["merchant_id"],), one=True)
    logs = q("SELECT * FROM Api_Request_Log WHERE client_id=? ORDER BY log_id DESC LIMIT 40",
             (client["client_id"],)) if client else []
    sources = q("SELECT pv.source_name, COUNT(*) AS n FROM v_review_provenance pv JOIN Product p ON p.product_id=pv.product_id "
                "WHERE p.merchant_id=? GROUP BY pv.source_name ORDER BY n DESC", (m["merchant_id"],))
    return render_template("merchant_integration.html", m=m, client=client, logs=logs, sources=sources,
                           secret_hint=(secret[0][:12] + "••••" + secret[0][-4:]) if secret else None)


@app.route("/merchant/integration/toggle", methods=["POST"])
@role_required("Merchant")
def merchant_toggle_key():
    m = current_merchant()
    db = get_db()
    cur = db.execute("UPDATE Api_Client SET is_active = 1 - is_active WHERE merchant_id=?", (m["merchant_id"] if m else -1,))
    db.commit()
    if not cur.rowcount:
        abort(404)
    active = q("SELECT is_active FROM Api_Client WHERE merchant_id=?", (m["merchant_id"],), one=True)[0]
    flash("API key resumed." if active else "API key paused. Signed calls will now be refused.", "success")
    return redirect(url_for("merchant_integration"))


# ------------------------------------------------------------------ admin

@app.route("/admin")
@role_required("Admin")
def admin_overview():
    stats = {
        "Users": q("SELECT COUNT(*) FROM Users", one=True)[0],
        "Products": q("SELECT COUNT(*) FROM Product", one=True)[0],
        "Reviews": q("SELECT COUNT(*) FROM Review", one=True)[0],
        "Avg rating": q("SELECT COALESCE(ROUND(AVG(rating),2),0) FROM Review WHERE status='Approved'", one=True)[0],
        "Pending reports": q("SELECT COUNT(*) FROM Review_Report WHERE report_status='Pending'", one=True)[0],
    }
    days = 21
    start = (datetime.now() - timedelta(days=days - 1)).date()
    per_day = {r[0]: r[1] for r in q("SELECT DATE(created_at), COUNT(*) FROM Review WHERE DATE(created_at)>=? "
                                     "GROUP BY DATE(created_at)", (start.isoformat(),))}
    series = [per_day.get((start + timedelta(days=i)).isoformat(), 0) for i in range(days)]
    mx = max(series) or 1
    w, h = 600, 120
    points = " ".join(f"{i * w / (days - 1):.1f},{h - 8 - v / mx * (h - 16):.1f}" for i, v in enumerate(series))
    latest = q("SELECT r.review_id, r.title, r.rating, r.status, r.created_at, u.full_name, p.product_name "
               "FROM Review r JOIN Users u ON u.user_id=r.user_id JOIN Product p ON p.product_id=r.product_id "
               "ORDER BY r.created_at DESC LIMIT 6")
    return render_template("admin/overview.html", stats=stats, series=series, points=points, spark_w=w, spark_h=h,
                           start=start, dist=distribution(), latest=latest)


@app.route("/admin/moderation")
@role_required("Admin")
def admin_moderation():
    return render_template("admin/moderation.html",
                           queue=q("SELECT * FROM v_moderation_queue ORDER BY pending_reports DESC, reported_at"))


@app.route("/admin/reports/<int:report_id>/<action>", methods=["POST"])
@role_required("Admin")
def admin_report_action(report_id, action):
    if action not in ("approve", "reject"):
        abort(404)
    db = get_db()
    cur = db.execute("UPDATE Review_Report SET report_status=? WHERE report_id=? AND report_status='Pending'",
                     ("Approved" if action == "approve" else "Rejected", report_id))
    db.commit()
    if cur.rowcount:
        flash("Report approved, review removed." if action == "approve" else "Report rejected, review kept.", "success")
    else:
        flash("That report was already resolved.", "error")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/reviews")
@role_required("Admin")
def admin_reviews():
    term, status = request.args.get("q", "").strip(), request.args.get("status", "")
    sql = ("SELECT r.*, u.full_name, p.product_name, h.score, pv.source_name, "
           "(SELECT COUNT(*) FROM Review_Report x WHERE x.review_id=r.review_id) AS reports "
           "FROM Review r JOIN Users u ON u.user_id=r.user_id JOIN Product p ON p.product_id=r.product_id "
           "JOIN v_helpfulness h ON h.review_id=r.review_id "
           "JOIN v_review_provenance pv ON pv.review_id=r.review_id WHERE 1=1")
    args = []
    if term:
        sql += " AND (r.title LIKE ? OR r.review_text LIKE ? OR p.product_name LIKE ? OR u.full_name LIKE ?)"
        args += [f"%{term}%"] * 4
    if status in ("Pending", "Approved", "Rejected"):
        sql += " AND r.status=?"
        args.append(status)
    return render_template("admin/reviews.html", reviews=q(sql + " ORDER BY r.created_at DESC", args),
                           term=term, status=status)


@app.route("/admin/reviews/<int:rid>/status", methods=["POST"])
@role_required("Admin")
def admin_review_status(rid):
    status = request.form.get("status")
    if status not in ("Pending", "Approved", "Rejected"):
        flash("Invalid status.", "error")
    else:
        db = get_db()
        db.execute("UPDATE Review SET status=? WHERE review_id=?", (status, rid))
        db.commit()
        flash(f"Review #{rid} set to {status}.", "success")
    return redirect(request.referrer or url_for("admin_reviews"))


@app.route("/admin/users")
@role_required("Admin")
def admin_users():
    users = q("SELECT u.*, m.company_name, (SELECT COUNT(*) FROM Review r WHERE r.user_id=u.user_id) AS reviews, "
              "(SELECT COUNT(*) FROM Transactions t WHERE t.user_id=u.user_id) AS orders "
              "FROM Users u LEFT JOIN Merchant m ON m.user_id=u.user_id ORDER BY u.role, u.full_name")
    return render_template("admin/users.html", users=users)


@app.route("/admin/catalog")
@role_required("Admin")
def admin_catalog():
    return render_template("admin/catalog.html",
                           categories=q("SELECT c.*, (SELECT COUNT(*) FROM Product p WHERE p.category_id=c.category_id) AS n "
                                        "FROM Category c ORDER BY category_name"),
                           products=q("SELECT * FROM v_product_stats ORDER BY category_name, product_name"),
                           merchants=q("SELECT * FROM Merchant ORDER BY company_name"))


@app.route("/admin/categories", methods=["POST"])
@role_required("Admin")
def admin_add_category():
    name, desc = request.form.get("category_name", "").strip(), request.form.get("description", "").strip()
    if len(name) < 2:
        flash("Category name must be at least 2 characters.", "error")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO Category (category_name, description) VALUES (?,?)", (name, desc or None))
            db.commit()
            flash(f"Category '{name}' added.", "success")
        except sqlite3.IntegrityError:
            flash("A category with that name already exists.", "error")
    return redirect(url_for("admin_catalog"))


@app.route("/admin/products", methods=["POST"])
@role_required("Admin")
def admin_add_product():
    f = request.form
    name, sku = f.get("product_name", "").strip(), f.get("sku", "").strip().upper()
    mid, cid = f.get("merchant_id", type=int), f.get("category_id", type=int)
    if len(name) < 2 or len(sku) < 3 or not mid or not cid:
        flash("Name, SKU (3+ chars), merchant and category are all required.", "error")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO Product (merchant_id, category_id, product_name, sku) VALUES (?,?,?,?)",
                       (mid, cid, name, sku))
            db.commit()
            flash(f"Product '{name}' added.", "success")
        except sqlite3.IntegrityError as e:
            flash("That SKU is already in use." if "sku" in str(e) else "Invalid merchant or category.", "error")
    return redirect(url_for("admin_catalog"))


@app.route("/admin/integrity", methods=["GET", "POST"])
@role_required("Admin")
def admin_integrity():
    result = None
    if request.method == "POST":
        ok, entries, bad, mismatched = verify_ledger(get_db())
        result = {"ok": ok, "entries": entries, "bad": bad, "mismatched": mismatched}
    return render_template("admin/integrity.html", result=result,
                           clients=q("SELECT * FROM v_client_activity ORDER BY company_name"),
                           logs=q("SELECT l.*, m.company_name FROM Api_Request_Log l "
                                  "LEFT JOIN Api_Client ac ON ac.client_id=l.client_id "
                                  "LEFT JOIN Merchant m ON m.merchant_id=ac.merchant_id ORDER BY l.log_id DESC LIMIT 60"),
                           ledger=q("SELECT * FROM Review_Ledger ORDER BY ledger_id DESC LIMIT 12"),
                           counts=q("SELECT event, COUNT(*) AS n FROM Review_Ledger GROUP BY event"))


@app.route("/admin/graph")
@role_required("Admin")
def admin_graph():
    return render_template("admin/graph.html")


@app.route("/admin/graph.json")
@role_required("Admin")
def admin_graph_json():
    nodes, links = [], []
    for c in q("SELECT c.*, (SELECT COUNT(*) FROM Product p WHERE p.category_id=c.category_id) AS n FROM Category c"):
        nodes.append({"id": f"c{c['category_id']}", "type": "category", "label": c["category_name"],
                      "stats": {"Products": c["n"], "Description": c["description"] or "-"}})
    for m in q("SELECT m.*, (SELECT COUNT(*) FROM Product p WHERE p.merchant_id=m.merchant_id) AS n, "
               "(SELECT ROUND(AVG(avg_rating),2) FROM Product p WHERE p.merchant_id=m.merchant_id AND total_reviews>0) AS avg "
               "FROM Merchant m"):
        nodes.append({"id": f"m{m['merchant_id']}", "type": "merchant", "label": m["company_name"],
                      "stats": {"Products": m["n"], "Avg product rating": m["avg"] or 0,
                                "Reg. no": m["business_registration_no"], "Contact": m["contact_email"]}})
    for p in q("SELECT * FROM v_product_stats"):
        nodes.append({"id": f"p{p['product_id']}", "type": "product", "label": p["product_name"],
                      "url": url_for("product_detail", pid=p["product_id"]),
                      "stats": {"SKU": p["sku"], "Avg rating": p["avg_rating"], "Reviews": p["total_reviews"],
                                "Verified": p["verified_count"]}})
        links.append({"source": f"c{p['category_id']}", "target": f"p{p['product_id']}", "kind": "category"})
        links.append({"source": f"m{p['merchant_id']}", "target": f"p{p['product_id']}", "kind": "merchant"})
    for u in q("SELECT u.user_id, u.full_name, u.email, COUNT(r.review_id) AS n, ROUND(AVG(r.rating),2) AS avg "
               "FROM Users u JOIN Review r ON r.user_id=u.user_id GROUP BY u.user_id"):
        nodes.append({"id": f"u{u['user_id']}", "type": "user", "label": u["full_name"],
                      "stats": {"Email": u["email"], "Reviews": u["n"], "Avg given": u["avg"]}})
    for r in q("SELECT user_id, product_id, rating, status FROM Review"):
        links.append({"source": f"u{r['user_id']}", "target": f"p{r['product_id']}", "kind": "review",
                      "rating": r["rating"], "status": r["status"]})
    degree = {}
    for l in links:
        for end in (l["source"], l["target"]):
            degree[end] = degree.get(end, 0) + 1
    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)
    return jsonify(nodes=nodes, links=links)


# ------------------------------------------------------------------ public

def review_trail(rid):
    """Ledger entries for one review, each re-checked against its stored predecessor."""
    db = get_db()
    entries = []
    for e in q("SELECT * FROM Review_Ledger WHERE review_id=? ORDER BY ledger_id", (rid,)):
        expect = sha256(f"{e['prev_hash']}|{e['content_hash']}|{e['event']}|{e['review_id']}")
        prev_ok = e["prev_hash"] == "GENESIS" or bool(db.execute(
            "SELECT 1 FROM Review_Ledger WHERE entry_hash=? AND ledger_id<?", (e["prev_hash"], e["ledger_id"])).fetchone())
        entries.append({**dict(e), "ok": expect == e["entry_hash"] and prev_ok})
    return entries


def review_page(r, template, **extra):
    prov = q("SELECT * FROM v_review_provenance WHERE review_id=?", (r["review_id"],), one=True)
    trail = review_trail(r["review_id"])
    current = sha256(content_string(r))
    matches = bool(trail) and trail[-1]["content_hash"] == current
    return render_template(template, r=r, prov=prov, trail=trail, current_hash=current,
                           intact=matches and all(e["ok"] for e in trail),
                           revisions=q("SELECT * FROM Review_Revision WHERE review_id=? ORDER BY revision_id DESC",
                                       (r["review_id"],)), **extra)


@app.route("/r/<token>", methods=["GET", "POST"])
def receipt(token):
    r = q("SELECT r.*, u.full_name, u.email FROM Review r JOIN Users u ON u.user_id=r.user_id WHERE receipt_token=?",
          (token,), one=True)
    if not r:
        abort(404)
    if request.method == "POST":
        f = request.form
        rating, title, text = f.get("rating", type=int), f.get("title", "").strip(), f.get("review_text", "").strip()
        problems = validate_review(rating, title, text, {})
        if problems:
            flash("Please " + ", ".join(problems) + ".", "error")
        elif (rating, title or None, text) == (r["rating"], r["title"], r["review_text"]):
            flash("Nothing changed.", "error")
        else:
            db = get_db()
            db.execute("UPDATE Review SET rating=?, title=?, review_text=?, sentiment_score=? WHERE review_id=?",
                       (rating, title or None, text, sentiment(f"{title} {text}"), r["review_id"]))
            db.commit()
            flash("Review updated. The change is recorded in the public ledger.", "success")
        return redirect(url_for("receipt", token=token))
    return review_page(r, "receipt.html")


@app.route("/verify/<int:rid>")
def verify_review(rid):
    r = q("SELECT r.*, u.full_name FROM Review r JOIN Users u ON u.user_id=r.user_id WHERE r.review_id=?", (rid,), one=True)
    if not r:
        abort(404)
    return review_page(r, "verify.html")


@app.route("/companies")
def companies():
    term = request.args.get("q", "").strip()
    like = f"%{term}%"
    rows = q("""
        SELECT m.*, ac.public_key IS NOT NULL AS integrated, ac.site_url,
               (SELECT COUNT(*) FROM Product p WHERE p.merchant_id=m.merchant_id) AS products,
               (SELECT COUNT(*) FROM Review r JOIN Product p ON p.product_id=r.product_id
                 WHERE p.merchant_id=m.merchant_id AND r.status='Approved') AS reviews,
               (SELECT ROUND(AVG(r.rating),2) FROM Review r JOIN Product p ON p.product_id=r.product_id
                 WHERE p.merchant_id=m.merchant_id AND r.status='Approved') AS avg
          FROM Merchant m LEFT JOIN Api_Client ac ON ac.merchant_id=m.merchant_id
         WHERE ?='' OR m.company_name LIKE ?
            OR EXISTS (SELECT 1 FROM Product p WHERE p.merchant_id=m.merchant_id AND p.product_name LIKE ?)
         ORDER BY integrated DESC, m.company_name""", (term, like, like))
    matches = q("SELECT * FROM v_product_stats WHERE product_name LIKE ? OR sku LIKE ? ORDER BY total_reviews DESC LIMIT 8",
                (like, like)) if term else []
    totals = q("SELECT (SELECT COUNT(*) FROM Review WHERE status='Approved') AS reviews, "
               "(SELECT COUNT(*) FROM Review WHERE status='Approved' AND is_verified_purchase=1) AS verified, "
               "(SELECT COUNT(*) FROM Merchant) AS companies", one=True)
    return render_template("companies.html", companies=rows, matches=matches, term=term, totals=totals)


@app.route("/companies/<int:mid>")
def company(mid):
    m = q("SELECT m.*, ac.site_url, ac.public_key FROM Merchant m LEFT JOIN Api_Client ac ON ac.merchant_id=m.merchant_id "
          "WHERE m.merchant_id=?", (mid,), one=True)
    if not m:
        abort(404)
    scope = "WHERE product_id IN (SELECT product_id FROM Product WHERE merchant_id=?)"
    agg = q("SELECT COUNT(*) AS n, ROUND(AVG(r.rating),2) AS avg, SUM(r.is_verified_purchase) AS verified, "
            "(SELECT COUNT(*) FROM Merchant_Response mr WHERE mr.merchant_id=?) AS replies "
            "FROM Review r JOIN Product p ON p.product_id=r.product_id WHERE p.merchant_id=? AND r.status='Approved'",
            (mid, mid), one=True)
    recent = q("""
        SELECT r.*, u.full_name, p.product_name, p.product_id, pv.source_name
          FROM Review r JOIN Product p ON p.product_id=r.product_id JOIN Users u ON u.user_id=r.user_id
          JOIN v_review_provenance pv ON pv.review_id=r.review_id
         WHERE p.merchant_id=? AND r.status='Approved' ORDER BY r.created_at DESC LIMIT 8""", (mid,))
    return render_template("company.html", m=m, agg=agg, recent=recent, dist=distribution(scope, (mid,)),
                           products=q("SELECT * FROM v_product_stats WHERE merchant_id=? ORDER BY avg_rating DESC", (mid,)))


@app.route("/schema")
def schema():
    objs = q("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL "
             "AND name NOT LIKE 'sqlite_%' AND type IN ('table','trigger','view') ORDER BY type, rowid")
    grouped = {t: [o for o in objs if o["type"] == t] for t in ("table", "trigger", "view")}
    return render_template("schema.html", grouped=grouped)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
