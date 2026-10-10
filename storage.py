"""戦績のSQLite保存と、APIレスポンスから保存用の1行を取り出す処理。"""
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
    "raw_list", "raw_detail",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS battles (
    battle_id         TEXT PRIMARY KEY,
    season_code       TEXT,
    played_at         INTEGER,   -- 試合時刻(unix秒)。APIから見つからなければNULL
    saved_at          INTEGER NOT NULL,
    result            TEXT,      -- WIN / LOSS / NULL(自分が試合データに見つからなかった)
    is_mvp            INTEGER,
    character         TEXT,
    partner_character TEXT,
    enemy_characters  TEXT,      -- "キャラA,キャラB"
    score_before      INTEGER,
    score_after       INTEGER,
    score_diff        INTEGER,
    damage            INTEGER,
    kills             INTEGER,
    deaths            INTEGER,
    round_time        INTEGER,
    raw_list          TEXT,      -- 一覧APIの1件分(生JSON)
    raw_detail        TEXT,      -- 詳細APIのレスポンス全体(生JSON)
    notification_sent INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_battles_season ON battles(season_code);
CREATE INDEX IF NOT EXISTS idx_battles_played ON battles(played_at);
"""

_conn = None


def init_db():
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = sqlite3.connect(DB_PATH)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    columns = {r[1] for r in _conn.execute("PRAGMA table_info(battles)")}
    if "notification_sent" not in columns:
        # Existing rows are historical records and must never trigger a mass notification.
        _conn.execute("ALTER TABLE battles ADD COLUMN notification_sent INTEGER NOT NULL DEFAULT 1")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_battles_notification ON battles(notification_sent, result)")
    _conn.commit()


def known_ids():
    """保存済みの試合IDをsetで返す"""
    return {r[0] for r in _conn.execute("SELECT battle_id FROM battles")}


def insert_battle(row, notification_pending=False):
    """1行保存する。新規に保存できたらTrue、既にあればFalse"""
    placeholders = ", ".join(f":{c}" for c in COLUMNS)
    cur = _conn.execute(
        f"INSERT OR IGNORE INTO battles ({', '.join(COLUMNS)}, notification_sent) VALUES ({placeholders}, :notification_sent)",
        {**{c: row.get(c) for c in COLUMNS}, "notification_sent": 0 if notification_pending and row.get("result") is not None else 1},
    )
    _conn.commit()
    return cur.rowcount == 1


def pending_notifications(limit=20):
    return [dict(r) for r in _conn.execute(
        "SELECT * FROM battles WHERE notification_sent=0 AND result IS NOT NULL ORDER BY saved_at, battle_id LIMIT ?", (limit,)
    )]


def mark_notification_sent(battle_id):
    with _conn:
        cur = _conn.execute("UPDATE battles SET notification_sent=1 WHERE battle_id=? AND notification_sent=0", (str(battle_id),))
    return cur.rowcount == 1


def season_for(played_at, season_starts):
    """試合時刻から所属シーズンのコードを返す。season_startsは[(開始unix秒, コード)]を開始順に並べたもの"""
    code = None
    for start, c in season_starts:
        if start <= played_at:
            code = c
        else:
            break
    return code


def fix_season_codes(season_starts):
    """保存済みの試合のseason_codeを、試合時刻から決め直す。直した件数を返す"""
    rows = _conn.execute("SELECT battle_id, played_at, season_code FROM battles WHERE played_at IS NOT NULL").fetchall()
    fixed = 0
    for r in rows:
        code = season_for(r["played_at"], season_starts)
        if code and code != r["season_code"]:
            _conn.execute("UPDATE battles SET season_code=? WHERE battle_id=?", (code, r["battle_id"]))
            fixed += 1
    _conn.commit()
    return fixed


def summary_lines():
    """シーズンごとの件数と期間(確認用)"""
    lines = []
    q = """SELECT season_code, COUNT(*) AS n, MIN(played_at) AS first, MAX(played_at) AS last
           FROM battles GROUP BY season_code ORDER BY season_code"""
    for r in _conn.execute(q):
        def fmt(t):
            return time.strftime("%Y-%m-%d", time.localtime(t)) if t else "不明"
        lines.append(f"   S{r['season_code']}: {r['n']}件 ({fmt(r['first'])} 〜 {fmt(r['last'])})")
    return lines


def count_battles():
    return _conn.execute("SELECT COUNT(*) FROM battles").fetchone()[0]


# ---- APIレスポンス → 保存用の行 ----------------------------------------

# 試合時刻っぽいキー(実際のレスポンスを見て確定させる)
_TIME_KEYS = ("BattleTime", "EndTime", "StartTime", "CreateTime", "CreatedAt", "Time", "Timestamp")


def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _find_played_at(*sources):
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in _TIME_KEYS:
            v = _to_int(src.get(key))
            if v is None:
                continue
            if v > 10**12:  # ミリ秒なら秒に直す
                v //= 1000
            if v > 10**9:
                return v
    return None


def _char_name(player):
    role = (player.get("BattleInfo") or {}).get("Role") or {}
    return role.get("name_jp") or role.get("name") or "不明"


def _name(player):
    return (player.get("UserInfo") or {}).get("Name")


def extract_row(battle_id, detail_json, my_username, list_item=None, season_code=None):
    """詳細APIのレスポンスから保存用の1行(dict)を作る。

    自分が見つからなくても生JSONは残す(result=NULL)。
    レスポンスの形が想定外ならNone。
    """
    data = detail_json.get("data") if isinstance(detail_json, dict) else None
    if (not isinstance(data, dict)
            or not isinstance(data.get("WinnerDetails"), list)
            or not isinstance(data.get("LoserDetails"), list)):
        return None

    winners = data.get("WinnerDetails") or []
    losers = data.get("LoserDetails") or []

    me = my_team = enemy_team = None
    is_win = False
    for team, enemy, win in ((winners, losers, True), (losers, winners, False)):
        for p in team:
            if _name(p) == my_username:
                me, my_team, enemy_team, is_win = p, team, enemy, win
                break
        if me:
            break

    row = {c: None for c in COLUMNS}
    row.update(
        battle_id=str(battle_id),
        season_code=str(season_code) if season_code is not None else None,
        played_at=_find_played_at(list_item, data),
        saved_at=int(time.time()),
        raw_list=json.dumps(list_item, ensure_ascii=False),
        raw_detail=json.dumps(detail_json, ensure_ascii=False),
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
            partner_character=",".join(_char_name(p) for p in my_team if p is not me),
            enemy_characters=",".join(_char_name(p) for p in enemy_team),
            score_before=before,
            score_after=after,
            score_diff=after - before,
            damage=_to_int(battle_info.get("ExportDamage"), 0),
            kills=_to_int(battle_info.get("BeatCnt"), 0),
            deaths=_to_int(battle_info.get("BeatedCnt"), 0),
            round_time=_to_int(data.get("RoundTime"), 0),
        )
    return row
