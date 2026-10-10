import time
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv

import storage

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
try:
    CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", ""))
    if CHANNEL_ID <= 0:
        raise ValueError
except ValueError:
    raise RuntimeError("DISCORD_CHANNEL_ID は正の整数で設定してください")
MY_USERNAME = os.getenv("STARWARD_USERNAME")
ROLE_ID = os.getenv("STARWARD_ROLE_ID")

API_LIST_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/battle_list"
API_DETAIL_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/battle_detail2v2"
API_SUMMARY_URL = f"https://xzy.shengtiangames.com/mini-game/xzy/battle-record/battle_summary?role_id={ROLE_ID}"
API_SEASON_LIST_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/season_list"

API_TOKEN = os.getenv("STARWARD_API_TOKEN")
COOKIE = os.getenv("STARWARD_COOKIE")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Cookie": COOKIE,
    "token": API_TOKEN,
    "game-code": "XZYJP",
    "lang": "ja-jp",
    "origin": "https://xzyjp.shengtiangames.com",
    "referer": "https://xzyjp.shengtiangames.com/",
}

required_env = {
    "DISCORD_BOT_TOKEN": BOT_TOKEN,
    "STARWARD_USERNAME": MY_USERNAME,
    "STARWARD_ROLE_ID": ROLE_ID,
    "STARWARD_API_TOKEN": API_TOKEN,
    "STARWARD_COOKIE": COOKIE,
}
for key in ("STARWARD_ALLOWED_USER_IDS", "STARWARD_ALLOWED_ROLE_IDS"):
    raw_ids = os.getenv(key, "")
    try:
        parsed = {int(value.strip()) for value in raw_ids.split(",") if value.strip()}
        if any(value <= 0 for value in parsed):
            raise ValueError
    except ValueError:
        raise RuntimeError(f"{key} は正のIDをカンマ区切りで指定してください")


missing = [key for key, value in required_env.items() if not value]
if missing:
    raise RuntimeError(f"必要な環境変数が設定されていません: {', '.join(missing)}")

ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("STARWARD_ALLOWED_USER_IDS", "").split(",") if x.strip()}
ALLOWED_ROLE_IDS = {int(x.strip()) for x in os.getenv("STARWARD_ALLOWED_ROLE_IDS", "").split(",") if x.strip()}
if not ALLOWED_USER_IDS and not ALLOWED_ROLE_IDS:
    print("!season の許可IDが未設定のため、Bot管理者のみ利用できます")

SYNC_SEASONS = int(os.getenv("SYNC_SEASONS", "3"))  # 起動時に遡るシーズン数
DETAIL_INTERVAL = 0.5  # 試合詳細APIを連続で叩くときの間隔(秒)
MAX_PAGES = 200  # 一覧のページ送りの上限(暴走防止)

http_session = None
startup_done = False
SEASON_STARTS = []  # [(開始unix秒, シーズンコード)] 開始順。試合時刻→シーズン判定に使う
_keys_logged = False
_failure_counts = {}

def log_failure(key, message):
    count = _failure_counts.get(key, 0) + 1
    _failure_counts[key] = count
    if count == 1 or count in (5, 20) or count % 100 == 0:
        print(f"⚠️ {message} (連続失敗 {count} 回)")

def clear_failure(key):
    _failure_counts.pop(key, None)

class StarwardBot(commands.Bot):
    """aiohttpのセッションライフサイクルを安全に管理するBotクラス"""
    async def setup_hook(self):
        global http_session
        http_session = aiohttp.ClientSession()

    async def close(self):
        global http_session
        if http_session and not http_session.closed:
            await http_session.close()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True
bot = StarwardBot(command_prefix="!", intents=intents)

async def get_json(url):
    """APIからJSONを非同期で取得する"""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with http_session.get(url, headers=HEADERS, timeout=timeout) as response:
            if response.status == 200:
                return await response.json()
            print(f"⚠️ API HTTPエラー: {response.status} ({url})")
    except Exception as e:
        log_failure("json", f"API取得に失敗しました: {type(e).__name__}")
    return None

def update_season_starts(started):
    """開始済みシーズンの一覧からSEASON_STARTSを更新する"""
    SEASON_STARTS[:] = sorted(
        (int(s.get("StartTime", 0)), str(s.get("Code"))) for s in started
    )

async def get_latest_season_info():
    """シーズン一覧を取得し、現在開催中の最新シーズンCodeを返す"""
    try:
        res = await get_json(API_SEASON_LIST_URL)
        if not api_ok(res) or not isinstance(res.get("data"), list) or not res["data"]:
            raise ValueError("season_list の応答形式または code が不正")
        seasons = res["data"]
        normalized = []
        for season in seasons:
            if not isinstance(season, dict) or season.get("Code") is None or season.get("StartTime") is None:
                raise ValueError("season_list のシーズン項目が不完全")
            normalized.append((int(season["StartTime"]), str(season["Code"])))
        started = sorted(x for x in normalized if x[0] <= int(time.time()))
        if not started:
            raise ValueError("開始済みシーズンがありません")
        SEASON_STARTS[:] = started
        clear_failure("season")
        return started[-1][1]
    except Exception as e:
        log_failure("season", f"シーズン一覧取得失敗: {type(e).__name__}")
        return None

def api_ok(res):
    """APIの応答が成功か(codeが無い or 0)"""
    return isinstance(res, dict) and res.get("code") == 0

async def fetch_battle_list(season_code, time_start, time_end, page=1):
    """2v2の戦績一覧を1ページ取得する(新しい順)。失敗はNone、試合なしは[]"""
    params = {
        "season_code": season_code,
        "match_type": "22",
        "time_start": time_start,
        "time_end": time_end,
        "page": page,
        "role_id": ROLE_ID,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with http_session.get(API_LIST_URL, headers=HEADERS, params=params, timeout=timeout) as response:
            if response.status != 200:
                body = await response.text()
                print(f"⚠️ API HTTPエラー: {response.status} ({response.url})\nレスポンス: {body[:500]}")
                return None
            res = await response.json()
    except Exception as e:
        log_failure("battle_list", f"戦績一覧の取得に失敗しました ({type(e).__name__})")
        return None

    return parse_battle_list_response(res)

def parse_battle_list_response(res):
    """Validated successful empty lists remain distinct from failures (None)."""
    if not api_ok(res):
        log_failure("battle_list", "戦績一覧APIが失敗を返しました")
        return None
    items = res.get("data")
    if not isinstance(items, list):
        log_failure("battle_list", "戦績一覧の data 形式が不正")
        return None
    clear_failure("battle_list")
    return items

async def get_battle_detail(battle_id):
    """指定した試合IDの完全な詳細データを取得する"""
    url = f"{API_DETAIL_URL}?id={battle_id}"
    return await get_json(url)

async def ingest_items(items, season_code, notification_eligible=False):
    """一覧のうち未保存の試合だけ詳細を取って保存する(古い順)。保存できた行のリストを返す"""
    global _keys_logged
    known = storage.known_ids()
    todo, seen = [], set()
    for item in reversed(items):  # 一覧は新しい順なので反転して古い順に
        bid = item.get("BattleId")
        if bid is None:
            continue
        bid = str(bid)
        if bid in known or bid in seen:
            continue
        seen.add(bid)
        todo.append((bid, item))

    saved = []
    for i, (bid, item) in enumerate(todo, 1):
        detail = await get_battle_detail(bid)
        if not api_ok(detail):
            print(f"⚠️ 試合詳細の取得に失敗(次回リトライします): {bid}")
            continue

        if not _keys_logged:  # 初回だけ項目名を出す(試合時刻のキー確認用)
            _keys_logged = True
            print(f"📋 一覧の項目: {list(item.keys())}")
            data = detail.get("data")
            if isinstance(data, dict):
                print(f"📋 詳細の項目: {list(data.keys())}")

        row = storage.extract_row(bid, detail, MY_USERNAME, item, season_code)
        if row is None:
            print(f"⚠️ 想定外の詳細データ形式のためスキップ: {bid}")
            continue
        if row["played_at"] and SEASON_STARTS:  # APIのseason_codeは当てにせず、試合時刻から決める
            row["season_code"] = storage.season_for(row["played_at"], SEASON_STARTS) or row["season_code"]
        if row["result"] is None:
            print(f"⚠️ 試合 {bid} に自分({MY_USERNAME})が見つかりません。生データのみ保存します")
        if storage.insert_battle(row, notification_pending=notification_eligible and row["result"] is not None):
            saved.append(row)

        if len(todo) > 1:
            if i % 25 == 0:
                print(f"   ...{i}/{len(todo)}件 処理済み")
            await asyncio.sleep(DETAIL_INTERVAL)
    return saved

async def sync_history():
    """起動時に、直近 SYNC_SEASONS シーズン分の未保存試合をまとめて保存する"""
    res = await get_json(API_SEASON_LIST_URL)
    if not api_ok(res) or not isinstance(res.get("data"), list) or not res["data"]:
        log_failure("season", "履歴補完を中断: シーズン一覧の取得または形式検証に失敗")
        return
    seasons = res["data"]

    now = int(time.time())
    try:
        if any(not isinstance(s, dict) or s.get("Code") is None or s.get("StartTime") is None for s in seasons):
            raise ValueError
        started = sorted((s for s in seasons if int(s["StartTime"]) <= now), key=lambda s: int(s["StartTime"]))
    except (TypeError, ValueError):
        log_failure("season", "履歴補完を中断: シーズン情報が不完全")
        return
    if not started:
        log_failure("season", "履歴補完を中断: 開始済みシーズンなし")
        return
    update_season_starts(started)
    targets = started[-SYNC_SEASONS:]
    if not targets:
        print("⚠️ シーズン一覧を取得できなかったため、過去分の補完をスキップします")
        return

    print(f"🔄 過去分の補完を開始: シーズン {[s.get('Code') for s in targets]}")
    total_saved = 0
    for s in targets:
        code = str(s.get("Code"))
        start = int(s.get("StartTime", 0))

        items, page_ids = [], set()
        for page in range(1, MAX_PAGES + 1):
            batch = await fetch_battle_list(code, start, now + 24 * 3600, page)
            if batch is None:
                log_failure("history_list", f"履歴補完を中断: S{code} p{page} 一覧取得失敗")
                return
            if not batch:
                break
            ids = {str(b.get("BattleId")) for b in batch}
            if ids <= page_ids:  # 同じページが返ってきたら打ち切り
                break
            page_ids |= ids
            items.extend(batch)

        print(f"   S{code}: 一覧 {len(items)}件")
        saved = await ingest_items(items, code, notification_eligible=False)
        total_saved += len(saved)
        print(f"   S{code}: 新規保存 {len(saved)}件")

    print(f"✅ 補完完了: 新規 {total_saved}件 / DB合計 {storage.count_battles()}件")

def build_battle_embed(row):
    is_win = row["result"] == "WIN"
    color = discord.Color.gold() if is_win else discord.Color.blue()
    title_prefix = "👑 【WIN】" if is_win else "💀 【LOSS】"
    mvp_suffix = " (MVP!)" if row["is_mvp"] else ""
    diff = row["score_diff"]
    diff_str = f"+{diff}" if diff >= 0 else str(diff)

    embed = discord.Embed(title=f"{title_prefix} ランクマッチ戦績更新{mvp_suffix}", color=color)
    embed.add_field(name="使用キャラ", value=row["character"], inline=True)
    embed.add_field(name="レート変動", value=f"{diff_str} ({row['score_before']} ➔ {row['score_after']})", inline=True)
    embed.add_field(name="与ダメージ", value=f"{row['damage']:,}", inline=True)
    embed.add_field(name="K / D", value=f"{row['kills']} / {row['deaths']}", inline=True)
    embed.add_field(name="試合時間", value=f"{row['round_time']}秒", inline=True)
    embed.set_footer(text="星の翼 戦績自動通知Bot")
    return embed

async def deliver_pending_notifications(channel, limit=20):
    """送信成功分だけ状態を更新し、失敗時は残りを次回実行に委ねる。"""
    sent = 0
    for row in storage.pending_notifications(limit=limit):
        try:
            await channel.send(embed=build_battle_embed(row))
        except Exception as e:
            log_failure("discord_send", f"Discord通知失敗 ({type(e).__name__})")
            break
        storage.mark_notification_sent(row["battle_id"])
        clear_failure("discord_send")
        sent += 1
        print("✅ Discordに新しい戦績を送信しました！")
    return sent

@bot.event
async def on_ready():
    global startup_done
    print(f"🤖 ログイン完了: {bot.user.name}")

    if startup_done:  # 再接続でon_readyが再度呼ばれても二重起動しない
        return
    startup_done = True

    storage.init_db()
    print(f"🗄️ DB準備完了(保存済み {storage.count_battles()}件)")
    try:
        await sync_history()
    except Exception as e:
        print(f"❌ 過去分の補完でエラー(通常の監視は続けます): {type(e).__name__}")
    if SEASON_STARTS:
        fixed = storage.fix_season_codes(SEASON_STARTS)
        if fixed:
            print(f"🔧 シーズン番号を試合時刻から修正: {fixed}件")
    print("📊 DBの中身:")
    for line in storage.summary_lines():
        print(line)
    check_new_battle.start()

@tasks.loop(minutes=2)
async def check_new_battle():
    channel = bot.get_channel(CHANNEL_ID)
    try:
        season = await get_latest_season_info()
        if season is None:
            return
        now = int(time.time())
        items = await fetch_battle_list(season, now - 30 * 24 * 3600, now + 24 * 3600)
        if items is None:
            return
        if not items:
            return

        new_rows = await ingest_items(items, season, notification_eligible=True)
        for row in new_rows:
            print(f"🆕 新しい試合を保存しました！ ID: {row['battle_id']}")
        if channel:
            await deliver_pending_notifications(channel, limit=20)

    except Exception as e:
        log_failure("periodic", f"定期処理に失敗しました: {type(e).__name__}")

@bot.command(name="season")
async def show_season_stats(ctx, season_input: str = None):
    if not await season_authorized(ctx):
        await ctx.send("このコマンドを使う権限がありません。")
        return
    async with ctx.typing():
        try:
            latest_code = await get_latest_season_info()
            if latest_code is None and season_input is None:
                await ctx.send("⚠️ シーズン情報を取得できませんでした。時間をおいて再試行してください。")
                return
            target = season_input if season_input else latest_code
            season_sum_url = f"https://xzy.shengtiangames.com/mini-game/xzy/battle-record/battle_seasonsummary?role_id={ROLE_ID}&season_code={target}"
            season_show_url = f"https://xzy.shengtiangames.com/mini-game/xzy/battle-record/season_show?season_code={target}&role_id={ROLE_ID}"
            raw_summary, raw_sum, raw_show = await asyncio.gather(get_json(API_SUMMARY_URL), get_json(season_sum_url), get_json(season_show_url))
            if not all(api_ok(x) for x in (raw_summary, raw_sum, raw_show)):
                log_failure("season_stats", "!season API code 検証失敗")
                await ctx.send("⚠️ 戦績APIの取得に失敗しました。しばらくしてから再試行してください。")
                return
            if not all(isinstance(x.get("data"), dict) for x in (raw_summary, raw_sum, raw_show)):
                await ctx.send("⚠️ 戦績APIの応答形式が不正です。時間をおいて再試行してください。")
                return
            summary, sum_data, show = raw_summary["data"], raw_sum["data"], raw_show["data"]
            season_sum, detail = sum_data.get("T2"), show.get("T2")
            top_roles = show.get("TopRoleList")
            if not all(isinstance(x, dict) for x in (summary, season_sum, detail)) or not isinstance(top_roles, list):
                await ctx.send("⚠️ 戦績APIの応答形式が不正です。時間をおいて再試行してください。")
                return
            # Validate every row before rendering; partial or malformed data is not a valid report.
            for item in top_roles:
                if not isinstance(item, dict) or not isinstance(item.get("Role"), dict):
                    raise ValueError("invalid TopRoleList row")
                for key in ("TotalCnt", "WinRate"):
                    if key not in item:
                        raise ValueError("missing character statistic")
            total_fight = int(summary["TotalFight"])
            win_rate_all = float(summary["WinRate"]) * 100
            arena = int(detail.get("ArenaScore", season_sum.get("ArenaScore", 0)))
            maximum = int(detail.get("MaxScore", 0))
            total = int(detail.get("TotalCnt", season_sum.get("TotalCnt", 0)))
            wins = int(detail.get("WinCnt", 0))
            if min(total_fight, total, wins, arena, maximum) < 0 or wins > total:
                raise ValueError("invalid totals")
            win_rate = float(detail.get("WinRate", season_sum.get("WinRate", 0))) * 100
            mvp = int(season_sum.get("MvpCnt", 0))
            if total == 0 and not top_roles:
                await ctx.send(f"⚠️ シーズン `S{target}` のプレイデータは見つかりませんでした。")
                return
            chars = []
            for item in top_roles:
                cnt = int(item["TotalCnt"])
                rate = float(item["WinRate"]) * 100
                direct_wins = item.get("WinCnt")
                result = f"{direct_wins}勝 {cnt-direct_wins}敗" if isinstance(direct_wins, int) and 0 <= direct_wins <= cnt else "勝敗数はAPIから取得できません"
                role = item["Role"]
                chars.append(f"• **{role.get('name_jp') or role.get('name') or '不明'}**: {cnt}戦 {result} (勝率 {rate:.1f}%)")
            embed = discord.Embed(title=f"📊 星の翼 シーズン戦績レポート (Season {target})", color=discord.Color.gold())
            embed.add_field(name="🏆 ランクポイント", value=f"現在: **{arena} pt**\n最高: **{maximum} pt**", inline=True)
            embed.add_field(name="⚔️ シーズン成績", value=f"{total}戦 {wins}勝 {total-wins}敗\n勝率: **{win_rate:.2f}%**\nMVP: **{mvp}回**", inline=True)
            embed.add_field(name="🌐 全シーズン総合", value=f"通算: **{total_fight:,}戦**\n総合勝率: **{win_rate_all:.2f}%**", inline=True)
            embed.add_field(name="🎮 キャラ別戦績", value="\n".join(chars) if chars else "このシーズンの使用データがありません。", inline=False)
            embed.set_footer(text="星の翼 公式連携Bot")
            await ctx.send(embed=embed)
        except Exception as e:
            log_failure("season_stats", f"!season 処理失敗: {type(e).__name__}")
            await ctx.send("⚠️ 戦績を表示できませんでした。時間をおいて再試行してください。")

async def season_authorized(ctx):
    if getattr(ctx.author.guild_permissions, "administrator", False):
        return True
    if ctx.author.id in ALLOWED_USER_IDS:
        return True
    return any(role.id in ALLOWED_ROLE_IDS for role in getattr(ctx.author, "roles", []))

if __name__ == "__main__":
    bot.run(BOT_TOKEN)

