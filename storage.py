"""SQLite persistence for battle history and durable Discord notification retries."""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("battles.db")

COLUMNS = [
    "battle_id", "season_code", "played_at", "saved_at",
    "result", "is_mvp", "character", "partner_character", "enemy_characters",
    "score_before", "score_after", "score_diff",
    "damage", "kills", "deaths", "round_time",
    "raw_list", "raw_detail", "notification_sent",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS battles (
    battle_id         TEXT PRIMARY KEY,
    season_code       TEXT,
    played_at         INTEGER,
    saved_at          INTEGER NOT NULL,
    result            TEXT,
    is_mvp            INTEGER,
    character         TEXT,
    partner_character TEXT,
    enemy_characters  TEXT,
    score_before      INTEGER,
    score_after       INTEGER,
    score_diff        INTEGER,
    damage            INTEGER,
    kills             INTEGER,
    deaths            INTEGER,
    round_time        INTEGER,
    raw_list          TEXT,
    raw_detail        TEXT,
    notification_sent INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_battles_season ON battles(season_code);
CREATE INDEX IF NOT EXISTS idx_battles_played ON battles(played_at);
CREATE INDEX IF NOT EXISTS idx_battles_notification
    ON battles(notification_sent, saved_at);
"""

_conn = None


def init_db():
    """Create the schema and migrate old databases without discarding rows."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = sqlite3.connect(DB_PATH)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    columns = {row["name"] for row in _conn.execute("PRAGMA table_info(battles)")}
    if "notification_sent" not in columns:
        # Existing history must never be broadcast as if it were new.
        _conn.execute(
            "ALTER TABLE battles ADD COLUMN notification_sent INTEGER NOT NULL DEFAULT 1"
        )
    _conn.commit()


def close_db():
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def known_ids():
    """Return all stored battle IDs."""
    return {r[0] for r in _conn.execute("SELECT battle_id FROM battles")}


def insert_battle(row):
    """Insert a battle once. Return True only if a new row was inserted."""
    values = dict(row)
    if values.get("notification_sent") is None:
        values["notification_sent"] = 1
    placeholders = ", ".join(f":{c}" for c in COLUMNS)
    cur = _conn.execute(
        f"INSERT OR IGNORE INTO battles ({', '.join(COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    _conn.commit()
    return cur.rowcount == 1


def pending_notifications(limit=20):
    """Return valid, unsent new battles in oldest-first order."""
    return [
        dict(row)
        for row in _conn.execute(
            """SELECT * FROM battles
               WHERE notification_sent = 0 AND result IS NOT NULL
               ORDER BY saved_at ASC, battle_id ASC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    ]


def mark_notification_sent(battle_id):
    """Mark a battle notified only after Discord confirms the send call."""
    cur = _conn.execute(
        """UPDATE battles SET notification_sent = 1
           WHERE battle_id = ? AND notification_sent = 0 AND result IS NOT NULL""",
        (str(battle_id),),
    )
    _conn.commit()
    return cur.rowcount == 1


def season_for(played_at, season_starts):
    """Find a season from [(start_unix_seconds, code), ...] sorted by start."""
    code = None
    for start, candidate in season_starts:
        if start <= played_at:
            code = candidate
        else:
            break
    return code


def fix_season_codes(season_starts):
    """Recalculate season codes from match timestamps."""
    rows = _conn.execute(
        "SELECT battle_id, played_at, season_code FROM battles WHERE played_at IS NOT NULL"
    ).fetchall()
    fixed = 0
    for row in rows:
        code = season_for(row["played_at"], season_starts)
        if code and code != row["season_code"]:
            _conn.execute(
                "UPDATE battles SET season_code=? WHERE battle_id=?",
                (code, row["battle_id"]),
            )
            fixed += 1
    _conn.commit()
    return fixed


def summary_lines():
    """Return counts and time ranges per season for diagnostics."""
    lines = []
    query = """SELECT season_code, COUNT(*) AS n, MIN(played_at) AS first, MAX(played_at) AS last
               FROM battles GROUP BY season_code ORDER BY season_code"""
    for row in _conn.execute(query):
        def fmt(timestamp):
            return time.strftime("%Y-%m-%d", time.localtime(timestamp)) if timestamp else "不明"
        lines.append(f"   S{row['season_code']}: {row['n']}件 ({fmt(row['first'])} 〜 {fmt(row['last'])})")
    return lines


def count_battles():
    return _conn.execute("SELECT COUNT(*) FROM battles").fetchone()[0]


_TIME_KEYS = ("BattleTime", "EndTime", "StartTime", "CreateTime", "CreatedAt", "Time", "Timestamp")


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_played_at(*sources):
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _TIME_KEYS:
            value = _to_int(source.get(key))
            if value is None:
                continue
            if value > 10**12:
                value //= 1000
            if value > 10**9:
                return value
    return None


def _char_name(player):
    role = (player.get("BattleInfo") or {}).get("Role") or {}
    return role.get("name_jp") or role.get("name") or "不明"


def _name(player):
    return (player.get("UserInfo") or {}).get("Name")


def extract_row(battle_id, detail_json, my_username, list_item=None, season_code=None):
    """Convert a validated battle-detail response to a DB row, retaining raw JSON."""
    data = detail_json.get("data") if isinstance(detail_json, dict) else None
    if not isinstance(data, dict):
        return None

    winners = data.get("WinnerDetails") or []
    losers = data.get("LoserDetails") or []
    if not isinstance(winners, list) or not isinstance(losers, list):
        return None
    if not all(isinstance(player, dict) for player in winners + losers):
        return None

    me = my_team = enemy_team = None
    is_win = False
    for team, enemy, won in ((winners, losers, True), (losers, winners, False)):
        for player in team:
            if _name(player) == my_username:
                me, my_team, enemy_team, is_win = player, team, enemy, won
                break
        if me:
            break

    row = {column: None for column in COLUMNS}
    row.update(
        battle_id=str(battle_id),
        season_code=str(season_code) if season_code is not None else None,
        played_at=_find_played_at(list_item, data),
        saved_at=int(time.time()),
        raw_list=json.dumps(list_item, ensure_ascii=False),
        raw_detail=json.dumps(detail_json, ensure_ascii=False),
        # History imports are silent unless the caller explicitly opts in.
        notification_sent=1,
    )

    if me:
        user_info = me.get("UserInfo") or {}
        battle_info = me.get("BattleInfo") or {}
        before = _to_int(user_info.get("ScoreBefore"), 0)
        after = _to_int(user_info.get("ScoreAfter"), 0)
        row.update(
            result="WIN" if is_win else "LOSS",
            is_mvp=1 if user_info.get("IsMvp") else 0,
            character=_char_name(me),
            partner_character=",".join(_char_name(player) for player in my_team if player is not me),
            enemy_characters=",".join(_char_name(player) for player in enemy_team),
            score_before=before,
            score_after=after,
            score_diff=after - before,
            damage=_to_int(battle_info.get("ExportDamage"), 0),
            kills=_to_int(battle_info.get("BeatCnt"), 0),
            deaths=_to_int(battle_info.get("BeatedCnt"), 0),
            round_time=_to_int(data.get("RoundTime"), 0),
        )
    return row
