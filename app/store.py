import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from itertools import groupby


AWARD_NAME = "Десяточка"
AWARD_SLUG = "joined-the-conversation"
AWARD_PATH = f"/achievements/{AWARD_SLUG}"
AWARD_MESSAGE_COUNT = 10
REACTION_AWARD_NAME = "Любимец публики"
REACTION_AWARD_SLUG = "public-favorite"
REACTION_AWARD_PATH = f"/achievements/{REACTION_AWARD_SLUG}"
REACTION_AWARD_COUNT = 4
ELEPHANT_AWARD_NAME = "Наш слон"
ELEPHANT_AWARD_SLUG = "our-elephant"
ELEPHANT_AWARD_PATH = f"/achievements/{ELEPHANT_AWARD_SLUG}"
ELEPHANT_AWARD_LETTER_COUNT = 10
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class SilenceTier:
    days: int
    name: str
    slug: str
    description: str

    @property
    def path(self):
        return f"/achievements/{self.slug}"


SILENCE_TIERS = (
    SilenceTier(1, "Ща придумаю", "not-in-touch",
                "Сутки думал, что написать в чат. Не придумал."),
)
SILENCE_BY_SLUG = {tier.slug: (number, tier) for number, tier in enumerate(SILENCE_TIERS, start=1)}


@dataclass(frozen=True)
class AchievementSeries:
    name: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class SeriesPosition:
    series: AchievementSeries
    number: int

    @property
    def label(self):
        if len(self.series.steps) == 1:
            return self.series.name
        return f"{self.series.name} · {self.number} из {len(self.series.steps)}"


ACHIEVEMENT_SERIES: tuple[AchievementSeries, ...] = ()
SERIES_BY_SLUG = {
    slug: SeriesPosition(series, number)
    for series in ACHIEVEMENT_SERIES
    for number, slug in enumerate(series.steps, start=1)
}


@dataclass(frozen=True)
class AwardBatch:
    name: str
    path: str
    user_ids: list[int]
    series_position: SeriesPosition | None = None


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
                CREATE TABLE IF NOT EXISTS message_reactions (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, reactor_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id, reactor_id)
                );
                CREATE TABLE IF NOT EXISTS reaction_awards (
                    user_id INTEGER PRIMARY KEY, day TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS elephant_letter_counts (
                    user_id INTEGER PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS elephant_awards (
                    user_id INTEGER PRIMARY KEY, day TEXT NOT NULL
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

    def record_message(self, chat_id, message_id, user_id, sent_at, *, count_for_message_award=True,
                       telegram_message=None):
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
            text = telegram_message.get("text") if telegram_message else None
            text_letter_count = sum(text.count(letter) for letter in "zZvV") if isinstance(text, str) else 0
            if text_letter_count:
                db.execute("""
                    INSERT INTO elephant_letter_counts VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE
                    SET count = elephant_letter_counts.count + excluded.count
                """, (user_id, text_letter_count))
            db.execute("""
                INSERT INTO last_activity VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET sent_at = excluded.sent_at
                WHERE excluded.sent_at > last_activity.sent_at
            """, (user_id, sent_at))
            return True

    def record_reaction(self, chat_id, message_id, reactor_id, reacted_at):
        with self.transaction() as db:
            result = db.execute("""
                INSERT OR IGNORE INTO message_reactions
                SELECT message.chat_id, message.message_id, :reactor
                FROM messages AS message
                WHERE message.chat_id = :chat AND message.message_id = :message
                  AND message.user_id != :reactor AND message.sent_at <= :time
                  AND (SELECT enabled FROM consent
                       WHERE user_id = :reactor AND changed_at <= :time
                       ORDER BY changed_at DESC LIMIT 1) = 1
                  AND (SELECT enabled FROM consent
                       WHERE user_id = message.user_id AND changed_at <= :time
                       ORDER BY changed_at DESC LIMIT 1) = 1
            """, {"chat": chat_id, "message": message_id, "reactor": reactor_id, "time": reacted_at})
            return result.rowcount == 1

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
            elephant = db.execute("""
                INSERT OR IGNORE INTO elephant_awards
                SELECT counts.user_id, ?
                FROM elephant_letter_counts AS counts
                WHERE counts.count >= ?
                  AND (SELECT enabled FROM consent WHERE user_id = counts.user_id
                       ORDER BY changed_at DESC LIMIT 1) = 1
            """, (local_date, ELEPHANT_AWARD_LETTER_COUNT))
            awarded += elephant.rowcount
            reactions = db.execute("""
                INSERT OR IGNORE INTO reaction_awards
                SELECT DISTINCT message.user_id, ?
                FROM (
                    SELECT chat_id, message_id FROM message_reactions
                    GROUP BY chat_id, message_id HAVING count(*) >= ?
                ) AS qualified
                JOIN messages AS message USING (chat_id, message_id)
                WHERE (SELECT enabled FROM consent WHERE user_id = message.user_id
                       ORDER BY changed_at DESC LIMIT 1) = 1
            """, (local_date, REACTION_AWARD_COUNT))
            awarded += reactions.rowcount
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
            return db.execute("""
                SELECT tier, day FROM silence_awards
                WHERE user_id = ? AND tier BETWEEN 1 AND ?
                ORDER BY tier
            """, (user_id, len(SILENCE_TIERS))).fetchall()

    def reaction_award_day(self, user_id):
        with self.transaction() as db:
            row = db.execute("SELECT day FROM reaction_awards WHERE user_id = ?", (user_id,)).fetchone()
            return row[0] if row else None

    def elephant_award_day(self, user_id):
        with self.transaction() as db:
            row = db.execute("SELECT day FROM elephant_awards WHERE user_id = ?", (user_id,)).fetchone()
            return row[0] if row else None

    def achievement_holders(self, slug):
        with self.transaction() as db:
            if slug in SILENCE_BY_SLUG:
                tier, _ = SILENCE_BY_SLUG[slug]
                return db.execute(
                    "SELECT user_id, day FROM silence_awards WHERE tier = ? ORDER BY user_id",
                    (tier,),
                ).fetchall()
            if slug == REACTION_AWARD_SLUG:
                return db.execute(
                    "SELECT user_id, day FROM reaction_awards ORDER BY user_id",
                ).fetchall()
            if slug == ELEPHANT_AWARD_SLUG:
                return db.execute(
                    "SELECT user_id, day FROM elephant_awards ORDER BY user_id",
                ).fetchall()
            if slug == AWARD_SLUG:
                return db.execute(
                    "SELECT user_id, day FROM awards ORDER BY user_id",
                ).fetchall()
            return []

    def achievement_discovered(self, slug=AWARD_SLUG):
        with self.transaction() as db:
            if slug in SILENCE_BY_SLUG:
                number, _ = SILENCE_BY_SLUG[slug]
                return db.execute("SELECT 1 FROM silence_awards WHERE tier = ? LIMIT 1",
                                  (number,)).fetchone() is not None
            if slug == REACTION_AWARD_SLUG:
                return db.execute("SELECT 1 FROM reaction_awards LIMIT 1").fetchone() is not None
            if slug == ELEPHANT_AWARD_SLUG:
                return db.execute("SELECT 1 FROM elephant_awards LIMIT 1").fetchone() is not None
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
            reaction_users = [user_id for user_id, in db.execute(
                "SELECT user_id FROM reaction_awards WHERE day = ? ORDER BY user_id", (day,),
            )]
            if reaction_users:
                groups.append(AwardBatch(REACTION_AWARD_NAME, REACTION_AWARD_PATH, reaction_users))
            elephant_users = [user_id for user_id, in db.execute(
                "SELECT user_id FROM elephant_awards WHERE day = ? ORDER BY user_id", (day,),
            )]
            if elephant_users:
                groups.append(AwardBatch(ELEPHANT_AWARD_NAME, ELEPHANT_AWARD_PATH, elephant_users))
            tiers = db.execute("""
                SELECT tier, user_id FROM silence_awards
                WHERE day = ? AND tier BETWEEN 1 AND ?
                ORDER BY tier, user_id
            """, (day, len(SILENCE_TIERS)))
            for tier, members in groupby(tiers, key=lambda row: row[0]):
                achievement = SILENCE_TIERS[tier - 1]
                groups.append(AwardBatch(achievement.name, achievement.path, [user_id for _, user_id in members],
                                         SERIES_BY_SLUG.get(achievement.slug)))
            return day, groups

    def mark_digest_sent(self, local_date):
        with self.transaction() as db:
            db.execute("UPDATE daily SET sent = 1 WHERE day = ?", (local_date,))
