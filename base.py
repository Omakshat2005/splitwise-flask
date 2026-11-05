"""
Splitwise Clone - single-file Flask + SQLite app (Oat / Blue / Pink edition).

Run:
    pip3 install flask
    python3 base.py
Then open http://127.0.0.1:5000

Features
  - Register / login (hashed passwords)
  - Add friends by username
  - Create groups from your friends, add members later
  - Add expenses (INR) split equally, by exact amounts, or by percentage
  - Live balances per group and overall ("you owe" / "you are owed")
  - Settle up payments
  - Chat: group chat and 1-to-1 chat with friends (auto-refreshes every 3s)
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_wtf.csrf import CSRFProtect
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "splitwise.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
csrf = CSRFProtect(app)

CATEGORIES = ["General", "Food", "Groceries", "Rent", "Travel", "Utilities",
              "Entertainment", "Shopping", "Other"]
CAT_ICONS = {"General": "🧾", "Food": "🍜", "Groceries": "🛒", "Rent": "🏠",
             "Travel": "✈️", "Utilities": "💡", "Entertainment": "🎬",
             "Shopping": "🛍️", "Other": "✨"}
app.jinja_env.globals["CAT_ICONS"] = CAT_ICONS

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friends (
    user_id INTEGER NOT NULL,
    friend_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, friend_id)
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_by INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL,          -- paise
    paid_by INTEGER NOT NULL,
    split_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expense_splits (
    expense_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    share INTEGER NOT NULL            -- paise
);
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    amount INTEGER NOT NULL,          -- paise
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,                 -- set for group chat
    sender_id INTEGER NOT NULL,
    recipient_id INTEGER,             -- set for direct messages
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def now():
    return datetime.now().strftime("%d %b, %H:%M")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def to_paise(text):
    """'250.50' -> 25050. Raises ValueError on bad input."""
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError("Enter a valid amount")
    if value <= 0 or value > Decimal("10000000"):
        raise ValueError("Amount must be greater than 0")
    return int((value * 100).quantize(Decimal("1")))


@app.template_filter("rupees")
def rupees(paise):
    """Format paise as ₹ with Indian digit grouping (12,34,567.89)."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(int(paise)), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "{}₹{}.{:02d}".format(sign, s, frac)


@app.context_processor
def inject_nav_user():
    uid = session.get("user_id")
    if uid is None:
        return {}
    row = get_db().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    return {"nav_user": row["username"] if row else "?", "nav_uid": uid}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    return session["user_id"]


def users_map():
    rows = get_db().execute("SELECT id, username FROM users").fetchall()
    return {r["id"]: r["username"] for r in rows}


def friend_ids(user_id):
    rows = get_db().execute(
        "SELECT friend_id FROM friends WHERE user_id=?", (user_id,)).fetchall()
    return [r["friend_id"] for r in rows]


def group_member_ids(group_id):
    rows = get_db().execute(
        "SELECT user_id FROM group_members WHERE group_id=?", (group_id,)).fetchall()
    return [r["user_id"] for r in rows]


def require_group_member(group_id):
    group = get_db().execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None:
        abort(404)
    if current_user_id() not in group_member_ids(group_id):
        abort(403)
    return group


def compute_debts(group_id=None):
    """Return list of (debtor_id, creditor_id, paise) with pairs netted out."""
    db = get_db()
    owe = defaultdict(int)

    sql = ("SELECT e.paid_by, s.user_id, s.share FROM expense_splits s "
           "JOIN expenses e ON e.id = s.expense_id")
    args = []
    if group_id is not None:
        sql += " WHERE e.group_id=?"
        args = [group_id]
    for r in db.execute(sql, args):
        if r["user_id"] != r["paid_by"]:
            owe[(r["user_id"], r["paid_by"])] += r["share"]

    sql = "SELECT from_user, to_user, amount FROM settlements"
    if group_id is not None:
        sql += " WHERE group_id=?"
    for r in db.execute(sql, args):
        owe[(r["from_user"], r["to_user"])] -= r["amount"]

    result, seen = [], set()
    for (a, b) in list(owe.keys()):
        if (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        net = owe.get((a, b), 0) - owe.get((b, a), 0)
        if net > 0:
            result.append((a, b, net))
        elif net < 0:
            result.append((b, a, -net))
    return result


def summarize_for_user(debts, user_id):
    i_owe = [(c, amt) for d, c, amt in debts if d == user_id]
    owed_to_me = [(d, amt) for d, c, amt in debts if c == user_id]
    return i_owe, owed_to_me


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if len(username) < 3 or not username.replace("_", "").isalnum():
            flash("Username must be 3+ letters/numbers/underscores.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)))
                db.commit()
            except sqlite3.IntegrityError:
                flash("That username is taken.", "error")
            else:
                session["user_id"] = cur.lastrowid
                return redirect(url_for("home"))
    return render_template("auth.html", title="Create your account", button="Sign up",
                           blurb="Split bills with friends without the awkward maths.",
                           other_url=url_for("login"), other_text="Already have an account? Log in")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        flash("Wrong username or password.", "error")
    return render_template("auth.html", title="Welcome back", button="Log in",
                           blurb="Pick up where you left off.",
                           other_url=url_for("register"), other_text="New here? Create an account")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Home: friends, groups, overall balances
# --------------------------------------------------------------------------

@app.route("/")
@login_required
def home():
    db = get_db()
    me = current_user_id()
    names = users_map()
    friends = [(fid, names[fid]) for fid in friend_ids(me)]
    groups = db.execute(
        "SELECT g.id, g.name, "
        "(SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS n "
        "FROM groups g JOIN group_members m ON m.group_id=g.id "
        "WHERE m.user_id=? ORDER BY g.id DESC", (me,)).fetchall()
    i_owe, owed_to_me = summarize_for_user(compute_debts(), me)
    total_owe = sum(a for _, a in i_owe)
    total_owed = sum(a for _, a in owed_to_me)
    both = total_owe + total_owed
    owe_pct = round(total_owe * 100 / both) if both else 0
    return render_template(
        "home.html", me=names[me], friends=friends, groups=groups, names=names,
        i_owe=i_owe, owed_to_me=owed_to_me,
        total_owe=total_owe, total_owed=total_owed, owe_pct=owe_pct)


@app.route("/friends/add", methods=["POST"])
@login_required
def add_friend():
    me = current_user_id()
    username = request.form.get("username", "").strip().lower()
    db = get_db()
    other = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if other is None:
        flash("No user with that username.", "error")
    elif other["id"] == me:
        flash("You can't add yourself.", "error")
    else:
        db.execute("INSERT OR IGNORE INTO friends VALUES (?, ?)", (me, other["id"]))
        db.execute("INSERT OR IGNORE INTO friends VALUES (?, ?)", (other["id"], me))
        db.commit()
        flash("Added {} as a friend.".format(username), "ok")
    return redirect(url_for("home"))


@app.route("/groups/create", methods=["POST"])
@login_required
def create_group():
    me = current_user_id()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the group a name.", "error")
        return redirect(url_for("home"))
    allowed = set(friend_ids(me))
    members = {me}
    for raw in request.form.getlist("members"):
        if raw.isdigit() and int(raw) in allowed:
            members.add(int(raw))
    db = get_db()
    cur = db.execute("INSERT INTO groups (name, created_by) VALUES (?, ?)", (name, me))
    gid = cur.lastrowid
    db.executemany("INSERT INTO group_members VALUES (?, ?)", [(gid, m) for m in members])
    db.commit()
    return redirect(url_for("group_page", gid=gid))


# --------------------------------------------------------------------------
# Group page, expenses, settle up
# --------------------------------------------------------------------------

@app.route("/group/<int:gid>")
@login_required
def group_page(gid):
    group = require_group_member(gid)
    db = get_db()
    me = current_user_id()
    names = users_map()
    member_ids = group_member_ids(gid)
    expenses = db.execute(
        "SELECT * FROM expenses WHERE group_id=? ORDER BY id DESC", (gid,)).fetchall()
    shares = defaultdict(dict)
    for r in db.execute(
            "SELECT s.expense_id, s.user_id, s.share FROM expense_splits s "
            "JOIN expenses e ON e.id=s.expense_id WHERE e.group_id=?", (gid,)):
        shares[r["expense_id"]][r["user_id"]] = r["share"]
    debts = compute_debts(gid)
    i_owe, owed_to_me = summarize_for_user(debts, me)
    addable = [(f, names[f]) for f in friend_ids(me) if f not in member_ids]
    return render_template(
        "group.html", group=group, names=names, me=me, members=member_ids,
        expenses=expenses, shares=shares, debts=debts, i_owe=i_owe,
        owed_to_me=owed_to_me, owe_map=dict(i_owe), categories=CATEGORIES,
        addable=addable)


@app.route("/group/<int:gid>/members/add", methods=["POST"])
@login_required
def add_group_member(gid):
    require_group_member(gid)
    me = current_user_id()
    raw = request.form.get("user_id", "")
    # Allow adding any existing user as a member
    db = get_db()
    other = db.execute("SELECT id FROM users WHERE id=?", (raw,)).fetchone()
    if other:
        db.execute("INSERT OR IGNORE INTO group_members VALUES (?, ?)", (gid, other["id"]))
        db.commit()
        flash("Member added.", "ok")
    else:
        flash("User not found.", "error")
    return redirect(url_for("group_page", gid=gid))


@app.route("/group/<int:gid>/expense", methods=["POST"])
@login_required
def add_expense(gid):
    require_group_member(gid)
    members = group_member_ids(gid)
    f = request.form
    back = redirect(url_for("group_page", gid=gid))
    try:
        description = f.get("description", "").strip()
        if not description:
            raise ValueError("Add a description")
        amount = to_paise(f.get("amount", ""))
        paid_by = int(f.get("paid_by", "0"))
        if paid_by not in members:
            raise ValueError("Payer must be a group member")
        category = f.get("category") if f.get("category") in CATEGORIES else "General"
        split_type = f.get("split_type", "equal")
        chosen = [u for u in members if f.get("p_{}".format(u))]
        if not chosen:
            raise ValueError("Select at least one person to split with")

        shares = {}
        if split_type == "equal":
            base, rem = divmod(amount, len(chosen))
            for i, u in enumerate(chosen):
                shares[u] = base + (1 if i < rem else 0)
        elif split_type == "exact":
            for u in chosen:
                shares[u] = to_paise(f.get("v_{}".format(u), "0") or "0")
            if sum(shares.values()) != amount:
                raise ValueError("Exact amounts must add up to {}".format(rupees(amount)))
        elif split_type == "percent":
            pcts = {u: Decimal(f.get("v_{}".format(u), "0") or "0") for u in chosen}
            if sum(pcts.values()) != Decimal("100"):
                raise ValueError("Percentages must add up to 100")
            for u in chosen:
                shares[u] = int((Decimal(amount) * pcts[u] / 100).quantize(Decimal("1")))
            shares[chosen[-1]] += amount - sum(shares.values())  # fix rounding
        else:
            raise ValueError("Unknown split type")
    except (ValueError, InvalidOperation) as exc:
        flash(str(exc) or "Invalid input", "error")
        return back

    db = get_db()
    cur = db.execute(
        "INSERT INTO expenses (group_id, description, category, amount, paid_by, split_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (gid, description, category, amount, paid_by, split_type, now()))
    db.executemany("INSERT INTO expense_splits VALUES (?, ?, ?)",
                   [(cur.lastrowid, u, s) for u, s in shares.items()])
    db.commit()
    flash("Expense added.", "ok")
    return back


@app.route("/group/<int:gid>/expense/<int:eid>/delete", methods=["POST"])
@login_required
def delete_expense(gid, eid):
    require_group_member(gid)
    db = get_db()
    exp = db.execute("SELECT * FROM expenses WHERE id=? AND group_id=?", (eid, gid)).fetchone()
    if exp and exp["paid_by"] == current_user_id():
        db.execute("DELETE FROM expense_splits WHERE expense_id=?", (eid,))
        db.execute("DELETE FROM expenses WHERE id=?", (eid,))
        db.commit()
        flash("Expense deleted.", "ok")
    else:
        flash("Only the person who paid can delete an expense.", "error")
    return redirect(url_for("group_page", gid=gid))


@app.route("/group/<int:gid>/settle", methods=["POST"])
@login_required
def settle(gid):
    require_group_member(gid)
    me = current_user_id()
    try:
        to_user = int(request.form.get("to_user", "0"))
        amount = to_paise(request.form.get("amount", ""))
        if to_user not in group_member_ids(gid) or to_user == me:
            raise ValueError("Pick someone in this group")
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO settlements (group_id, from_user, to_user, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (gid, me, to_user, amount, now()))
        db.commit()
        flash("Payment recorded.", "celebrate")
    return redirect(url_for("group_page", gid=gid))


# --------------------------------------------------------------------------
# Chat (group chat + direct messages), polled by the browser
# --------------------------------------------------------------------------

def messages_json(rows, names, me):
    return [{"id": r["id"], "sender": names.get(r["sender_id"], "?"),
             "body": r["body"], "time": r["created_at"],
             "mine": r["sender_id"] == me} for r in rows]


def read_body():
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or request.form.get("body") or "").strip()
    return body[:1000]


@app.route("/group/<int:gid>/messages", methods=["GET", "POST"])
@login_required
def group_messages(gid):
    require_group_member(gid)
    db, me = get_db(), current_user_id()
    if request.method == "POST":
        body = read_body()
        if not body:
            return jsonify(error="empty"), 400
        db.execute("INSERT INTO messages (group_id, sender_id, body, created_at) VALUES (?, ?, ?, ?)",
                   (gid, me, body, now()))
        db.commit()
        return jsonify(ok=True)
    after = request.args.get("after", "0")
    after = int(after) if after.isdigit() else 0
    rows = db.execute(
        "SELECT * FROM messages WHERE group_id=? AND id>? ORDER BY id", (gid, after)).fetchall()
    return jsonify(messages_json(rows, users_map(), me))


@app.route("/chat/<int:fid>")
@login_required
def dm_page(fid):
    if fid not in friend_ids(current_user_id()):
        abort(403)
    return render_template("dm.html", friend=users_map()[fid], fid=fid)


@app.route("/chat/<int:fid>/messages", methods=["GET", "POST"])
@login_required
def dm_messages(fid):
    me = current_user_id()
    if fid not in friend_ids(me):
        abort(403)
    db = get_db()
    if request.method == "POST":
        body = read_body()
        if not body:
            return jsonify(error="empty"), 400
        db.execute("INSERT INTO messages (sender_id, recipient_id, body, created_at) VALUES (?, ?, ?, ?)",
                   (me, fid, body, now()))
        db.commit()
        return jsonify(ok=True)
    after = request.args.get("after", "0")
    after = int(after) if after.isdigit() else 0
    rows = db.execute(
        "SELECT * FROM messages WHERE group_id IS NULL AND id>? AND "
        "((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?)) ORDER BY id",
        (after, me, fid, fid, me)).fetchall()
    return jsonify(messages_json(rows, users_map(), me))


# --------------------------------------------------------------------------
# Templates (kept inline so everything lives in this one file)
#
# Palette (approximations of the Pantone P-series swatches - tweak the CSS
# variables at the top of base.html if you want to match them more closely):
#   Oat   P 3-9 C    -> --oat
#   Blue  P 104-8 C  -> --blue
#   Pink  P 73-7 C   -> --pink
# --------------------------------------------------------------------------

TEMPLATES = {

"macros.html": r"""{% macro avatar(name, uid, size='') -%}
<span class="av av{{ uid % 3 }} {{ size }}" title="{{ name }}">{{ name[:1]|upper }}</span>
{%- endmacro %}
{% macro tick() -%}
<svg class="tick" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
{%- endmacro %}""",

"base.html": r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Splitwise Clone</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --oat:#F1E7CE; --oat-soft:#F9F4E6; --oat-deep:#E2D4AE; --oat-line:#D2C196;
  --blue:#4A78D0; --blue-deep:#2F58A8; --blue-soft:#D5E1F7;
  --pink:#EE8DB8; --pink-deep:#CF5F94; --pink-soft:#F9D5E4; --pink-ink:#A8265F;
  --ink:#1E2A47; --muted:#5B6177; --field:#FFFDF6;
  --ease:cubic-bezier(.2,.8,.2,1); --spring:cubic-bezier(.34,1.56,.64,1);
  --display:'Bricolage Grotesque','Avenir Next','Segoe UI',system-ui,sans-serif;
  --body:'Figtree','Avenir Next','Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:var(--body);font-size:16px;line-height:1.5;background:var(--oat);color:var(--ink);-webkit-font-smoothing:antialiased}
h1,h2,h3{font-family:var(--display);margin:0;letter-spacing:-.02em;line-height:1.1}
h1{font-size:clamp(30px,5vw,44px);font-weight:800}
h2{font-size:22px;font-weight:800;margin-bottom:14px}
h3{font-size:16px;font-weight:700;margin:18px 0 8px}
a{color:var(--blue-deep);font-weight:600}
b{font-weight:700}
.muted{color:var(--muted);font-size:14px}
:focus-visible{outline:3px solid var(--pink-deep);outline-offset:2px}

/* ---------- nav ---------- */
.nav{max-width:1000px;margin:0 auto;padding:18px 18px 6px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.nav nav{display:flex;align-items:center;gap:10px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--ink);font-family:var(--display);font-weight:800;font-size:21px;letter-spacing:-.02em}
.logo{width:42px;height:28px;overflow:visible}
.logo circle{transition:transform .5s var(--spring)}
.logo .c1{fill:var(--blue)} .logo .c2{fill:var(--pink);mix-blend-mode:multiply}
.brand:hover .c1{transform:translateX(-5px)} .brand:hover .c2{transform:translateX(5px)}
.pill-link{text-decoration:none;color:var(--ink);padding:8px 14px;border-radius:999px;transition:background .2s}
.pill-link:hover{background:var(--oat-deep)}

main{max-width:1000px;margin:18px auto 80px;padding:0 18px}

/* ---------- cards ---------- */
.card{background:var(--oat-soft);border:2px solid var(--oat-deep);border-radius:26px;padding:24px;box-shadow:0 5px 0 var(--oat-deep);margin-bottom:22px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.grid2>.card{margin-bottom:22px}
@media(max-width:780px){.grid2{grid-template-columns:1fr}}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px}
ul{list-style:none;padding:0;margin:0}

/* ---------- avatars ---------- */
.av{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:50%;font-family:var(--display);font-weight:800;font-size:16px;flex:none;border:3px solid var(--oat-soft)}
.av0{background:var(--blue);color:#fff} .av1{background:var(--pink);color:var(--ink)} .av2{background:var(--oat-deep);color:var(--ink)}
.av.xs{width:26px;height:26px;font-size:12px;border-width:2px} .av.sm{width:32px;height:32px;font-size:14px;border-width:2px} .av.md{width:44px;height:44px;font-size:18px}
.stack{display:flex} .stack .av+.av{margin-left:-12px}

/* ---------- buttons ---------- */
.btn{position:relative;overflow:hidden;display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:15px;padding:12px 20px;font:700 16px var(--body);text-decoration:none;cursor:pointer;
  background:var(--blue);color:#fff;box-shadow:0 4px 0 var(--blue-deep);transition:transform .18s var(--ease),box-shadow .18s var(--ease),background .2s,color .2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--blue-deep)}
.btn:active{transform:translateY(3px);box-shadow:0 1px 0 var(--blue-deep)}
.btn.pink{background:var(--pink);color:var(--ink);box-shadow:0 4px 0 var(--pink-deep)}
.btn.pink:hover{box-shadow:0 6px 0 var(--pink-deep)} .btn.pink:active{box-shadow:0 1px 0 var(--pink-deep)}
.btn.ghost{background:transparent;color:var(--ink);box-shadow:inset 0 0 0 2px var(--oat-line),0 3px 0 var(--oat-line)}
.btn.ghost:hover{background:var(--oat-deep);box-shadow:inset 0 0 0 2px var(--oat-line),0 5px 0 var(--oat-line)}
.btn.ghost:active{box-shadow:inset 0 0 0 2px var(--oat-line),0 1px 0 var(--oat-line)}
.btn.sm{padding:7px 14px;font-size:14px;border-radius:12px}
.btn.block{width:100%}
.btn.armed{background:var(--pink);color:var(--ink);box-shadow:0 4px 0 var(--pink-deep);animation:wiggle .4s}
.ripple{position:absolute;border-radius:50%;background:rgba(255,255,255,.55);transform:scale(0);animation:ripple .6s var(--ease);pointer-events:none}
.btn.pink .ripple,.btn.ghost .ripple,.btn.armed .ripple{background:rgba(30,42,71,.18)}
@keyframes ripple{to{transform:scale(4);opacity:0}}
@keyframes wiggle{25%{transform:rotate(-3deg)}75%{transform:rotate(3deg)}}

/* ---------- forms ---------- */
input,select{font:inherit;color:var(--ink)}
input[type=text],input[type=password],input[type=number],input:not([type]),select{background:var(--field);border:2px solid var(--oat-deep);border-radius:14px;padding:11px 14px;min-width:0;transition:border-color .2s,box-shadow .2s}
input:focus,select:focus{outline:0;border-color:var(--blue);box-shadow:0 0 0 4px var(--blue-soft)}
input::placeholder{color:#9a9a9a}
form.inline{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
form.inline.nowrap{flex-wrap:nowrap} form.inline input.grow{flex:1}
.f{display:flex;flex-direction:column;gap:6px;font-weight:600;font-size:14px;margin-bottom:14px}
.f>span:first-child,.fl{color:var(--muted)}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
@media(max-width:620px){.fgrid{grid-template-columns:1fr}}
.money{display:flex;align-items:center;background:var(--field);border:2px solid var(--oat-deep);border-radius:14px;padding-left:14px;transition:border-color .2s,box-shadow .2s}
.money:focus-within{border-color:var(--blue);box-shadow:0 0 0 4px var(--blue-soft)}
.money i{font-style:normal;font-weight:800;color:var(--blue-deep)}
.money input{border:0!important;box-shadow:none!important;background:transparent!important;flex:1;width:100%}
.flash-in{animation:flashfield .7s}
@keyframes flashfield{0%{box-shadow:0 0 0 0 var(--pink)}40%{box-shadow:0 0 0 6px var(--pink-soft)}100%{box-shadow:0 0 0 0 transparent}}

/* ---------- chips (checkbox / radio pills) ---------- */
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{position:relative;display:inline-block}
.chip input{position:absolute;opacity:0;pointer-events:none}
.chip-in{display:inline-flex;align-items:center;gap:8px;padding:6px 14px 6px 8px;border-radius:999px;border:2px solid var(--oat-deep);background:var(--field);font-weight:600;cursor:pointer;user-select:none;transition:background .2s,border-color .2s,transform .2s var(--spring)}
.chip.txt .chip-in{padding-left:14px}
.chip:hover .chip-in{transform:translateY(-1px)}
.chip input:checked+.chip-in{background:var(--blue-soft);border-color:var(--blue)}
.chip input:focus-visible+.chip-in{outline:3px solid var(--pink-deep);outline-offset:2px}
.chip .tick{width:0;height:14px;opacity:0;color:var(--blue-deep);transition:width .25s var(--spring),opacity .2s}
.chip input:checked+.chip-in .tick{width:16px;opacity:1}

/* ---------- segmented control ---------- */
.seg{position:relative;display:inline-flex;background:var(--oat-deep);border-radius:17px;padding:4px;margin-bottom:6px;max-width:100%}
.seg-pill{position:absolute;top:4px;bottom:4px;left:0;width:0;border-radius:13px;background:var(--blue);box-shadow:0 3px 0 var(--blue-deep);transition:transform .4s var(--spring),width .4s var(--spring)}
.seg label{position:relative;z-index:1;cursor:pointer}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label span{display:block;padding:9px 16px;font-weight:700;border-radius:13px;color:var(--muted);transition:color .25s}
.seg input:checked+span{color:#fff}
.seg input:focus-visible+span{outline:3px solid var(--pink-deep);outline-offset:1px}

/* ---------- split rows ---------- */
.splitrow{display:flex;align-items:center;gap:12px;margin:8px 0;transition:opacity .25s}
.splitrow.off{opacity:.45}
.val{max-width:0;opacity:0;overflow:hidden;padding:5px;margin:-5px;transition:max-width .4s var(--ease),opacity .3s}
.mode-exact .val,.mode-percent .val{max-width:170px;opacity:1}
.val input{width:130px}
.note{margin:12px 0 18px;padding:10px 14px;border-radius:14px;font-weight:600;font-size:14px;background:var(--blue-soft);color:var(--blue-deep);transition:background .3s,color .3s}
.note.warn{background:var(--pink-soft);color:var(--pink-ink)}

/* ---------- balances / debts ---------- */
.debt{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:2px dashed var(--oat-deep)}
.debt:last-child{border-bottom:0}
.debt .dtxt{flex:1}
.tag{font-weight:800;padding:4px 12px;border-radius:999px;white-space:nowrap}
.tag.owe{background:var(--pink-soft);color:var(--pink-ink)} .tag.owed{background:var(--blue-soft);color:var(--blue-deep)} .tag.neutral{background:var(--oat-deep)}
.empty{text-align:center;color:var(--muted);padding:18px 8px}
.empty .big-e{font-size:34px;display:block;margin-bottom:4px}

/* ---------- home hero ---------- */
.greet{margin:10px 0 22px}
.greet p{margin:6px 0 0;color:var(--muted)}
.hero{position:relative;display:grid;grid-template-columns:1fr 1fr;border-radius:30px;overflow:hidden;margin-bottom:22px;box-shadow:0 6px 0 var(--oat-line)}
.panel{padding:28px 28px 40px}
.panel .lbl{font-weight:700;font-size:15px}
.panel .amt{font-family:var(--display);font-weight:800;font-size:clamp(30px,5.5vw,50px);letter-spacing:-.03em;margin-top:6px;font-variant-numeric:tabular-nums}
.panel.owe{background:var(--pink);color:var(--ink);animation:slideL .8s var(--ease) both}
.panel.owed{background:var(--blue);color:#fff;animation:slideR .8s var(--ease) both}
@keyframes slideL{from{transform:translateX(45%);opacity:0}} @keyframes slideR{from{transform:translateX(-45%);opacity:0}}
.bar{position:absolute;left:0;right:0;bottom:0;height:12px;display:flex;background:rgba(30,42,71,.12)}
.bar i{display:block;height:100%;width:var(--w);animation:grow 1s .5s var(--ease) both}
.bar .b-owe{background:var(--pink-deep)} .bar .b-owed{background:var(--blue-deep)}
@keyframes grow{from{width:0}}
@media(max-width:620px){.hero{grid-template-columns:1fr}.panel{padding:22px 22px 26px}.panel.owed{padding-bottom:36px}}

/* ---------- tiles (groups / friends) ---------- */
.tile{display:flex;align-items:center;gap:14px;padding:12px 14px;margin:0 -14px;border-radius:18px;text-decoration:none;color:var(--ink);transition:background .2s,transform .25s var(--spring)}
.tile:hover{background:var(--oat-deep);transform:translateX(4px)}
.tile .gl{display:grid;place-items:center;width:46px;height:46px;border-radius:15px;font-family:var(--display);font-weight:800;font-size:20px;flex:none}
.g0{background:var(--blue);color:#fff} .g1{background:var(--pink);color:var(--ink)} .g2{background:var(--oat-deep);color:var(--ink)}
.tile .tname{flex:1;font-weight:700;line-height:1.2} .tile small{display:block;color:var(--muted);font-weight:500}
.tile .go{width:18px;height:18px;color:var(--muted);transition:transform .25s var(--spring)} .tile:hover .go{transform:translateX(4px);color:var(--blue-deep)}
.frow{display:flex;align-items:center;gap:12px;padding:8px 0}
.frow span.n{flex:1;font-weight:600}

/* ---------- group page ---------- */
.back{text-decoration:none;font-size:14px;color:var(--muted)} .back:hover{color:var(--blue-deep)}
.ghead h1{margin-top:6px}
.ghead .muted{margin:12px 0 0}

/* ---------- expenses ---------- */
.exp{border-bottom:2px dashed var(--oat-deep)} .exp:last-child{border-bottom:0}
.exp-head{display:flex;align-items:center;gap:14px;padding:14px 4px;cursor:pointer;border-radius:16px;transition:background .2s}
.exp-head:hover{background:rgba(226,212,174,.45)}
.cat{display:grid;place-items:center;width:46px;height:46px;border-radius:15px;background:var(--pink-soft);font-size:22px;flex:none;transition:transform .35s var(--spring)}
.exp:nth-child(even) .cat{background:var(--blue-soft)}
.exp-head:hover .cat{transform:rotate(-8deg) scale(1.08)}
.etitle{flex:1;min-width:0;line-height:1.25} .etitle small{display:block;color:var(--muted);font-weight:500}
.eamt{font-family:var(--display);font-weight:800;font-size:19px}
.chev{width:18px;height:18px;color:var(--muted);transition:transform .35s var(--spring)}
.exp.open .chev{transform:rotate(180deg)}
.exp-detail{display:grid;grid-template-rows:0fr;transition:grid-template-rows .4s var(--ease)}
.exp.open .exp-detail{grid-template-rows:1fr}
.exp-detail>div{overflow:hidden}
.shares{display:flex;flex-wrap:wrap;gap:8px;padding:2px 4px 14px 64px;align-items:center}
.sh{display:inline-flex;align-items:center;gap:6px;padding:4px 12px 4px 4px;border-radius:999px;background:var(--field);border:2px solid var(--oat-deep);font-size:14px}
.sh-title{font-weight:700;color:var(--muted);font-size:14px;margin-right:4px}
.exp-detail form{padding:0 4px 16px 64px}

/* ---------- chat ---------- */
.chatbox{height:290px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;background:var(--oat);border-radius:20px;padding:14px;margin-bottom:12px;scroll-behavior:smooth}
.chat-empty{margin:auto;color:var(--muted);text-align:center}
.bub{max-width:80%;animation:pop .35s var(--spring);transform-origin:bottom}
.bub .txt{padding:9px 14px;border-radius:19px;overflow-wrap:anywhere}
.bub .who{font-size:12px;font-weight:700;color:var(--muted);margin:0 0 2px 10px}
.bub .time{font-size:11px;color:var(--muted);margin:2px 8px 0}
.bub.mine{align-self:flex-end;text-align:right} .bub.mine .txt{background:var(--blue);color:#fff;border-bottom-right-radius:6px}
.bub.theirs{align-self:flex-start} .bub.theirs .txt{background:var(--field);border:2px solid var(--oat-deep);border-bottom-left-radius:6px}
@keyframes pop{from{transform:scale(.7) translateY(10px);opacity:0}}

/* ---------- auth ---------- */
.auth{max-width:420px;margin:30px auto 0;text-align:center}
.auth .hero-logo{width:120px;height:72px;margin:0 auto 10px;display:block;overflow:visible}
.auth .hero-logo .c1{fill:var(--blue);animation:joinL 1.1s var(--spring) both}
.auth .hero-logo .c2{fill:var(--pink);mix-blend-mode:multiply;animation:joinR 1.1s var(--spring) both}
@keyframes joinL{from{transform:translateX(-46px)}} @keyframes joinR{from{transform:translateX(46px)}}
.auth form{text-align:left;margin-top:18px}
.auth .f{margin-bottom:14px} .auth input{width:100%}
.auth .muted{margin:0 0 4px}

/* ---------- toasts & confetti ---------- */
#toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;flex-direction:column;gap:10px;z-index:50;width:min(92vw,420px);pointer-events:none}
.toast{pointer-events:auto;display:flex;align-items:center;gap:12px;padding:13px 18px;border-radius:18px;font-weight:700;cursor:pointer;box-shadow:0 5px 0 rgba(30,42,71,.25);animation:toastin .5s var(--spring) both;background:var(--blue);color:#fff}
.toast.error{background:var(--pink);color:var(--ink)}
.toast .ic{font-size:20px}
.toast.out{animation:toastout .3s ease-in forwards}
@keyframes toastin{from{transform:translateY(40px) scale(.9);opacity:0}} @keyframes toastout{to{transform:translateY(20px) scale(.95);opacity:0}}
.confetti{position:fixed;left:50%;bottom:60px;width:10px;height:14px;border-radius:3px;z-index:60;pointer-events:none;animation:conf 1.4s cubic-bezier(.1,.7,.3,1) forwards}
@keyframes conf{to{transform:translate(var(--x),var(--y)) rotate(var(--r));opacity:0}}

@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-delay:0s!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
@media(max-width:480px){.brand span{display:none}.card{padding:18px}}
</style></head><body>
{% from "macros.html" import avatar %}
<header class="nav">
  <a class="brand" href="/" aria-label="Home">
    <svg class="logo" viewBox="0 0 42 28" aria-hidden="true"><circle class="c1" cx="14" cy="14" r="12"/><circle class="c2" cx="28" cy="14" r="12"/></svg>
    <span>Splitwise Clone</span></a>
  {% if session.user_id %}<nav>
    <a class="pill-link" href="/">Home</a>
    {{ avatar(nav_user, nav_uid, 'sm') }}
    <a class="btn sm ghost" href="/logout">Log out</a>
  </nav>{% endif %}
</header>
<main>
  {% block content %}{% endblock %}
</main>
<div id="toasts" aria-live="polite"></div>
<script>
/* CSRF: attach token to every fetch/XHR so Flask-WTF won't reject POSTs */
(function () {
  var token = "{{ csrf_token() }}";
  if (!token) return;
  var orig = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-CSRFToken": token }, opts.headers || {});
    return orig.call(this, url, opts);
  };
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.addEventListener("load", function () {
      var t = this.getResponseHeader("X-CSRFToken") || token;
      if (t) this._csrf = t;
    });
    return origOpen.apply(this, arguments);
  };
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    if (this._csrf) this.setRequestHeader("X-CSRFToken", this._csrf);
    return origSend.call(this, body);
  };
})();

var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
var FLASHES = {{ get_flashed_messages(with_categories=true)|tojson }};

/* toasts */
function toast(msg, kind) {
  var box = document.getElementById("toasts");
  var t = document.createElement("div");
  t.className = "toast " + (kind === "error" ? "error" : "");
  var ic = document.createElement("span"); ic.className = "ic";
  ic.textContent = kind === "error" ? "⚠️" : kind === "celebrate" ? "🎉" : "✅";
  var tx = document.createElement("span"); tx.textContent = msg;
  t.appendChild(ic); t.appendChild(tx); box.appendChild(t);
  function close() { t.classList.add("out"); setTimeout(function () { t.remove(); }, 300); }
  t.addEventListener("click", close);
  setTimeout(close, 4200);
  if (kind === "celebrate") confetti();
}
function confetti() {
  if (REDUCE) return;
  var colors = ["#4A78D0", "#EE8DB8", "#F1E7CE", "#2F58A8", "#CF5F94"];
  for (var i = 0; i < 44; i++) {
    var c = document.createElement("i"); c.className = "confetti";
    c.style.background = colors[i % colors.length];
    c.style.setProperty("--x", (Math.random() * 520 - 260) + "px");
    c.style.setProperty("--y", (-Math.random() * 420 - 80) + "px");
    c.style.setProperty("--r", (Math.random() * 720 - 360) + "deg");
    c.style.animationDelay = (Math.random() * 0.15) + "s";
    document.body.appendChild(c);
    setTimeout((function (el) { return function () { el.remove(); }; })(c), 1700);
  }
}
FLASHES.forEach(function (f, i) { setTimeout(function () { toast(f[1], f[0]); }, 250 + i * 250); });

/* button ripple + two-step delete */
document.addEventListener("click", function (e) {
  var b = e.target.closest(".btn");
  if (!b) return;
  var r = b.getBoundingClientRect(), s = Math.max(r.width, r.height);
  var d = document.createElement("span"); d.className = "ripple";
  d.style.width = d.style.height = s + "px";
  d.style.left = (e.clientX - r.left - s / 2) + "px"; d.style.top = (e.clientY - r.top - s / 2) + "px";
  b.appendChild(d); setTimeout(function () { d.remove(); }, 600);
  var arm = b.getAttribute("data-arm");
  if (arm && !b.classList.contains("armed")) {
    e.preventDefault();
    var old = b.firstChild.textContent;
    b.classList.add("armed"); b.firstChild.textContent = arm;
    setTimeout(function () { b.classList.remove("armed"); b.firstChild.textContent = old; }, 3000);
  }
});

/* count-up numbers */
function fmtINR(p) {
  return "₹" + (p / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
document.querySelectorAll("[data-count]").forEach(function (el) {
  var target = parseInt(el.getAttribute("data-count"), 10);
  if (REDUCE || !target) return;
  var t0 = null, dur = 1000;
  el.textContent = fmtINR(0);
  function step(t) {
    if (t0 === null) t0 = t;
    var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = fmtINR(Math.round(target * e));
    if (k < 1) requestAnimationFrame(step);
  }
  setTimeout(function () { requestAnimationFrame(step); }, 350);
});

/* chat */
function initChat(url, boxId, formId, showNames) {
  var last = 0, first = true;
  var box = document.getElementById(boxId), form = document.getElementById(formId);
  function add(m) {
    var row = document.createElement("div"); row.className = "bub " + (m.mine ? "mine" : "theirs");
    if (showNames && !m.mine) { var w = document.createElement("div"); w.className = "who"; w.textContent = m.sender; row.appendChild(w); }
    var tx = document.createElement("div"); tx.className = "txt"; tx.textContent = m.body; row.appendChild(tx);
    var ti = document.createElement("div"); ti.className = "time"; ti.textContent = m.time; row.appendChild(ti);
    box.appendChild(row);
  }
  function poll() {
    fetch(url + "?after=" + last).then(function (r) { return r.json(); }).then(function (data) {
      if (data.length) {
        var empty = box.querySelector(".chat-empty"); if (empty) empty.remove();
        var near = first || box.scrollHeight - box.scrollTop - box.clientHeight < 90;
        data.forEach(function (m) { last = Math.max(last, m.id); add(m); });
        if (near) box.scrollTop = box.scrollHeight;
      }
      first = false;
    }).catch(function () {});
  }
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var inp = form.elements["body"], body = inp.value.trim(); if (!body) return;
    inp.value = "";
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                 body: JSON.stringify({ body: body }) }).then(poll);
  });
  poll(); setInterval(poll, 3000);
}
</script>
{% block scripts %}{% endblock %}
</body></html>""",

"auth.html": r"""{% extends "base.html" %}{% block content %}
<div class="card auth">
  <svg class="hero-logo" viewBox="0 0 42 28" aria-hidden="true"><circle class="c1" cx="14" cy="14" r="12"/><circle class="c2" cx="28" cy="14" r="12"/></svg>
  <h1 style="font-size:32px">{{ title }}</h1>
  <p class="muted" style="margin-top:8px">{{ blurb }}</p>
  <form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label class="f"><span>Username</span><input name="username" autocomplete="username" required></label>
    <label class="f"><span>Password</span><input name="password" type="password" autocomplete="current-password" required></label>
    <button class="btn block">{{ button }}</button>
  </form>
  <p style="margin:18px 0 0"><a href="{{ other_url }}">{{ other_text }}</a></p>
</div>{% endblock %}""",

"home.html": r"""{% extends "base.html" %}{% from "macros.html" import avatar, tick %}{% block content %}
<section class="greet"><h1>Hi, {{ me }}</h1><p>Here is where everyone stands.</p></section>

<section class="hero" aria-label="Your balance">
  <div class="panel owe"><span class="lbl">You owe</span><div class="amt" data-count="{{ total_owe }}">{{ total_owe|rupees }}</div></div>
  <div class="panel owed"><span class="lbl">You are owed</span><div class="amt" data-count="{{ total_owed }}">{{ total_owed|rupees }}</div></div>
  <div class="bar" aria-hidden="true">{% if total_owe or total_owed %}<i class="b-owe" style="--w:{{ owe_pct }}%"></i><i class="b-owed" style="--w:{{ 100 - owe_pct }}%"></i>{% endif %}</div>
</section>

<section class="card">
  <h2>Who owes who</h2>
  {% if i_owe or owed_to_me %}<ul>
    {% for uid, amt in i_owe %}<li class="debt">{{ avatar(names[uid], uid) }}<span class="dtxt">You owe <b>{{ names[uid] }}</b></span><span class="tag owe">{{ amt|rupees }}</span></li>{% endfor %}
    {% for uid, amt in owed_to_me %}<li class="debt">{{ avatar(names[uid], uid) }}<span class="dtxt"><b>{{ names[uid] }}</b> owes you</span><span class="tag owed">{{ amt|rupees }}</span></li>{% endfor %}
  </ul>{% else %}<div class="empty"><span class="big-e">🎉</span>All settled up. Nobody owes anybody.</div>{% endif %}
</section>

<div class="grid2">
  <section class="card"><h2>Your groups</h2>
    <ul>{% for grp in groups %}<li><a class="tile" href="{{ url_for('group_page', gid=grp.id) }}">
      <span class="gl g{{ grp.id % 3 }}">{{ grp.name[:1]|upper }}</span>
      <span class="tname">{{ grp.name }}<small>{{ grp.n }} member{{ '' if grp.n == 1 else 's' }}</small></span>
      <svg class="go" viewBox="0 0 16 16"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
    {% else %}<li class="empty">No groups yet. Add a friend, then start one for your next trip.</li>{% endfor %}</ul>
    <h3>Start a new group</h3>
    <form method="post" action="{{ url_for('create_group') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <p style="margin:0 0 12px"><input name="name" placeholder="Group name, e.g. Goa trip" required style="width:100%"></p>
      {% if friends %}<div class="chips" style="margin-bottom:14px">{% for fid, fname in friends %}
        <label class="chip"><input type="checkbox" name="members" value="{{ fid }}"><span class="chip-in">{{ avatar(fname, fid, 'xs') }}{{ fname }}{{ tick() }}</span></label>{% endfor %}</div>
      {% else %}<p class="muted">Add a friend first to put them in a group.</p>{% endif %}
      <button class="btn">Create group</button>
    </form>
  </section>

  <section class="card"><h2>Friends</h2>
    <ul>{% for fid, fname in friends %}<li class="frow">{{ avatar(fname, fid) }}<span class="n">{{ fname }}</span>
      <a class="btn sm ghost" href="{{ url_for('dm_page', fid=fid) }}">Chat</a></li>
    {% else %}<li class="empty">No friends yet. Add someone by their username.</li>{% endfor %}</ul>
    <h3>Add a friend</h3>
    <form class="inline nowrap" method="post" action="{{ url_for('add_friend') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input class="grow" name="username" placeholder="Their username" required><button class="btn">Add</button>
    </form>
    <p class="muted" style="margin-bottom:0">Your friend needs to have signed up first.</p>
  </section>
</div>{% endblock %}""",

"group.html": r"""{% extends "base.html" %}{% from "macros.html" import avatar, tick %}{% block content %}
<section class="card ghead">
  <div class="row">
    <div><a class="back" href="/">&lsaquo; All groups</a><h1>{{ group.name }}</h1></div>
    <div class="stack">{% for m in members %}{{ avatar(names[m], m, 'md') }}{% endfor %}</div>
  </div>
  <p class="muted">{% for m in members %}{{ names[m] }}{% if not loop.last %}, {% endif %}{% endfor %}</p>
  {% if addable %}<form class="inline" style="margin-top:14px" method="post" action="{{ url_for('add_group_member', gid=group.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <select name="user_id">{% for fid, fname in addable %}<option value="{{ fid }}">{{ fname }}</option>{% endfor %}</select>
    <button class="btn sm">Add member</button></form>{% endif %}
</section>

<div class="grid2">
  <section class="card"><h2>Balances</h2>
    {% if debts %}<ul>{% for d, c, amt in debts %}
      <li class="debt">{{ avatar(names[d], d) }}
        <span class="dtxt">{% if d == me %}<b>You</b> owe{% else %}<b>{{ names[d] }}</b> owes{% endif %} {% if c == me %}<b>you</b>{% else %}<b>{{ names[c] }}</b>{% endif %}</span>
        <span class="tag {{ 'owe' if d == me else 'owed' if c == me else 'neutral' }}">{{ amt|rupees }}</span></li>
    {% endfor %}</ul>{% else %}<div class="empty"><span class="big-e">🎉</span>Everyone is settled up.</div>{% endif %}
    {% if members|length > 1 %}<h3>Settle up</h3>
    <form class="inline" method="post" action="{{ url_for('settle', gid=group.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <select name="to_user" id="to_user" data-owed='{{ owe_map|tojson }}'>{% for m in members if m != me %}<option value="{{ m }}">{{ names[m] }}</option>{% endfor %}</select>
      <span class="money" style="width:150px"><i>₹</i><input id="settle_amt" name="amount" type="number" step="0.01" min="0.01" placeholder="Amount" required></span>
      <button class="btn pink">I paid them</button></form>
    <p class="muted" style="margin:8px 0 0">If you owe them, the amount fills in for you.</p>{% endif %}
  </section>

  <section class="card"><h2>Group chat</h2>
    <div id="chat" class="chatbox"><p class="chat-empty">No messages yet. Say hi.</p></div>
    <form id="chatform" class="inline nowrap"><input class="grow" name="body" placeholder="Message the group" autocomplete="off"><button class="btn">Send</button></form>
  </section>
</div>

<section class="card"><h2>Add an expense</h2>
  <form id="expform" class="mode-equal" method="post" action="{{ url_for('add_expense', gid=group.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="fgrid">
      <label class="f"><span>What was it for?</span><input name="description" placeholder="Dinner at Gopi" required></label>
      <label class="f"><span>Amount</span><span class="money"><i>₹</i><input name="amount" type="number" step="0.01" min="0.01" placeholder="0.00" required></span></label>
    </div>
    <div class="f"><span class="fl">Category</span>
      <div class="chips">{% for c in categories %}<label class="chip txt"><input type="radio" name="category" value="{{ c }}" {{ 'checked' if loop.first }}><span class="chip-in">{{ CAT_ICONS[c] }} {{ c }}</span></label>{% endfor %}</div></div>
    <label class="f" style="max-width:300px"><span>Paid by</span>
      <select name="paid_by">{% for m in members %}<option value="{{ m }}" {{ 'selected' if m == me }}>{{ names[m] }}{{ ' (you)' if m == me }}</option>{% endfor %}</select></label>
    <div class="f"><span class="fl">How to split</span>
      <div class="seg"><i class="seg-pill"></i>
        <label><input type="radio" name="split_type" value="equal" checked><span>Equally</span></label>
        <label><input type="radio" name="split_type" value="exact"><span>Exact amounts</span></label>
        <label><input type="radio" name="split_type" value="percent"><span>Percentages</span></label>
      </div></div>
    <div class="fl" style="margin-bottom:4px;font-weight:600;font-size:14px">Split between</div>
    {% for m in members %}<div class="splitrow">
      <label class="chip"><input type="checkbox" name="p_{{ m }}" checked><span class="chip-in">{{ avatar(names[m], m, 'xs') }}{{ names[m] }}{{ tick() }}</span></label>
      <div class="val"><input class="splitval" type="number" step="0.01" name="v_{{ m }}" placeholder="₹" disabled></div>
    </div>{% endfor %}
    <p id="splitnote" class="note">Enter an amount to see each share.</p>
    <button class="btn">Save expense</button>
  </form>
</section>

<section class="card"><h2>Expenses</h2>
  <ul>{% for e in expenses %}<li class="exp">
    <div class="exp-head" role="button" tabindex="0" aria-expanded="false">
      <span class="cat">{{ CAT_ICONS.get(e.category, '🧾') }}</span>
      <span class="etitle"><b>{{ e.description }}</b><small>Paid by {{ names[e.paid_by] }} on {{ e.created_at }}</small></span>
      <span class="eamt">{{ e.amount|rupees }}</span>
      <svg class="chev" viewBox="0 0 16 16"><path d="M3 6l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="exp-detail"><div>
      <div class="shares"><span class="sh-title">Split {{ e.split_type }}</span>
        {% for uid, sh in shares[e.id].items() %}<span class="sh">{{ avatar(names[uid], uid, 'xs') }}{{ names[uid] }} <b>{{ sh|rupees }}</b></span>{% endfor %}</div>
      {% if e.paid_by == me %}<form method="post" action="{{ url_for('delete_expense', gid=group.id, eid=e.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn sm ghost" data-arm="Tap again to delete">Delete</button></form>{% endif %}
    </div></div>
  </li>{% else %}<li class="empty"><span class="big-e">🧾</span>No expenses yet. Add the first one above.</li>{% endfor %}</ul>
</section>{% endblock %}

{% block scripts %}<script>
initChat("{{ url_for('group_messages', gid=group.id) }}", "chat", "chatform", true);

/* expense list: tap to expand */
document.querySelectorAll(".exp-head").forEach(function (h) {
  function toggle() { var li = h.parentNode, open = li.classList.toggle("open"); h.setAttribute("aria-expanded", open); }
  h.addEventListener("click", toggle);
  h.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
});

/* settle up: prefill what you owe */
var sel = document.getElementById("to_user");
if (sel) {
  var owed = JSON.parse(sel.getAttribute("data-owed")), amtIn = document.getElementById("settle_amt");
  function fill() {
    var p = owed[sel.value];
    amtIn.value = p ? (p / 100).toFixed(2) : "";
    if (p) { amtIn.parentNode.classList.remove("flash-in"); void amtIn.offsetWidth; amtIn.parentNode.classList.add("flash-in"); }
  }
  sel.addEventListener("change", fill); fill();
}

/* add expense: sliding split control + live summary */
(function () {
  var form = document.getElementById("expform"), seg = form.querySelector(".seg"), pill = seg.querySelector(".seg-pill");
  var note = document.getElementById("splitnote"), amount = form.elements["amount"];
  function placePill() {
    var on = seg.querySelector("input:checked"); if (!on) return;
    var l = on.parentNode; pill.style.width = l.offsetWidth + "px"; pill.style.transform = "translateX(" + l.offsetLeft + "px)";
  }
  function setNote(text, warn) { note.textContent = text; note.className = "note" + (warn ? " warn" : ""); }
  function money(n) { return "₹" + n.toFixed(2); }
  function update() {
    var mode = form.querySelector("input[name=split_type]:checked").value;
    form.classList.remove("mode-equal", "mode-exact", "mode-percent"); form.classList.add("mode-" + mode);
    var n = 0, sum = 0;
    form.querySelectorAll(".splitrow").forEach(function (row) {
      var cb = row.querySelector("input[type=checkbox]"), v = row.querySelector(".splitval");
      row.classList.toggle("off", !cb.checked);
      v.disabled = !cb.checked || mode === "equal";
      v.placeholder = mode === "percent" ? "%" : "₹";
      if (cb.checked) { n++; sum += parseFloat(v.value) || 0; }
    });
    var amt = parseFloat(amount.value) || 0;
    if (!n) return setNote("Pick at least one person to split with.", true);
    if (mode === "equal") return setNote(amt ? money(amt / n) + " each across " + n + (n === 1 ? " person" : " people") : "Enter an amount to see each share.", false);
    var target = mode === "percent" ? 100 : amt;
    if (!target) return setNote("Enter an amount first.", true);
    var diff = Math.round((target - sum) * 100) / 100, unit = mode === "percent" ? "%" : "₹";
    if (Math.abs(diff) < 0.005) return setNote("Perfect, everything is assigned.", false);
    var txt = Math.abs(diff).toFixed(2);
    setNote(diff > 0 ? (unit === "₹" ? "₹" + txt : txt + "%") + " left to assign" : "Over by " + (unit === "₹" ? "₹" + txt : txt + "%"), true);
  }
  form.addEventListener("input", update);
  form.addEventListener("change", function () { update(); placePill(); });
  window.addEventListener("resize", placePill);
  window.addEventListener("load", placePill);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(placePill);
  update(); placePill();
})();
</script>{% endblock %}""",

"dm.html": r"""{% extends "base.html" %}{% from "macros.html" import avatar %}{% block content %}
<section class="card">
  <div class="row"><div style="display:flex;align-items:center;gap:14px">{{ avatar(friend, fid, 'md') }}<div><a class="back" href="/">&lsaquo; Home</a><h1 style="font-size:30px">{{ friend }}</h1></div></div></div>
</section>
<section class="card">
  <div id="chat" class="chatbox" style="height:400px"><p class="chat-empty">No messages yet. Say hi to {{ friend }}.</p></div>
  <form id="chatform" class="inline nowrap"><input class="grow" name="body" placeholder="Type a message" autocomplete="off"><button class="btn">Send</button></form>
</section>{% endblock %}
{% block scripts %}<script>initChat("{{ url_for('dm_messages', fid=fid) }}", "chat", "chatform", false);</script>{% endblock %}""",
}

app.jinja_loader = DictLoader(TEMPLATES)

# --------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5050)
