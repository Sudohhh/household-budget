import sqlite3
import math
import os
from datetime import date, timedelta
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")
app.json.sort_keys = False

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "household.db")

CATEGORIES = {
    "生活費": ["食費", "生活雑費", "家賃", "水道光熱費", "家具・家電", "その他"],
    "娯楽費": ["外食", "デート", "旅行", "嗜好品"],
}
PAYMENTS = ["あかり立替", "だいち立替", "共通カード"]


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                detail TEXT,
                payment TEXT NOT NULL,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                UNIQUE(year, month, category)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                person TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(year, month, person)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL,
                person TEXT,
                notes TEXT,
                auto_key TEXT UNIQUE,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        try:
            conn.execute("ALTER TABLE expenses ADD COLUMN recurring_key TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_recurring_key
            ON expenses (recurring_key) WHERE recurring_key IS NOT NULL
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (date)
        """)


RENT_AMOUNT = 74667
RENT_DAY = 27
_rent_ensured_month = None


def ensure_monthly_rent():
    """毎月27日以降に、翌月分の家賃をあかり立替で自動生成する"""
    global _rent_ensured_month
    today = date.today()
    if today.day < RENT_DAY:
        return
    month_key = (today.year, today.month)
    if _rent_ensured_month == month_key:
        return
    if today.month == 12:
        next_month = 1
    else:
        next_month = today.month + 1
    rent_date = f"{today.year}-{today.month:02d}-{RENT_DAY:02d}"
    recurring_key = f"rent:{today.year}-{today.month:02d}"
    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO expenses
                (date, amount, category, detail, payment, notes, recurring_key)
            VALUES (?, ?, '家賃', ?, 'あかり立替', '', ?)
        """, (rent_date, RENT_AMOUNT, f"{next_month}月分", recurring_key))
    _rent_ensured_month = month_key


CARD_DAY = 26
_card_ensured_month = None


def ensure_monthly_card_deduction():
    """前月分の共通カード合計を口座取引に自動登録する（未登録なら日付に関わらず実行）"""
    global _card_ensured_month
    today = date.today()
    month_key = (today.year, today.month)
    if _card_ensured_month == month_key:
        return

    prev_year = today.year if today.month > 1 else today.year - 1
    prev_month = today.month - 1 if today.month > 1 else 12
    auto_key = f"card:{prev_year}-{prev_month:02d}"

    pm_from = f"{prev_year}-{prev_month:02d}-01"
    pm_to = f"{today.year}-{today.month:02d}-01"
    card_date = f"{today.year}-{today.month:02d}-{CARD_DAY:02d}"

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM account_transactions WHERE auto_key = ?", (auto_key,)
        ).fetchone()
        if existing:
            _card_ensured_month = month_key
            return

        card_total = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total FROM expenses
            WHERE date >= ? AND date < ? AND payment = '共通カード'
        """, (pm_from, pm_to)).fetchone()["total"]

        if card_total > 0:
            conn.execute("""
                INSERT OR IGNORE INTO account_transactions
                    (date, amount, type, notes, auto_key)
                VALUES (?, ?, 'カード引き落とし', ?, ?)
            """, (card_date, -card_total, f"{prev_year}年{prev_month}月分", auto_key))

    _card_ensured_month = month_key


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/categories")
def get_categories():
    return jsonify({"categories": CATEGORIES, "payments": PAYMENTS})


@app.route("/api/months")
def get_months():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT
                CAST(strftime('%Y', date) AS INTEGER) AS year,
                CAST(strftime('%m', date) AS INTEGER) AS month
            FROM expenses
            ORDER BY year DESC, month DESC
        """).fetchall()
    return jsonify([{"year": r["year"], "month": r["month"]} for r in rows])


@app.route("/api/expenses", methods=["GET"])
def get_expenses():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month:
        return jsonify({"error": "year and month required"}), 400

    today = date.today()
    if year == today.year and month == today.month:
        ensure_monthly_rent()

    d_from = f"{year}-{month:02d}-01"
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    d_to = f"{next_y}-{next_m:02d}-01"
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, date, amount, category, detail, payment, notes
            FROM expenses
            WHERE date >= ? AND date < ?
            ORDER BY date DESC, id DESC
        """, (d_from, d_to)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/expenses", methods=["POST"])
def add_expense():
    data = request.json
    required = ["date", "amount", "category", "payment"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO expenses (date, amount, category, detail, payment, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data["date"],
            int(data["amount"]),
            data["category"],
            data.get("detail", ""),
            data["payment"],
            data.get("notes", ""),
        ))
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (new_id,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/expenses/<int:expense_id>", methods=["PUT"])
def update_expense(expense_id):
    data = request.json
    required = ["date", "amount", "category", "payment"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    with get_db() as conn:
        conn.execute("""
            UPDATE expenses
            SET date=?, amount=?, category=?, detail=?, payment=?, notes=?
            WHERE id=?
        """, (
            data["date"],
            int(data["amount"]),
            data["category"],
            data.get("detail", ""),
            data["payment"],
            data.get("notes", ""),
            expense_id,
        ))
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    with get_db() as conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    return jsonify({"ok": True})


@app.route("/api/summary", methods=["GET"])
def get_summary():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month:
        return jsonify({"error": "year and month required"}), 400

    d_from = f"{year}-{month:02d}-01"
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    d_to = f"{next_y}-{next_m:02d}-01"
    with get_db() as conn:
        rows = conn.execute("""
            SELECT category, payment, SUM(amount) AS total
            FROM expenses
            WHERE date >= ? AND date < ?
            GROUP BY category, payment
        """, (d_from, d_to)).fetchall()

        budgets = conn.execute("""
            SELECT category, amount FROM budgets
            WHERE year = ? AND month = ?
        """, (year, month)).fetchall()

        statuses = conn.execute("""
            SELECT person, status FROM settlement_status
            WHERE year = ? AND month = ?
        """, (year, month)).fetchall()

    by_category = {}
    by_payment = {"共通カード": 0, "あかり立替": 0, "だいち立替": 0}
    for r in rows:
        cat = r["category"]
        by_category[cat] = by_category.get(cat, 0) + r["total"]
        if r["payment"] in by_payment:
            by_payment[r["payment"]] += r["total"]

    budget_map = {b["category"]: b["amount"] for b in budgets}
    status_map = {s["person"]: s["status"] for s in statuses}

    total = sum(by_category.values())
    akari_paid = by_payment["あかり立替"]
    daichi_paid = by_payment["だいち立替"]
    akari_owes = math.ceil(total / 2 - akari_paid) if total > 0 else 0
    daichi_owes = math.ceil(total / 2 - daichi_paid) if total > 0 else 0

    categories_out = []
    for parent, subs in CATEGORIES.items():
        for sub in subs:
            categories_out.append({
                "parent": parent,
                "name": sub,
                "amount": by_category.get(sub, 0),
                "budget": budget_map.get(sub),
            })

    return jsonify({
        "total": total,
        "by_payment": by_payment,
        "categories": categories_out,
        "settlement": {
            "akari_paid": akari_paid,
            "daichi_paid": daichi_paid,
            "akari_owes": akari_owes,
            "daichi_owes": daichi_owes,
            "akari_status": status_map.get("akari", "pending"),
            "daichi_status": status_map.get("daichi", "pending"),
        }
    })


@app.route("/api/settlement-status", methods=["POST"])
def set_settlement_status():
    data = request.json
    required = ["year", "month", "person", "status"]
    for field in required:
        if data.get(field) is None:
            return jsonify({"error": f"{field} is required"}), 400
    if data["status"] not in ["pending", "partial", "completed"]:
        return jsonify({"error": "invalid status"}), 400
    if data["person"] not in ["akari", "daichi"]:
        return jsonify({"error": "invalid person"}), 400

    year, month, person, status = data["year"], data["month"], data["person"], data["status"]
    owes = int(data.get("owes", 0))

    PARTIAL_AMOUNT = 100000
    partial_key = f"settlement-partial:{person}:{year:04d}-{month:02d}"
    completed_key = f"settlement-completed:{person}:{year:04d}-{month:02d}"
    tx_type = "振込入金(あかり)" if person == "akari" else "振込入金(だいち)"
    tx_notes = f"{year}年{month}月分精算"
    today_str = date.today().strftime("%Y-%m-%d")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO settlement_status (year, month, person, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(year, month, person) DO UPDATE SET
                status = excluded.status,
                updated_at = datetime('now', 'localtime')
        """, (year, month, person, status))

        if owes > 0:
            if status == "partial":
                conn.execute("""
                    INSERT OR IGNORE INTO account_transactions (date, amount, type, person, notes, auto_key)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (today_str, PARTIAL_AMOUNT, tx_type, person, tx_notes, partial_key))
                conn.execute("DELETE FROM account_transactions WHERE auto_key = ?", (completed_key,))

            elif status == "completed":
                partial_exists = conn.execute(
                    "SELECT id FROM account_transactions WHERE auto_key = ?", (partial_key,)
                ).fetchone()
                if partial_exists:
                    remainder = owes - PARTIAL_AMOUNT
                    if remainder > 0:
                        conn.execute("""
                            INSERT OR IGNORE INTO account_transactions (date, amount, type, person, notes, auto_key)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (today_str, remainder, tx_type, person, tx_notes, completed_key))
                else:
                    conn.execute("""
                        INSERT OR IGNORE INTO account_transactions (date, amount, type, person, notes, auto_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (today_str, owes, tx_type, person, tx_notes, completed_key))

        if status == "pending":
            conn.execute(
                "DELETE FROM account_transactions WHERE auto_key IN (?, ?)",
                (partial_key, completed_key)
            )

    return jsonify({"ok": True})


@app.route("/api/account", methods=["GET"])
def get_account():
    ensure_monthly_card_deduction()

    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, date, amount, type, person, notes, auto_key
            FROM account_transactions
            ORDER BY date DESC, id DESC
        """).fetchall()
        balance_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS balance FROM account_transactions"
        ).fetchone()

    return jsonify({
        "transactions": [dict(r) for r in rows],
        "balance": balance_row["balance"],
        "card_suggestion": None,
    })


@app.route("/api/account", methods=["POST"])
def add_account_transaction():
    data = request.json
    required = ["date", "amount", "type"]
    for field in required:
        if data.get(field) is None:
            return jsonify({"error": f"{field} is required"}), 400

    auto_key = data.get("auto_key")
    with get_db() as conn:
        if auto_key:
            conn.execute("""
                INSERT OR IGNORE INTO account_transactions (date, amount, type, person, notes, auto_key)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (data["date"], int(data["amount"]), data["type"],
                  data.get("person"), data.get("notes", ""), auto_key))
            row = conn.execute(
                "SELECT * FROM account_transactions WHERE auto_key = ?", (auto_key,)
            ).fetchone()
        else:
            cur = conn.execute("""
                INSERT INTO account_transactions (date, amount, type, person, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (data["date"], int(data["amount"]), data["type"],
                  data.get("person"), data.get("notes", "")))
            row = conn.execute(
                "SELECT * FROM account_transactions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/account/<int:tx_id>", methods=["PUT"])
def update_account_transaction(tx_id):
    data = request.json
    required = ["date", "amount", "type"]
    for field in required:
        if data.get(field) is None:
            return jsonify({"error": f"{field} is required"}), 400
    person = {"振込入金(あかり)": "akari", "振込入金(だいち)": "daichi"}.get(data["type"])
    with get_db() as conn:
        conn.execute("""
            UPDATE account_transactions
            SET date=?, amount=?, type=?, person=?, notes=?
            WHERE id=?
        """, (data["date"], int(data["amount"]), data["type"],
              person, data.get("notes", ""), tx_id))
        row = conn.execute("SELECT * FROM account_transactions WHERE id = ?", (tx_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/account/<int:tx_id>", methods=["DELETE"])
def delete_account_transaction(tx_id):
    with get_db() as conn:
        conn.execute("DELETE FROM account_transactions WHERE id = ?", (tx_id,))
    return jsonify({"ok": True})


@app.route("/api/budgets", methods=["POST"])
def set_budget():
    data = request.json
    required = ["year", "month", "category", "amount"]
    for field in required:
        if data.get(field) is None:
            return jsonify({"error": f"{field} is required"}), 400

    with get_db() as conn:
        conn.execute("""
            INSERT INTO budgets (year, month, category, amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(year, month, category) DO UPDATE SET amount = excluded.amount
        """, (data["year"], data["month"], data["category"], int(data["amount"])))
    return jsonify({"ok": True})


init_db()
ensure_monthly_rent()
ensure_monthly_card_deduction()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
