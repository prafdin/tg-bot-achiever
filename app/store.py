import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from itertools import groupby


AWARD_NAME = "Влился в беседу"
AWARD_SLUG = "joined-the-conversation"
AWARD_PATH = f"/achievements/{AWARD_SLUG}"
AWARD_MESSAGE_COUNT = 10
SILENCE_NAME = "Числится живым"
SILENCE_SLUG = "listed-as-alive"
SILENCE_PATH = f"/achievements/{SILENCE_SLUG}"
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class SilenceTier:
    days: int
    name: str
    description: str


SILENCE_TIERS = (
    SilenceTier(1, "Не вышел на связь", "Сутки без сообщений. Дежурный пока просит не волноваться."),
    SilenceTier(7, "Объявлен в розыск", "Неделя тишины. Свидетели сходятся на том, что когда-то ты здесь писал."),
    SilenceTier(14, "Поиски приостановлены", "Две недели без связи. Признаков переписки не обнаружено."),
    SilenceTier(30, "Дело сдано в архив", "Месяц тишины. Если появишься, оформим как новое обстоятельство."),
)


@dataclass(frozen=True)
class AwardBatch:
    name: str
    path: str
    user_ids: list[int]
    tier_name: str | None = None


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
                CREATE TABLE IF NOT EXISTS last_activity (
                    user_id INTEGER PRIMARY KEY, sent_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS silence_awards (
                    user_id INTEGER NOT NULL, tier INTEGER NOT NULL, day TEXT NOT NULL,
                    PRIMARY KEY (user_id, tier)
                );
            """)
            # Include regular messages recorded by older versions, even after a rollback.
            db.execute("""
                INSERT INTO last_activity
                SELECT user_id, max(sent_at) FROM messages
                WHERE message_id > 0 GROUP BY user_id
                ON CONFLICT(user_id) DO UPDATE SET sent_at = excluded.sent_at
                WHERE excluded.sent_at > last_activity.sent_at
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def set_consent(self, user_id, enabled, changed_at):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO consent VALUES (?, ?, ?)",
                       (user_id, changed_at, enabled))

    def record_message(self, chat_id, message_id, user_id, sent_at, *, count_for_message_award=True):
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            consent = db.execute("""
                SELECT enabled FROM consent WHERE user_id = ? AND changed_at <= ?
                ORDER BY changed_at DESC LIMIT 1
            """, (user_id, sent_at)).fetchone()
            if consent != (1,):
                return False
            if count_for_message_award and not db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (chat_id, message_id, user_id, sent_at),
            ).rowcount:
                return False
            db.execute("""
                INSERT INTO last_activity VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET sent_at = excluded.sent_at
                WHERE excluded.sent_at > last_activity.sent_at
            """, (user_id, sent_at))
            return True

    def award_daily(self, local_date, checked_at=None):
        if checked_at is None:
            checked_at = time.time()
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
            awarded = result.rowcount + self._award_silence(db, local_date, checked_at)
            if not awarded:
                db.execute("UPDATE daily SET sent = 1 WHERE day = ?", (local_date,))
            return awarded

    @staticmethod
    def _award_silence(db, local_date, checked_at):
        candidates = db.execute("""
            SELECT activity.user_id, activity.sent_at,
                   (SELECT min(started.changed_at) FROM consent AS started
                    WHERE started.user_id = activity.user_id AND started.enabled = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM consent AS stopped
                          WHERE stopped.user_id = started.user_id AND stopped.enabled = 0
                            AND stopped.changed_at >= started.changed_at
                      )),
                   COALESCE((SELECT max(tier) FROM silence_awards
                             WHERE user_id = activity.user_id), 0)
            FROM last_activity AS activity
        """)
        awards = [
            (user_id, current_tier + 1, local_date)
            for user_id, last_message, consent_since, current_tier in candidates
            if consent_since is not None and current_tier < len(SILENCE_TIERS)
            and checked_at - max(last_message, consent_since) >= SILENCE_TIERS[current_tier].days * SECONDS_PER_DAY
        ]
        db.executemany("INSERT INTO silence_awards VALUES (?, ?, ?)", awards)
        return len(awards)

    def profile(self, user_id):
        with self.transaction() as db:
            consent = db.execute("SELECT enabled FROM consent WHERE user_id = ? "
                                 "ORDER BY changed_at DESC LIMIT 1", (user_id,)).fetchone()
            award = db.execute("SELECT day FROM awards WHERE user_id = ?", (user_id,)).fetchone()
            return bool(consent and consent[0]), award[0] if award else None

    def silence_history(self, user_id):
        with self.transaction() as db:
            return db.execute("SELECT tier, day FROM silence_awards WHERE user_id = ? ORDER BY tier",
                              (user_id,)).fetchall()

    def achievement_discovered(self, slug=AWARD_SLUG):
        with self.transaction() as db:
            if slug == SILENCE_SLUG:
                return db.execute("SELECT 1 FROM silence_awards LIMIT 1").fetchone() is not None
            return slug == AWARD_SLUG and db.execute("SELECT 1 FROM awards LIMIT 1").fetchone() is not None

    def pending_digest(self):
        with self.transaction() as db:
            row = db.execute("SELECT day FROM daily WHERE sent = 0 ORDER BY day LIMIT 1").fetchone()
            if row is None:
                return None
            day = row[0]
            users = [user_id for user_id, in db.execute(
                "SELECT user_id FROM awards WHERE day = ? ORDER BY user_id", (day,),
            )]
            groups = [AwardBatch(AWARD_NAME, AWARD_PATH, users)] if users else []
            tiers = db.execute("SELECT tier, user_id FROM silence_awards WHERE day = ? ORDER BY tier, user_id", (day,))
            for tier, members in groupby(tiers, key=lambda row: row[0]):
                groups.append(AwardBatch(SILENCE_NAME, SILENCE_PATH,
                                         [user_id for _, user_id in members], SILENCE_TIERS[tier - 1].name))
            return day, groups

    def mark_digest_sent(self, local_date):
        with self.transaction() as db:
            db.execute("UPDATE daily SET sent = 1 WHERE day = ?", (local_date,))
