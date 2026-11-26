import sqlite3
from contextlib import closing, contextmanager


AWARD_NAME = "Влился в беседу"
AWARD_SLUG = "joined-the-conversation"
AWARD_PATH = f"/achievements/{AWARD_SLUG}"
AWARD_MESSAGE_COUNT = 10


class Store:
    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS consent (
                    user_id INTEGER, changed_at REAL, enabled INTEGER,
                    PRIMARY KEY (user_id, changed_at)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER, message_id INTEGER, user_id INTEGER, sent_at INTEGER,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS awards (
                    user_id INTEGER PRIMARY KEY, day TEXT
                );
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def set_consent(self, user_id, enabled, changed_at):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO consent VALUES (?, ?, ?)",
                       (user_id, changed_at, enabled))

    def record_message(self, chat_id, message_id, user_id, sent_at):
        with self.transaction() as db:
            result = db.execute("""
                INSERT OR IGNORE INTO messages
                SELECT ?, ?, ?, ?
                WHERE (SELECT enabled FROM consent WHERE user_id = ? AND changed_at <= ?
                       ORDER BY changed_at DESC LIMIT 1) = 1
            """, (chat_id, message_id, user_id, sent_at, user_id, sent_at))
            return result.rowcount == 1

    def award_daily(self, local_date):
        with self.transaction() as db:
            if not db.execute("INSERT OR IGNORE INTO daily(day) VALUES (?)",
                              (local_date,)).rowcount:
                return 0
            result = db.execute("""
                INSERT OR IGNORE INTO awards
                SELECT messages.user_id, ?
                FROM messages
                GROUP BY messages.user_id
                HAVING count(*) >= ?
                   AND (
                       SELECT enabled FROM consent
                       WHERE consent.user_id = messages.user_id
                       ORDER BY changed_at DESC LIMIT 1
                   ) = 1
            """, (local_date, AWARD_MESSAGE_COUNT))
            if not result.rowcount:
                db.execute("UPDATE daily SET sent = 1 WHERE day = ?", (local_date,))
            return result.rowcount

    def profile(self, user_id):
        with self.transaction() as db:
            consent = db.execute("SELECT enabled FROM consent WHERE user_id = ? "
                                 "ORDER BY changed_at DESC LIMIT 1", (user_id,)).fetchone()
            award = db.execute("SELECT day FROM awards WHERE user_id = ?", (user_id,)).fetchone()
            return bool(consent and consent[0]), award[0] if award else None

    def achievement_discovered(self):
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM awards LIMIT 1").fetchone() is not None

    def pending_digest(self):
        with self.transaction() as db:
            row = db.execute("SELECT day FROM daily WHERE sent = 0 ORDER BY day LIMIT 1").fetchone()
            if row is None:
                return None
            day = row[0]
            users = db.execute("SELECT user_id FROM awards WHERE day = ? ORDER BY user_id", (day,))
            return day, [user_id for user_id, in users]

    def mark_digest_sent(self, local_date):
        with self.transaction() as db:
            db.execute("UPDATE daily SET sent = 1 WHERE day = ?", (local_date,))
