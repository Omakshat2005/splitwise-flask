# Splitwise Clone

A single-file Flask + SQLite clone of [Splitwise](https://splitwise.com/).
Split bills with friends, track who owes what across groups, settle up, and chat.

## Features

- **Accounts** — register / log in with hashed passwords (session-based).
- **Friends** — add people by username; friendship is mutual.
- **Groups** — create a group from your friends and add members later.
- **Expenses** (INR, stored in paise for exact arithmetic) split three ways:
  - **Equal** — split evenly, remainder distributed one paise at a time.
  - **Exact** — you enter each person's share.
  - **Percent** — shares as percentages of the total.
- **Balances** — live per-group and overall "you owe" / "you are owed", netted so only the minimum number of payments is shown.
- **Settle up** — record a payment between two people; it reduces what the debtor owes.
- **Chat** — group chat and 1-to-1 direct messages with friends, auto-refreshing every 3 seconds.
- **Security** — CSRF protection via Flask-WTF, passwords stored as salted hashes.

## Tech

- Python 3
- Flask 3.x + Flask-WTF (CSRF)
- SQLite (single file, `sqlite3` from the stdlib)
- Jinja2 templates embedded in the Python file via `jinja2.DictLoader`
- Amounts stored in **paise** (1/100 of a rupee) to avoid floating-point errors; rendered with Indian digit grouping (`12,34,567.89`).

## Run it

```bash
pip3 install -r requirements.txt
python3 base.py
```

Then open <http://127.0.0.1:5050>.

> The app runs in debug mode on `0.0.0.0:5050`. Change `host`/`port`/`debug` in the `if __name__ == "__main__":` block at the bottom of `base.py`.

## Database

On first run `init_db()` creates the schema in `splitwise.db` next to `base.py`.
The database path is overridable with the `DB_PATH` environment variable, e.g.:

```bash
DB_PATH=/tmp/my-splitwise.db python3 base.py
```

**Do not commit `splitwise.db`** — it holds your accounts, expenses, and chat messages. It is in `.gitignore`.

## Project layout

```
base.py            # entire app: schema, routes, embedded Jinja2 templates, JS
requirements.txt   # pinned dependencies
README.md
```

Everything — models, auth, routes, HTML/CSS/JS templates — lives in the single `base.py` file.

## License

MIT