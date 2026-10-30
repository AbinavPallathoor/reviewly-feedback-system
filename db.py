"""Database helpers: connection, schema init and demo seed data."""
import hashlib
import os
import random
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviewly.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
CRITERIA = ("Quality", "Delivery", "Value")

# Fixed dev credentials so the example shops (shops/*) can talk to a freshly seeded Reviewly.
API_CLIENTS = [
    # (contact name, login email, company, reg no, public key, secret, site)
    ("Kai Mercer", "kickvault@reviewly.test", "KICKVAULT", "BRN-310552",
     "pk_test_kickvault", "sk_test_kickvault_7f3a9c2e51d84b60", "http://127.0.0.1:5001"),
    ("Flora Quince", "fernfig@reviewly.test", "Fern & Fig", "BRN-420913",
     "pk_test_fernfig", "sk_test_fernfig_b18e4d07a29c6f35", "http://127.0.0.1:5002"),
]

POSITIVE = set("""good great excellent amazing love loved perfect fast happy recommend solid sturdy
comfortable beautiful best nice reliable worth easy brilliant fantastic quality smooth quick helpful
crisp bright superb impressive pleased awesome durable""".split())
NEGATIVE = set("""bad poor terrible awful broke broken slow disappointed disappointing waste cheap flimsy
worst hate late refund defective noisy useless faulty overpriced damaged missing problem issue
returned annoying weak scratched""".split())


def sentiment(text):
    """Tiny lexicon score in [-1, 1]: (pos - neg) / (pos + neg)."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    return 0.0 if pos + neg == 0 else round((pos - neg) / (pos + neg), 2)


def sha256(text):
    return hashlib.sha256(str(text).encode()).hexdigest()


def content_string(r):
    """Must match the string the ledger triggers in schema.sql hash."""
    return f"{r['review_id']}|{r['user_id']}|{r['product_id']}|{r['rating']}|{r['title'] or ''}|{r['review_text']}|{r['status']}"


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("sha256", 1, sha256, deterministic=True)
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    return cur.fetchone() if one else cur.fetchall()


def new_token():
    return secrets.token_urlsafe(18)


def validate_review(rating, title, text, scores):
    """Shared rules for reviews written on Reviewly and reviews sent through the API."""
    problems = []
    if rating not in range(1, 6):
        problems.append("choose an overall rating")
    if any(s not in range(1, 6) for s in scores.values()):
        problems.append("rate Quality, Delivery and Value")
    if len(text) < 10:
        problems.append("write at least 10 characters")
    if len(title) > 150:
        problems.append("keep the title under 150 characters")
    return problems


def verify_ledger(conn):
    """Recompute the whole hash chain. Returns (ok, entries, first_bad_entry, reviews_not_matching)."""
    prev, bad, latest = "GENESIS", None, {}
    rows = conn.execute("SELECT * FROM Review_Ledger ORDER BY ledger_id").fetchall()
    for e in rows:
        expect = sha256(f"{prev}|{e['content_hash']}|{e['event']}|{e['review_id']}")
        if bad is None and (e["prev_hash"] != prev or e["entry_hash"] != expect):
            bad = e["ledger_id"]
        prev = e["entry_hash"]
        latest[e["review_id"]] = e["content_hash"]
    mismatched = [r["review_id"] for r in conn.execute("SELECT * FROM Review").fetchall()
                  if latest.get(r["review_id"]) != sha256(content_string(r))]
    return bad is None and not mismatched, len(rows), bad, mismatched


def init_db(path=None):
    path = path or DB_PATH
    if os.path.exists(path):
        return False
    conn = connect(path)
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        seed(conn)
        conn.commit()
    except Exception:
        conn.close()
        os.remove(path)
        raise
    conn.close()
    return True


def seed(conn):
    rng = random.Random(42)
    pw = generate_password_hash("password")
    now = datetime.now()
    ts = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")

    def add_user(name, email, role, days_ago):
        cur = conn.execute(
            "INSERT INTO Users (full_name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (name, email, pw, role, ts(now - timedelta(days=days_ago))))
        return cur.lastrowid

    add_user("Ada Admin", "admin@reviewly.test", "Admin", 60)

    merchants = []
    for name, email, company, reg in [
        ("Nora Vance", "northwind@reviewly.test", "Northwind Electronics", "BRN-100245"),
        ("Hugo Page", "hearth@reviewly.test", "Hearth & Page Co.", "BRN-200871"),
    ]:
        uid = add_user(name, email, "Merchant", 55)
        cur = conn.execute(
            "INSERT INTO Merchant (user_id, company_name, business_registration_no, contact_email) VALUES (?,?,?,?)",
            (uid, company, reg, email))
        merchants.append(cur.lastrowid)

    for name, email, company, reg, pk, sk, site in API_CLIENTS:
        uid = add_user(name, email, "Merchant", 30)
        mid = conn.execute(
            "INSERT INTO Merchant (user_id, company_name, business_registration_no, contact_email) VALUES (?,?,?,?)",
            (uid, company, reg, email)).lastrowid
        conn.execute("INSERT INTO Api_Client (merchant_id, public_key, api_secret, site_url) VALUES (?,?,?,?)",
                     (mid, pk, sk, site))

    customers = [add_user(n, e, "Customer", rng.randint(20, 50)) for n, e in [
        ("Priya Nair", "priya@reviewly.test"), ("Liam Chen", "liam@reviewly.test"),
        ("Sofia Rossi", "sofia@reviewly.test"), ("Omar Haddad", "omar@reviewly.test"),
        ("Mei Tanaka", "mei@reviewly.test"), ("Jonas Berg", "jonas@reviewly.test"),
        ("Aisha Bello", "aisha@reviewly.test"), ("Diego Alvarez", "diego@reviewly.test"),
    ]]

    cats = {}
    for name, desc in [("Electronics", "Gadgets, audio and accessories"),
                       ("Home & Kitchen", "Cookware, appliances and decor"),
                       ("Books", "Print books across genres")]:
        cats[name] = conn.execute("INSERT INTO Category (category_name, description) VALUES (?,?)",
                                  (name, desc)).lastrowid

    product_rows = [
        (0, "Electronics", "Aurora Wireless Earbuds", "NW-EB-001"),
        (0, "Electronics", "Pulse 65W USB-C Charger", "NW-CH-002"),
        (0, "Electronics", "Vista 27\" Monitor", "NW-MN-003"),
        (0, "Electronics", "Echo Mechanical Keyboard", "NW-KB-004"),
        (0, "Home & Kitchen", "SmartBrew Coffee Maker", "NW-CM-005"),
        (0, "Electronics", "Drift Bluetooth Speaker", "NW-SP-006"),
        (1, "Home & Kitchen", "Cast Iron Skillet 10\"", "HP-SK-101"),
        (1, "Home & Kitchen", "Linen Throw Blanket", "HP-BL-102"),
        (1, "Home & Kitchen", "Ceramic Pour-Over Set", "HP-PO-103"),
        (1, "Books", "The Quiet Database", "HP-BK-104"),
        (1, "Books", "Patterns of Normal Forms", "HP-BK-105"),
        (1, "Books", "Midnight in the Index", "HP-BK-106"),
    ]
    products = []
    for m, c, name, sku in product_rows:
        products.append(conn.execute(
            "INSERT INTO Product (merchant_id, category_id, product_name, sku) VALUES (?,?,?,?)",
            (merchants[m], cats[c], name, sku)).lastrowid)

    texts = {
        5: [("Absolutely love it", "Excellent quality and fast delivery. Would recommend to anyone."),
            ("Best purchase this year", "Great build, works perfect, worth every penny."),
            ("Superb", "Impressive and reliable. Packaging was nice and it arrived quick.")],
        4: [("Really good", "Solid product, good value. Delivery was a bit slow but happy overall."),
            ("Nice and sturdy", "Comfortable to use and looks beautiful. Minor issue with the box."),
            ("Pleased", "Easy to set up and reliable so far.")],
        3: [("It's okay", "Does the job. Nothing amazing, nothing terrible."),
            ("Average", "Decent but a little overpriced for what you get."),
            ("Mixed feelings", "Good design, but the finish feels cheap in places.")],
        2: [("Disappointed", "Arrived late and the quality is poor for the price."),
            ("Not great", "Flimsy build, noisy, and support was slow to reply.")],
        1: [("Broke in a week", "Terrible. It broke after a week and the refund was a problem."),
            ("Waste of money", "Defective unit, useless, and returned it immediately.")],
    }

    # Transactions first so the BEFORE INSERT trigger can verify purchases.
    pairs = [(u, p) for u in customers for p in products]
    rng.shuffle(pairs)
    review_pairs = pairs[:40]
    for i, (u, p) in enumerate(review_pairs[:26] + pairs[40:46]):
        conn.execute(
            "INSERT INTO Transactions (user_id, product_id, purchase_date, order_reference) VALUES (?,?,?,?)",
            (u, p, ts(now - timedelta(days=rng.randint(25, 45))), f"ORD-{10000 + i}"))

    weights = [1, 1, 2, 4, 5]  # skew towards positive ratings
    review_ids = []
    for u, p in review_pairs:
        rating = rng.choices([1, 2, 3, 4, 5], weights)[0]
        title, body = rng.choice(texts[rating])
        created = now - timedelta(days=rng.randint(0, 20), hours=rng.randint(0, 23))
        conn.execute(
            "INSERT INTO Review (user_id, product_id, rating, title, review_text, sentiment_score, created_at, receipt_token) "
            "VALUES (?,?,?,?,?,?,?,?)", (u, p, rating, title, body, sentiment(title + " " + body), ts(created), new_token()))
        rid = conn.execute("SELECT review_id FROM Review WHERE user_id=? AND product_id=?", (u, p)).fetchone()[0]
        review_ids.append(rid)
        for crit in CRITERIA:
            score = max(1, min(5, rating + rng.choice([-1, 0, 0, 1])))
            conn.execute("INSERT INTO Review_Criteria_Rating (review_id, criteria_name, score) VALUES (?,?,?)",
                         (rid, crit, score))

    conn.execute("INSERT INTO Review_Media (review_id, media_url, media_type) VALUES (?,?,?)",
                 (review_ids[0], "https://picsum.photos/seed/reviewly1/480/320", "image"))
    conn.execute("INSERT INTO Review_Media (review_id, media_url, media_type) VALUES (?,?,?)",
                 (review_ids[3], "https://picsum.photos/seed/reviewly2/480/320", "image"))

    for rid in review_ids:
        author = conn.execute("SELECT user_id FROM Review WHERE review_id=?", (rid,)).fetchone()[0]
        for voter in rng.sample([c for c in customers if c != author], rng.randint(0, 4)):
            conn.execute("INSERT INTO Review_Vote (review_id, user_id, vote_type) VALUES (?,?,?)",
                         (rid, voter, rng.choices(["Up", "Down"], [4, 1])[0]))

    # 3 pending reports on low-rated reviews (one each, so nothing auto-hides).
    low = conn.execute("SELECT review_id, user_id FROM Review ORDER BY rating, review_id LIMIT 3").fetchall()
    reasons = ["Spam or advertising", "Offensive language", "Not about this product"]
    for (rid, author), reason in zip(low, reasons):
        reporter = next(c for c in customers if c != author)
        conn.execute("INSERT INTO Review_Report (review_id, reporter_user_id, report_reason) VALUES (?,?,?)",
                     (rid, reporter, reason))

    # 2 merchant responses (one per merchant).
    for mid, text in [(merchants[0], "Thanks for the detailed feedback! We've passed this on to our QA team."),
                      (merchants[1], "Sorry to hear that. Please contact us at support and we'll make it right.")]:
        rid = conn.execute(
            "SELECT r.review_id FROM Review r JOIN Product p ON p.product_id=r.product_id "
            "WHERE p.merchant_id=? ORDER BY r.rating, r.review_id LIMIT 1", (mid,)).fetchone()[0]
        conn.execute("INSERT INTO Merchant_Response (review_id, merchant_id, response_text) VALUES (?,?,?)",
                     (rid, mid, text))
