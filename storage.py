"""SQLite保存とAPIレスポンスからの戦績抽出。"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("battles.db")

COLUMNS = [
    "battle_id", "season_code", "played_at", "saved_at",
    "result", "is_mvp", "character", "partner_character", "enemy_characters",
    "score_before", "score_after", "score_diff",
    "damage", "kills", "deaths", "round_time",
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
    round_time        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_battles_season ON battles(season_code);
CREATE INDEX IF NOT EXISTS idx_battles_played ON battles(played_at);
"""

_conn = None


def init_db():
    """DBを初期化し、旧バージョンが保存したAPI生データを消去する."""
    global _conn
    _conn = sqlite3.connect(DB_PATH)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    existing_columns = {
        row["name"] for row in _conn.execute("PRAGMA table_info(battles)")
    }
    legacy_raw_columns = {"raw_list", "raw_detail"} & existing_columns
    if legacy_raw_columns:
        _conn.execute("UPDATE battles SET raw_list=NULL, raw_detail=NULL")
    _conn.commit()


def known_ids():
    """保存済み試合IDをsetで返す."""
    return {r[0] for r in _conn.execute("SELECT battle_id FROM battles")}


def insert_battle(row):
    """1行保存する。新規保存できたらTrueを返す."""
    placeholders = ", ".join(f":{column}" for column in COLUMNS)
    cur = _conn.execute(
        f"INSERT OR IGNORE INTO battles ({', '.join(COLUMNS)}) "
        f"VALUES ({placeholders})",
        row,
    )
    _conn.commit()
    return cur.rowcount == 1


def season_for(played_at, season_starts):
    """試合時刻から所属シーズンのコードを返す."""
    code = None
    for start, season_code in season_starts:
        if start <= played_at:
            code = season_code
        else:
            break
    return code


def fix_season_codes(season_starts):
    """保存済み試合のseason_codeを試合時刻から決め直す."""
    rows = _conn.execute(
        "SELECT battle_id, played_at, season_code FROM battles "
        "WHERE played_at IS NOT NULL"
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
    """シーズンごとの件数と期間を返す."""
    lines = []
    query = """SELECT season_code, COUNT(*) AS n,
                      MIN(played_at) AS first, MAX(played_at) AS last
               FROM battles GROUP BY season_code ORDER BY season_code"""
    for row in _conn.execute(query):
        def fmt(timestamp):
            return (
                time.strftime("%Y-%m-%d", time.localtime(timestamp))
                if timestamp else "不明"
            )
        lines.append(
            f"   S{row['season_code']}: {row['n']}件 "
            f"({fmt(row['first'])} 〜 {fmt(row['last'])})"
        )
    return lines


def count_battles():
    return _conn.execute("SELECT COUNT(*) FROM battles").fetchone()[0]


_TIME_KEYS = (
    "BattleTime", "EndTime", "StartTime", "CreateTime",
    "CreatedAt", "Time", "Timestamp",
)


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
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
    if not isinstance(player, dict):
        return "不明"
    battle_info = player.get("BattleInfo")
    battle_info = battle_info if isinstance(battle_info, dict) else {}
    role = battle_info.get("Role")
    role = role if isinstance(role, dict) else {}
    return role.get("name_jp") or role.get("name") or "不明"


def _name(player):
    if not isinstance(player, dict):
        return None
    user_info = player.get("UserInfo")
    return user_info.get("Name") if isinstance(user_info, dict) else None


def extract_row(battle_id, detail_json, my_username, list_item=None, season_code=None):
    """詳細APIからDiscord通知と集計に必要な項目だけを抽出する."""
    data = detail_json.get("data") if isinstance(detail_json, dict) else None
    if not isinstance(data, dict):
        return None

    winners = data.get("WinnerDetails")
    winners = winners if isinstance(winners, list) else []
    losers = data.get("LoserDetails")
    losers = losers if isinstance(losers, list) else []

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
    )

    if me:
        user_info = me.get("UserInfo")
        user_info = user_info if isinstance(user_info, dict) else {}
        battle_info = me.get("BattleInfo")
        battle_info = battle_info if isinstance(battle_info, dict) else {}
        before = _to_int(user_info.get("ScoreBefore"), 0)
        after = _to_int(user_info.get("ScoreAfter"), 0)
        row.update(
            result="WIN" if is_win else "LOSS",
            is_mvp=1 if user_info.get("IsMvp") else 0,
            character=_char_name(me),
            partner_character=",".join(
                _char_name(player) for player in my_team if player is not me
            ),
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
