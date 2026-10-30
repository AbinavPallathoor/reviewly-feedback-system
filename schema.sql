-- Reviewly: Customer Feedback and Review Management System
-- Standard SQL (SQLite dialect). Run with PRAGMA foreign_keys = ON.

-- ============================== TABLES ==============================

CREATE TABLE Users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name      VARCHAR(100) NOT NULL,
    email          VARCHAR(150) NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL,
    role           VARCHAR(20)  NOT NULL DEFAULT 'Customer'
                   CHECK (role IN ('Customer', 'Merchant', 'Admin')),
    created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Merchant (
    merchant_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                   INTEGER NOT NULL UNIQUE REFERENCES Users(user_id) ON DELETE CASCADE,
    company_name              VARCHAR(150) NOT NULL,
    business_registration_no  VARCHAR(50)  NOT NULL UNIQUE,
    contact_email             VARCHAR(150) NOT NULL
);

-- A company that sends reviews to Reviewly through the signed API.
-- (In production api_secret would be stored encrypted, never in plain text.)
CREATE TABLE Api_Client (
    client_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id   INTEGER NOT NULL UNIQUE REFERENCES Merchant(merchant_id) ON DELETE CASCADE,
    public_key    VARCHAR(64)  NOT NULL UNIQUE,
    api_secret    VARCHAR(128) NOT NULL,
    site_url      VARCHAR(200) NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  TIMESTAMP
);

-- Replay protection: a signed request's nonce can be used once per client.
CREATE TABLE Api_Nonce (
    client_id  INTEGER NOT NULL REFERENCES Api_Client(client_id) ON DELETE CASCADE,
    nonce      VARCHAR(64) NOT NULL,
    used_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (client_id, nonce)
);

-- Every API call, accepted or not. client_id is NULL when the key was unknown.
CREATE TABLE Api_Request_Log (
    log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER REFERENCES Api_Client(client_id) ON DELETE SET NULL,
    method       VARCHAR(8)   NOT NULL,
    endpoint     VARCHAR(200) NOT NULL,
    status_code  INTEGER      NOT NULL,
    outcome      VARCHAR(10)  NOT NULL CHECK (outcome IN ('Accepted', 'Rejected', 'Flagged')),
    reason       TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Category (
    category_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name  VARCHAR(80) NOT NULL UNIQUE,
    description    TEXT
);

CREATE TABLE Product (
    product_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id    INTEGER NOT NULL REFERENCES Merchant(merchant_id) ON DELETE CASCADE,
    category_id    INTEGER NOT NULL REFERENCES Category(category_id) ON DELETE RESTRICT,
    product_name   VARCHAR(150) NOT NULL,
    sku            VARCHAR(50)  NOT NULL UNIQUE,
    image_url      VARCHAR(500),
    avg_rating     DECIMAL(3,2) NOT NULL DEFAULT 0,
    total_reviews  INTEGER      NOT NULL DEFAULT 0 CHECK (total_reviews >= 0)
);

CREATE TABLE Transactions (
    transaction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL REFERENCES Product(product_id) ON DELETE CASCADE,
    purchase_date    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    order_reference  VARCHAR(40) NOT NULL UNIQUE,
    source_client_id INTEGER REFERENCES Api_Client(client_id) ON DELETE SET NULL
);

CREATE TABLE Review (
    review_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    product_id            INTEGER NOT NULL REFERENCES Product(product_id) ON DELETE CASCADE,
    transaction_id        INTEGER UNIQUE REFERENCES Transactions(transaction_id) ON DELETE SET NULL,
    rating                INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title                 VARCHAR(150),
    review_text           TEXT NOT NULL,
    is_verified_purchase  INTEGER NOT NULL DEFAULT 0 CHECK (is_verified_purchase IN (0, 1)),
    sentiment_score       REAL,
    status                VARCHAR(20) NOT NULL DEFAULT 'Approved'
                          CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_client_id      INTEGER REFERENCES Api_Client(client_id) ON DELETE SET NULL,
    receipt_token         VARCHAR(64) UNIQUE,
    flag_reason           TEXT,
    UNIQUE (user_id, product_id)
);

CREATE TABLE Review_Media (
    media_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id    INTEGER NOT NULL REFERENCES Review(review_id) ON DELETE CASCADE,
    media_url    VARCHAR(500) NOT NULL UNIQUE,
    media_type   VARCHAR(20) NOT NULL DEFAULT 'image' CHECK (media_type IN ('image', 'video', 'link')),
    uploaded_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Review_Criteria_Rating (
    criteria_rating_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id           INTEGER NOT NULL REFERENCES Review(review_id) ON DELETE CASCADE,
    criteria_name       VARCHAR(40) NOT NULL,
    score               INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    UNIQUE (review_id, criteria_name)
);

CREATE TABLE Merchant_Response (
    response_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL UNIQUE REFERENCES Review(review_id) ON DELETE CASCADE,
    merchant_id    INTEGER NOT NULL REFERENCES Merchant(merchant_id) ON DELETE CASCADE,
    response_text  TEXT NOT NULL,
    responded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Review_Vote (
    vote_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id  INTEGER NOT NULL REFERENCES Review(review_id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    vote_type  VARCHAR(4) NOT NULL CHECK (vote_type IN ('Up', 'Down')),
    voted_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (review_id, user_id)
);

CREATE TABLE Review_Report (
    report_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id         INTEGER NOT NULL REFERENCES Review(review_id) ON DELETE CASCADE,
    reporter_user_id  INTEGER NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    report_reason     TEXT NOT NULL,
    report_status     VARCHAR(20) NOT NULL DEFAULT 'Pending'
                      CHECK (report_status IN ('Pending', 'Approved', 'Rejected')),
    reported_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (review_id, reporter_user_id)
);

-- Append-only hash chain over every review event. review_id is deliberately not a
-- foreign key: the ledger must outlive the row it describes.
CREATE TABLE Review_Ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id     INTEGER NOT NULL,
    event         VARCHAR(10) NOT NULL CHECK (event IN ('created', 'edited', 'status')),
    content_hash  CHAR(64) NOT NULL,
    prev_hash     VARCHAR(64) NOT NULL,
    entry_hash    CHAR(64) NOT NULL UNIQUE,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Edit history: the previous version of a review, written by a trigger.
CREATE TABLE Review_Revision (
    revision_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id    INTEGER NOT NULL REFERENCES Review(review_id) ON DELETE CASCADE,
    rating       INTEGER NOT NULL,
    title        VARCHAR(150),
    review_text  TEXT NOT NULL,
    revised_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_review_product ON Review(product_id, status);
CREATE INDEX idx_txn_user_product ON Transactions(user_id, product_id);
CREATE INDEX idx_report_review ON Review_Report(review_id, report_status);
CREATE INDEX idx_ledger_review ON Review_Ledger(review_id);
CREATE INDEX idx_log_client ON Api_Request_Log(client_id, created_at);

-- ============================== TRIGGERS ==============================

-- Trigger 2: BEFORE INSERT on Review -> verified-purchase detection.
-- SQLite cannot assign to NEW, so when a matching transaction exists the trigger
-- re-issues the INSERT with transaction_id + is_verified_purchase = 1 filled in,
-- then RAISE(IGNORE) drops the original row (no rollback of the re-insert).
CREATE TRIGGER trg_review_verify_purchase
BEFORE INSERT ON Review
WHEN NEW.transaction_id IS NULL
 AND EXISTS (SELECT 1 FROM Transactions t
             WHERE t.user_id = NEW.user_id AND t.product_id = NEW.product_id)
BEGIN
    INSERT INTO Review (user_id, product_id, transaction_id, rating, title, review_text,
                        is_verified_purchase, sentiment_score, status, created_at,
                        source_client_id, receipt_token, flag_reason)
    VALUES (NEW.user_id, NEW.product_id,
            (SELECT t.transaction_id FROM Transactions t
              WHERE t.user_id = NEW.user_id AND t.product_id = NEW.product_id
              ORDER BY t.purchase_date LIMIT 1),
            NEW.rating, NEW.title, NEW.review_text, 1, NEW.sentiment_score,
            COALESCE(NEW.status, 'Approved'), COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
            NEW.source_client_id, NEW.receipt_token, NEW.flag_reason);
    SELECT RAISE(IGNORE);
END;

-- Trigger 1: keep Product.avg_rating / total_reviews in sync (Approved reviews only).
CREATE TRIGGER trg_review_stats_insert
AFTER INSERT ON Review
BEGIN
    UPDATE Product
       SET avg_rating    = COALESCE((SELECT ROUND(AVG(rating), 2) FROM Review
                                      WHERE product_id = NEW.product_id AND status = 'Approved'), 0),
           total_reviews = (SELECT COUNT(*) FROM Review
                             WHERE product_id = NEW.product_id AND status = 'Approved')
     WHERE product_id = NEW.product_id;
END;

CREATE TRIGGER trg_review_stats_update
AFTER UPDATE OF rating, status, product_id ON Review
BEGIN
    UPDATE Product
       SET avg_rating    = COALESCE((SELECT ROUND(AVG(r.rating), 2) FROM Review r
                                      WHERE r.product_id = Product.product_id AND r.status = 'Approved'), 0),
           total_reviews = (SELECT COUNT(*) FROM Review r
                             WHERE r.product_id = Product.product_id AND r.status = 'Approved')
     WHERE product_id IN (OLD.product_id, NEW.product_id);
END;

CREATE TRIGGER trg_review_stats_delete
AFTER DELETE ON Review
BEGIN
    UPDATE Product
       SET avg_rating    = COALESCE((SELECT ROUND(AVG(rating), 2) FROM Review
                                      WHERE product_id = OLD.product_id AND status = 'Approved'), 0),
           total_reviews = (SELECT COUNT(*) FROM Review
                             WHERE product_id = OLD.product_id AND status = 'Approved')
     WHERE product_id = OLD.product_id;
END;

-- Trigger 3: three pending reports auto-hide a review for moderation.
CREATE TRIGGER trg_report_autohide
AFTER INSERT ON Review_Report
WHEN (SELECT COUNT(*) FROM Review_Report
       WHERE review_id = NEW.review_id AND report_status = 'Pending') >= 3
BEGIN
    UPDATE Review SET status = 'Pending'
     WHERE review_id = NEW.review_id AND status = 'Approved';
END;

-- Moderation outcome: an approved report removes the review and closes sibling reports;
-- a rejected report leaves the review alone, and un-hides it once no pending reports remain.
CREATE TRIGGER trg_report_resolved
AFTER UPDATE OF report_status ON Review_Report
WHEN OLD.report_status = 'Pending' AND NEW.report_status <> 'Pending'
BEGIN
    UPDATE Review SET status = 'Rejected'
     WHERE review_id = NEW.review_id AND NEW.report_status = 'Approved';
    UPDATE Review_Report SET report_status = 'Approved'
     WHERE review_id = NEW.review_id AND report_status = 'Pending' AND NEW.report_status = 'Approved';
    UPDATE Review SET status = 'Approved'
     WHERE review_id = NEW.review_id AND status = 'Pending' AND NEW.report_status = 'Rejected'
       AND NOT EXISTS (SELECT 1 FROM Review_Report
                        WHERE review_id = NEW.review_id AND report_status = 'Pending');
END;

-- Tamper evidence. sha256() is registered on every connection by db.connect(), so a
-- plain sqlite3 shell cannot change a review without the trigger failing.
-- content = review_id|user_id|product_id|rating|title|text|status
-- entry   = sha256(prev_hash|content_hash|event|review_id)
CREATE TRIGGER trg_ledger_review_insert
AFTER INSERT ON Review
BEGIN
    INSERT INTO Review_Ledger (review_id, event, content_hash, prev_hash, entry_hash)
    SELECT NEW.review_id, 'created', c, p, sha256(p || '|' || c || '|created|' || NEW.review_id)
      FROM (SELECT sha256(NEW.review_id || '|' || NEW.user_id || '|' || NEW.product_id || '|' || NEW.rating || '|' ||
                          COALESCE(NEW.title, '') || '|' || NEW.review_text || '|' || NEW.status) AS c,
                   COALESCE((SELECT entry_hash FROM Review_Ledger ORDER BY ledger_id DESC LIMIT 1), 'GENESIS') AS p);
END;

CREATE TRIGGER trg_ledger_review_update
AFTER UPDATE OF rating, title, review_text, status ON Review
WHEN OLD.rating IS NOT NEW.rating OR OLD.title IS NOT NEW.title
  OR OLD.review_text IS NOT NEW.review_text OR OLD.status IS NOT NEW.status
BEGIN
    INSERT INTO Review_Ledger (review_id, event, content_hash, prev_hash, entry_hash)
    SELECT NEW.review_id, e, c, p, sha256(p || '|' || c || '|' || e || '|' || NEW.review_id)
      FROM (SELECT sha256(NEW.review_id || '|' || NEW.user_id || '|' || NEW.product_id || '|' || NEW.rating || '|' ||
                          COALESCE(NEW.title, '') || '|' || NEW.review_text || '|' || NEW.status) AS c,
                   COALESCE((SELECT entry_hash FROM Review_Ledger ORDER BY ledger_id DESC LIMIT 1), 'GENESIS') AS p,
                   CASE WHEN OLD.rating IS NEW.rating AND OLD.title IS NEW.title
                         AND OLD.review_text IS NEW.review_text THEN 'status' ELSE 'edited' END AS e);
END;

CREATE TRIGGER trg_ledger_no_update
BEFORE UPDATE ON Review_Ledger
BEGIN
    SELECT RAISE(ABORT, 'Review_Ledger is append-only');
END;

CREATE TRIGGER trg_ledger_no_delete
BEFORE DELETE ON Review_Ledger
BEGIN
    SELECT RAISE(ABORT, 'Review_Ledger is append-only');
END;

-- Keep the previous version of a review whenever its content changes.
CREATE TRIGGER trg_review_revision
BEFORE UPDATE OF rating, title, review_text ON Review
WHEN OLD.rating IS NOT NEW.rating OR OLD.title IS NOT NEW.title OR OLD.review_text IS NOT NEW.review_text
BEGIN
    INSERT INTO Review_Revision (review_id, rating, title, review_text)
    VALUES (OLD.review_id, OLD.rating, OLD.title, OLD.review_text);
END;

-- ============================== VIEWS ==============================

CREATE VIEW v_product_stats AS
SELECT p.product_id, p.product_name, p.sku, p.image_url,
       m.merchant_id, m.company_name,
       c.category_id, c.category_name,
       p.avg_rating, p.total_reviews,
       (SELECT COUNT(*) FROM Review r
         WHERE r.product_id = p.product_id AND r.status = 'Approved'
           AND r.is_verified_purchase = 1) AS verified_count
  FROM Product p
  JOIN Merchant m ON m.merchant_id = p.merchant_id
  JOIN Category c ON c.category_id = p.category_id;

CREATE VIEW v_rating_distribution AS
SELECT product_id, rating, COUNT(*) AS count
  FROM Review
 WHERE status = 'Approved'
 GROUP BY product_id, rating;

CREATE VIEW v_helpfulness AS
SELECT r.review_id,
       COALESCE(SUM(CASE WHEN v.vote_type = 'Up'   THEN 1 ELSE 0 END), 0) AS up,
       COALESCE(SUM(CASE WHEN v.vote_type = 'Down' THEN 1 ELSE 0 END), 0) AS down,
       COALESCE(SUM(CASE WHEN v.vote_type = 'Up' THEN 1 WHEN v.vote_type = 'Down' THEN -1 ELSE 0 END), 0) AS score
  FROM Review r
  LEFT JOIN Review_Vote v ON v.review_id = r.review_id
 GROUP BY r.review_id;

CREATE VIEW v_moderation_queue AS
SELECT rp.report_id, rp.report_reason, rp.reported_at,
       r.review_id, r.title, r.review_text, r.rating, r.status AS review_status,
       p.product_id, p.product_name,
       au.full_name AS author_name,
       ru.user_id AS reporter_id, ru.full_name AS reporter_name,
       (SELECT COUNT(*) FROM Review_Report x
         WHERE x.review_id = r.review_id AND x.report_status = 'Pending') AS pending_reports
  FROM Review_Report rp
  JOIN Review  r  ON r.review_id = rp.review_id
  JOIN Product p  ON p.product_id = r.product_id
  JOIN Users   au ON au.user_id = r.user_id
  JOIN Users   ru ON ru.user_id = rp.reporter_user_id
 WHERE rp.report_status = 'Pending';

CREATE VIEW v_client_activity AS
SELECT ac.client_id, ac.public_key, ac.site_url, ac.is_active, ac.last_used_at,
       m.merchant_id, m.company_name,
       (SELECT COUNT(*) FROM Api_Request_Log l WHERE l.client_id = ac.client_id AND l.outcome = 'Accepted') AS accepted,
       (SELECT COUNT(*) FROM Api_Request_Log l WHERE l.client_id = ac.client_id AND l.outcome = 'Rejected') AS rejected,
       (SELECT COUNT(*) FROM Api_Request_Log l WHERE l.client_id = ac.client_id AND l.outcome = 'Flagged')  AS flagged,
       (SELECT COUNT(*) FROM Review r WHERE r.source_client_id = ac.client_id) AS reviews,
       (SELECT ROUND(AVG(r.rating), 2) FROM Review r
         WHERE r.source_client_id = ac.client_id AND r.status = 'Approved') AS avg_rating
  FROM Api_Client ac
  JOIN Merchant m ON m.merchant_id = ac.merchant_id;

CREATE VIEW v_review_provenance AS
SELECT r.review_id, r.status, r.is_verified_purchase, r.created_at,
       r.source_client_id, ac.site_url,
       COALESCE(sm.company_name, 'Reviewly') AS source_name,
       t.order_reference,
       p.product_id, p.product_name, p.sku, pm.company_name AS seller_name,
       (SELECT l.entry_hash FROM Review_Ledger l
         WHERE l.review_id = r.review_id ORDER BY l.ledger_id DESC LIMIT 1) AS entry_hash,
       (SELECT COUNT(*) FROM Review_Ledger l WHERE l.review_id = r.review_id) AS ledger_entries
  FROM Review r
  JOIN Product p   ON p.product_id = r.product_id
  JOIN Merchant pm ON pm.merchant_id = p.merchant_id
  LEFT JOIN Api_Client ac ON ac.client_id = r.source_client_id
  LEFT JOIN Merchant sm   ON sm.merchant_id = ac.merchant_id
  LEFT JOIN Transactions t ON t.transaction_id = r.transaction_id;
