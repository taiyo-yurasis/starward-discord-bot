import time
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
import traceback
import os
from dotenv import load_dotenv

import storage

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
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

missing = [key for key, value in required_env.items() if not value]
if missing:
    raise RuntimeError(f"必要な環境変数が設定されていません: {', '.join(missing)}")

SYNC_SEASONS = int(os.getenv("SYNC_SEASONS", "3"))  # 起動時に遡るシーズン数
DETAIL_INTERVAL = 0.5  # 試合詳細APIを連続で叩くときの間隔(秒)
MAX_PAGES = 200  # 一覧のページ送りの上限(暴走防止)

http_session = None
startup_done = False
SEASON_STARTS = []  # [(開始unix秒, シーズンコード)] 開始順。試合時刻→シーズン判定に使う
_keys_logged = False

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
        print(f"⚠️ API取得に失敗しました: {e}")
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
        if isinstance(res, dict):
            seasons = res.get("data", [])
            if isinstance(seasons, list) and seasons:
                now = int(time.time())
                started = [s for s in seasons if int(s.get("StartTime", 0)) <= now]
                if started:
                    update_season_starts(started)
                    return str(SEASON_STARTS[-1][1])
    except Exception as e:
        print(f"⚠️ シーズン一覧の取得に失敗しました: {e}")
    return "14"

def api_ok(res):
    """APIの応答が成功か(codeが無い or 0)"""
    return isinstance(res, dict) and res.get("code", 0) == 0

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
        print(f"⚠️ 戦績一覧の取得に失敗しました: {e}")
        return None

    if not api_ok(res):
        print(f"⚠️ 戦績一覧APIがエラーを返しました: {res.get('msg') if isinstance(res, dict) else res}")
        return None
    items = res.get("data", [])
    return items if isinstance(items, list) else []

async def get_battle_detail(battle_id):
    """指定した試合IDの完全な詳細データを取得する"""
    url = f"{API_DETAIL_URL}?id={battle_id}"
    return await get_json(url)

async def ingest_items(items, season_code):
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
        if storage.insert_battle(row):
            saved.append(row)

        if len(todo) > 1:
            if i % 25 == 0:
                print(f"   ...{i}/{len(todo)}件 処理済み")
            await asyncio.sleep(DETAIL_INTERVAL)
    return saved

async def sync_history():
    """起動時に、直近 SYNC_SEASONS シーズン分の未保存試合をまとめて保存する"""
    res = await get_json(API_SEASON_LIST_URL)
    seasons = res.get("data", []) if api_ok(res) else []
    if not isinstance(seasons, list):
        seasons = []

    now = int(time.time())
    started = sorted(
        (s for s in seasons if int(s.get("StartTime", 0)) <= now),
        key=lambda s: int(s.get("StartTime", 0)),
    )
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
                print(f"⚠️ S{code} 一覧のp{page}で取得失敗。ここまでの分だけ処理します")
                break
            if not batch:
                break
            ids = {str(b.get("BattleId")) for b in batch}
            if ids <= page_ids:  # 同じページが返ってきたら打ち切り
                break
            page_ids |= ids
            items.extend(batch)

        print(f"   S{code}: 一覧 {len(items)}件")
        saved = await ingest_items(items, code)
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
        print(f"❌ 過去分の補完でエラー(通常の監視は続けます): {e}")
        traceback.print_exc()
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
        now = int(time.time())
        items = await fetch_battle_list(season, now - 30 * 24 * 3600, now + 24 * 3600)
        if not items:
            return

        new_rows = await ingest_items(items, season)
        for row in new_rows:
            print(f"🆕 新しい試合を保存しました！ ID: {row['battle_id']}")
            if channel and row["result"] is not None:
                await channel.send(embed=build_battle_embed(row))
                print("✅ Discordに新しい戦績を送信しました！")

    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")

@bot.command(name="season")
async def show_season_stats(ctx, season_input: str = None):
    async with ctx.typing():
        try:
            latest_code = await get_latest_season_info()
            target_season_code = season_input if season_input else latest_code

            season_sum_url = f"https://xzy.shengtiangames.com/mini-game/xzy/battle-record/battle_seasonsummary?role_id={ROLE_ID}&season_code={target_season_code}"
            season_show_url = f"https://xzy.shengtiangames.com/mini-game/xzy/battle-record/season_show?season_code={target_season_code}&role_id={ROLE_ID}"

            raw_summary, raw_season_sum, raw_season_show = await asyncio.gather(
                get_json(API_SUMMARY_URL),
                get_json(season_sum_url),
                get_json(season_show_url),
            )

            if raw_summary is None or raw_season_sum is None or raw_season_show is None:
                await ctx.send("⚠️ 戦績APIの取得に失敗しました。しばらくしてから再試行してください。")
                return

            if isinstance(raw_summary, dict) and raw_summary.get("code") != 0:
                msg = raw_summary.get("msg", "認証エラー")
                await ctx.send(f"⚠️ **戦績の取得に失敗しました（サーバー応答: {msg}）**\nCookieの有効期限が切れています。新しいCookieを取得して更新してください。")
                return

            res_summary = raw_summary.get("data") if isinstance(raw_summary.get("data"), dict) else {}
            sum_data = raw_season_sum.get("data") if isinstance(raw_season_sum.get("data"), dict) else {}
            res_season_sum = sum_data.get("T2", {}) if isinstance(sum_data, dict) else {}
            res_season_show = raw_season_show.get("data") if isinstance(raw_season_show.get("data"), dict) else {}

            t2_detail = res_season_show.get("T2", {}) or {}
            top_roles = res_season_show.get("TopRoleList", [])

            total_fight_all = res_summary.get("TotalFight", 0)
            win_rate_all = float(res_summary.get("WinRate", "0")) * 100

            arena_score = t2_detail.get("ArenaScore", res_season_sum.get("ArenaScore", 0))
            max_score = t2_detail.get("MaxScore", 0)
            total_cnt = t2_detail.get("TotalCnt", res_season_sum.get("TotalCnt", 0))
            win_cnt = t2_detail.get("WinCnt", 0)
            loss_cnt = total_cnt - win_cnt if total_cnt >= win_cnt else 0
            win_rate_season = float(t2_detail.get("WinRate", res_season_sum.get("WinRate", "0"))) * 100
            mvp_cnt = res_season_sum.get("MvpCnt", 0)

            if total_cnt == 0 and not top_roles:
                await ctx.send(f"⚠️ シーズン `S{target_season_code}` のプレイデータは見つかりませんでした。")
                return

            char_stats_text = ""
            for item in top_roles:
                role_info = item.get("Role", {})
                char_name = role_info.get("name_jp") or role_info.get("name", "不明")
                c_total = item.get("TotalCnt", 0)
                c_win_rate = float(item.get("WinRate", "0")) * 100
                c_wins = round(c_total * (c_win_rate / 100))
                c_losses = c_total - c_wins
                
                char_stats_text += f"• **{char_name}**: {c_total}戦 {c_wins}勝 {c_losses}敗 (勝率 {c_win_rate:.1f}%)\n"

            if not char_stats_text:
                char_stats_text = "このシーズンの使用データがありません。"

            embed = discord.Embed(
                title=f"📊 星の翼 シーズン戦績レポート (Season {target_season_code})",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="🏆 ランクポイント",
                value=f"現在: **{arena_score} pt**\n最高: **{max_score} pt**",
                inline=True
            )
            embed.add_field(
                name="⚔️ シーズン成績",
                value=f"{total_cnt}戦 {win_cnt}勝 {loss_cnt}敗\n勝率: **{win_rate_season:.2f}%**\nMVP: **{mvp_cnt}回**",
                inline=True
            )
            embed.add_field(
                name="🌐 全シーズン総合",
                value=f"通算: **{total_fight_all:,}戦**\n総合勝率: **{win_rate_all:.2f}%**",
                inline=True
            )
            embed.add_field(
                name="🎮 キャラ別戦績",
                value=char_stats_text,
                inline=False
            )
            embed.set_footer(text="星の翼 公式連携Bot")

            await ctx.send(embed=embed)

        except Exception as e:
            print(f"❌ シーズン戦績取得エラー: {e}")
            await ctx.send(f"⚠️ エラーが発生しました:\n```python\n{e}\n```")

bot.run(BOT_TOKEN)