import sqlite3
import math
from datetime import date
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")
app.json.sort_keys = False

DB = "household.db"

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

    month_str = f"{year}-{month:02d}"
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, date, amount, category, detail, payment, notes
            FROM expenses
            WHERE strftime('%Y-%m', date) = ?
            ORDER BY date DESC, id DESC
        """, (month_str,)).fetchall()
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

    month_str = f"{year}-{month:02d}"
    with get_db() as conn:
        rows = conn.execute("""
            SELECT category, payment, SUM(amount) AS total
            FROM expenses
            WHERE strftime('%Y-%m', date) = ?
            GROUP BY category, payment
        """, (month_str,)).fetchall()

        budgets = conn.execute("""
            SELECT category, amount FROM budgets
            WHERE year = ? AND month = ?
        """, (year, month)).fetchall()

    # aggregate by category
    by_category = {}
    by_payment = {"共通カード": 0, "あかり立替": 0, "だいち立替": 0}
    for r in rows:
        cat = r["category"]
        by_category[cat] = by_category.get(cat, 0) + r["total"]
        if r["payment"] in by_payment:
            by_payment[r["payment"]] += r["total"]

    budget_map = {b["category"]: b["amount"] for b in budgets}

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
        }
    })


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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
