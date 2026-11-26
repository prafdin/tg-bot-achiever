# Achievement Holders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Show the names of participants who earned an achievement on its detail page, with each name linking to that participant's profile.

**Architecture:** Query existing award history in SQLite by achievement slug; map participant IDs to configured display names and profile URLs in the web layer; render an alphabetically sorted list in the existing authenticated achievement page.

**Tech Stack:** Python, SQLite, Starlette, Jinja2.

**Spec:** [Achievement holders design](../specs/2026-11-26-achievement-holders-design.md)

## Global Constraints

- Achievement detail pages remain behind the current login and achievement-discovery checks.
- Use configured participant labels and the existing profile routes.
- Show names only; omit dates, counts, rankings, and achievements not yet discovered.

## Review Focus

- Each achievement type (message, reaction, silence tier) uses its own existing award history table.
- A participant's own link targets `/`; another participant's link targets `/participants/{id}`.
- Missing configured labels use the existing `Участник {id}` fallback.
- Empty or undiscovered achievements do not leak a catalogue or holder list.
- Holder names sort alphabetically using case-insensitive labels.

---

### Task 1: Load and render achievement holders

**Files:**
- Modify: `app/store.py`
- Modify: `app/web.py`
- Modify: `app/templates/achievement.html`
- Modify: `PRODUCT_CONCEPT.md`

**Interfaces:**
- `Store.achievement_holders(slug)` returns ordered `(user_id, day)` pairs for that achievement from the existing award table; unsupported slugs return an empty list.
- The web layer passes `holders`, a list of `{name, path}` records, to the template after the existing access and discovery checks.

- [x] Add a `Store.achievement_holders(slug)` query for the existing message, reaction, and silence award tables.
- [x] Map each holder to the configured label and the current user's or participant's existing profile URL; sort by case-folded label.
- [x] Render a “Уже получили ачивку” section containing only linked names, and retain the existing achievement-page access rules.
- [x] Review the diff for every achievement slug and the three profile URL cases (self, other, missing label).
