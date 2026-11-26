# «Наш слон» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Award a permanent «Наш слон» achievement after a participant sends text messages containing at least ten Latin `z` or `v` letters in total.

**Architecture:** Count exact characters transiently in incoming Telegram `text` fields and update one cumulative SQLite counter per consenting participant. The daily award flow, digest, profile history, achievement page, and data policy will expose the new achievement without storing message text.

**Tech Stack:** Python, SQLite, Starlette, Jinja2, Telegram Bot API.

**Spec:** [Our Elephant design](../specs/2026-11-26-our-elephant-design.md)

## Global Constraints

- Count only ordinary text (`message.text`), not media captions.
- Count `z`, `Z`, `v`, and `V`; replies count.
- Inspect/count text only after verifying active observation and the author's collection consent; for a text reply, the sender's consent is sufficient.
- Store only the cumulative per-user total, never the source text.
- The achievement is permanent, one-time, and awarded during the daily check if consent remains enabled.

## Review Focus

- Text with zero target letters changes no counter; all four target character variants count once each.
- Captions and non-text messages do not count; text replies do.
- Consent off, paused collection, wrong chat, and pre-observation messages do not increment the total.
- Totals survive restarts and opt-out periods; a currently disabled user is not awarded.
- The new award appears once in profile history, the achievement detail page, and the digest.
- Before the first award, the public data policy and profiles explain generic character-count storage without exposing this achievement's name or exact rule.

---

### Task 1: Count letters and award the achievement

**Files:**
- Modify: `app/bot.py`
- Modify: `app/store.py`
- Modify: `app/web.py`
- Modify: `app/templates/profile.html`
- Modify: `app/templates/achievement.html`
- Modify: `app/templates/data_policy.html`
- Modify: `PRODUCT_CONCEPT.md`

**Interfaces:**
- `Store.record_message(..., telegram_message=None)` reads and counts `telegram_message["text"]` only after the existing consent check succeeds.
- `Store.elephant_award_day(user_id)` returns the award day or `None`.
- `Store.achievement_holders("our-elephant")` returns existing award holders; `achievement_discovered("our-elephant")` follows the established discovery rule.

- [x] Pass the transient Telegram message to the store and count only the four agreed characters in `message.text` after the consent check; never persist the text.
- [x] Add an aggregate counter and one-time award table; include the threshold and current-consent check in `award_daily`.
- [x] Include the award in pending daily digests, profiles, holder lists, and its own authenticated detail page with the agreed title, description, and criterion.
- [x] Update the data policy to explain transient counting, the stored aggregate, and exclusions.
- [x] Review the changed code paths against every Review Focus item; do not run or add tests unless requested.
