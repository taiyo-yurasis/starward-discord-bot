import asyncio
import math
import os
import re
import time
import traceback

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import storage

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
MY_USERNAME = os.getenv("STARWARD_USERNAME")
ROLE_ID = os.getenv("STARWARD_ROLE_ID")
API_TOKEN = os.getenv("STARWARD_API_TOKEN")
COOKIE = os.getenv("STARWARD_COOKIE")

API_BASE = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record"
API_LIST_URL = f"{API_BASE}/battle_list"
API_DETAIL_URL = f"{API_BASE}/battle_detail2v2"
API_SUMMARY_URL = f"{API_BASE}/battle_summary"
API_SEASON_SUMMARY_URL = f"{API_BASE}/battle_seasonsummary"
API_SEASON_SHOW_URL = f"{API_BASE}/season_show"
API_SEASON_LIST_URL = f"{API_BASE}/season_list"

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

SYNC_SEASONS = int(os.getenv("SYNC_SEASONS", "3"))
DETAIL_INTERVAL = 0.5
MAX_PAGES = 200
MAX_EMBED_FIELD_VALUE = 900

http_session = None
startup_done = False
SEASON_STARTS = []
_keys_logged = False


class StarwardBot(commands.Bot):
    """aiohttpのセッションライフサイクルを管理するBotクラス."""

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


async def get_json(url, params=None):
    """APIからJSONを非同期で取得する."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with http_session.get(
            url, headers=HEADERS, params=params, timeout=timeout
        ) as response:
            if response.status == 200:
                return await response.json()
            print(f"⚠️ API HTTPエラー: {response.status} ({response.url})")
    except Exception as e:
        print(f"⚠️ API取得に失敗しました: {e}")
    return None


def update_season_starts(started):
    """開始済みシーズンの一覧からSEASON_STARTSを更新する."""
    SEASON_STARTS[:] = sorted(
        (int(s.get("StartTime", 0)), str(s.get("Code"))) for s in started
    )


async def get_latest_season_info():
    """開催中の最新シーズンCodeを返す."""
    try:
        res = await get_json(API_SEASON_LIST_URL)
        if isinstance(res, dict):
            seasons = res.get("data", [])
            if isinstance(seasons, list) and seasons:
                now = int(time.time())
                started = [
                    s for s in seasons
                    if isinstance(s, dict) and int(s.get("StartTime", 0)) <= now
                ]
                if started:
                    update_season_starts(started)
                    return str(SEASON_STARTS[-1][1])
    except Exception as e:
        print(f"⚠️ シーズン一覧の取得に失敗しました: {e}")
    return "14"


def api_ok(res):
    """APIの応答が成功か(codeが無い or 0)."""
    return isinstance(res, dict) and res.get("code", 0) == 0


async def fetch_battle_list(season_code, time_start, time_end, page=1):
    """2v2戦績一覧を1ページ取得する."""
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
        async with http_session.get(
            API_LIST_URL, headers=HEADERS, params=params, timeout=timeout
        ) as response:
            if response.status != 200:
                body = await response.text()
                print(
                    f"⚠️ API HTTPエラー: {response.status} ({response.url})"
                    f"\nレスポンス: {body[:500]}"
                )
                return None
            res = await response.json()
    except Exception as e:
        print(f"⚠️ 戦績一覧の取得に失敗しました: {e}")
        return None

    if not api_ok(res):
        msg = res.get("msg") if isinstance(res, dict) else res
        print(f"⚠️ 戦績一覧APIがエラーを返しました: {msg}")
        return None
    items = res.get("data", [])
    return items if isinstance(items, list) else []


async def get_battle_detail(battle_id):
    """指定した試合IDの詳細を取得する."""
    return await get_json(API_DETAIL_URL, params={"id": battle_id})


async def ingest_items(items, season_code):
    """未保存の試合詳細を取得し、必要な項目だけ保存する."""
    global _keys_logged
    known = storage.known_ids()
    todo, seen = [], set()
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
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

        if not _keys_logged:
            _keys_logged = True
            print(f"📋 一覧の項目: {list(item.keys())}")
            data = detail.get("data")
            if isinstance(data, dict):
                print(f"📋 詳細の項目: {list(data.keys())}")

        row = storage.extract_row(bid, detail, MY_USERNAME, item, season_code)
        if row is None:
            print(f"⚠️ 想定外の詳細データ形式のためスキップ: {bid}")
            continue
        if row["played_at"] and SEASON_STARTS:
            row["season_code"] = (
                storage.season_for(row["played_at"], SEASON_STARTS)
                or row["season_code"]
            )
        if row["result"] is None:
            print(f"⚠️ 試合 {bid} に自分({MY_USERNAME})が見つかりません")
        if storage.insert_battle(row):
            saved.append(row)

        if len(todo) > 1:
            if i % 25 == 0:
                print(f"   ...{i}/{len(todo)}件 処理済み")
            await asyncio.sleep(DETAIL_INTERVAL)
    return saved


async def sync_history():
    """起動時に直近シーズン分の未保存試合を補完する."""
    res = await get_json(API_SEASON_LIST_URL)
    seasons = res.get("data", []) if api_ok(res) else []
    if not isinstance(seasons, list):
        seasons = []

    now = int(time.time())
    started = sorted(
        (
            s for s in seasons
            if isinstance(s, dict) and int(s.get("StartTime", 0)) <= now
        ),
        key=lambda s: int(s.get("StartTime", 0)),
    )
    update_season_starts(started)
    targets = started[-SYNC_SEASONS:]
    if not targets:
        print("⚠️ シーズン一覧を取得できなかったため、過去分の補完をスキップします")
        return

    print(f"🔄 過去分の補完を開始: シーズン {[s.get('Code') for s in targets]}")
    total_saved = 0
    for season in targets:
        code = str(season.get("Code"))
        start = int(season.get("StartTime", 0))
        items, page_ids = [], set()
        for page in range(1, MAX_PAGES + 1):
            batch = await fetch_battle_list(code, start, now + 24 * 3600, page)
            if batch is None:
                print(f"⚠️ S{code} 一覧のp{page}で取得失敗。ここまでの分だけ処理します")
                break
            if not batch:
                break
            ids = {
                str(item.get("BattleId"))
                for item in batch
                if isinstance(item, dict) and item.get("BattleId") is not None
            }
            if ids and ids <= page_ids:
                break
            page_ids |= ids
            items.extend(batch)

        print(f"   S{code}: 一覧 {len(items)}件")
        saved = await ingest_items(items, code)
        total_saved += len(saved)
        print(f"   S{code}: 新規保存 {len(saved)}件")

    print(f"✅ 補完完了: 新規 {total_saved}件 / DB合計 {storage.count_battles()}件")


def _bounded_text(value, limit=MAX_EMBED_FIELD_VALUE):
    text = str(value)
    if len(text) <= limit:
        return text or "不明"
    return text[: limit - 1] + "…"


def _safe_int(value, default=0):
    try:
        return max(min(int(value), 10**15), -(10**15))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def build_battle_embed(row):
    is_win = row["result"] == "WIN"
    color = discord.Color.gold() if is_win else discord.Color.blue()
    title_prefix = "👑 【WIN】" if is_win else "💀 【LOSS】"
    mvp_suffix = " (MVP!)" if row["is_mvp"] else ""
    diff = _safe_int(row["score_diff"])
    diff_str = f"+{diff}" if diff >= 0 else str(diff)

    embed = discord.Embed(
        title=_bounded_text(f"{title_prefix} ランクマッチ戦績更新{mvp_suffix}", 256),
        color=color,
    )
    embed.add_field(
        name="使用キャラ", value=_bounded_text(row["character"]), inline=True
    )
    embed.add_field(
        name="レート変動",
        value=_bounded_text(
            f"{diff_str} ({_safe_int(row['score_before'])} ➔ "
            f"{_safe_int(row['score_after'])})"
        ),
        inline=True,
    )
    embed.add_field(name="与ダメージ", value=f"{_safe_int(row['damage']):,}", inline=True)
    embed.add_field(
        name="K / D",
        value=f"{_safe_int(row['kills'])} / {_safe_int(row['deaths'])}",
        inline=True,
    )
    embed.add_field(
        name="試合時間", value=f"{_safe_int(row['round_time'])}秒", inline=True
    )
    embed.set_footer(text="星の翼 戦績自動通知Bot")
    return embed


@bot.event
async def on_ready():
    global startup_done
    print(f"🤖 ログイン完了: {bot.user.name}")
    if startup_done:
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
        items = await fetch_battle_list(
            season, now - 30 * 24 * 3600, now + 24 * 3600
        )
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
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
@commands.cooldown(1, 60, commands.BucketType.user)
async def show_season_stats(ctx, season_input: str = None):
    if season_input is not None and not re.fullmatch(r"[0-9]{1,6}", season_input):
        await ctx.send("⚠️ シーズン番号は1〜6桁の数字で指定してください。")
        return

    async with ctx.typing():
        try:
            latest_code = await get_latest_season_info()
            target_season_code = season_input or latest_code
            role_params = {"role_id": ROLE_ID}
            season_params = {"role_id": ROLE_ID, "season_code": target_season_code}

            raw_summary, raw_season_sum, raw_season_show = await asyncio.gather(
                get_json(API_SUMMARY_URL, params=role_params),
                get_json(API_SEASON_SUMMARY_URL, params=season_params),
                get_json(API_SEASON_SHOW_URL, params=season_params),
            )
            if (
                raw_summary is None
                or raw_season_sum is None
                or raw_season_show is None
            ):
                await ctx.send(
                    "⚠️ 戦績APIの取得に失敗しました。しばらくしてから再試行してください。"
                )
                return

            if not api_ok(raw_summary):
                await ctx.send(
                    "⚠️ 戦績の取得に失敗しました。Cookieの有効期限を確認してください。"
                )
                return

            res_summary = raw_summary.get("data")
            res_summary = res_summary if isinstance(res_summary, dict) else {}
            sum_data = raw_season_sum.get("data")
            sum_data = sum_data if isinstance(sum_data, dict) else {}
            res_season_sum = sum_data.get("T2")
            res_season_sum = res_season_sum if isinstance(res_season_sum, dict) else {}
            res_season_show = raw_season_show.get("data")
            res_season_show = (
                res_season_show if isinstance(res_season_show, dict) else {}
            )
            t2_detail = res_season_show.get("T2")
            t2_detail = t2_detail if isinstance(t2_detail, dict) else {}
            top_roles = res_season_show.get("TopRoleList")
            top_roles = top_roles if isinstance(top_roles, list) else []

            total_fight_all = _safe_int(res_summary.get("TotalFight"))
            win_rate_all = _safe_float(res_summary.get("WinRate")) * 100
            arena_score = _safe_int(
                t2_detail.get("ArenaScore", res_season_sum.get("ArenaScore", 0))
            )
            max_score = _safe_int(t2_detail.get("MaxScore"))
            total_cnt = _safe_int(
                t2_detail.get("TotalCnt", res_season_sum.get("TotalCnt", 0))
            )
            win_cnt = min(_safe_int(t2_detail.get("WinCnt")), total_cnt)
            loss_cnt = max(total_cnt - win_cnt, 0)
            win_rate_season = _safe_float(
                t2_detail.get("WinRate", res_season_sum.get("WinRate", "0"))
            ) * 100
            mvp_cnt = _safe_int(res_season_sum.get("MvpCnt"))

            if total_cnt == 0 and not top_roles:
                await ctx.send(
                    f"⚠️ シーズン S{target_season_code} のプレイデータは見つかりませんでした。"
                )
                return

            char_lines = []
            for item in top_roles:
                if not isinstance(item, dict):
                    continue
                role_info = item.get("Role")
                role_info = role_info if isinstance(role_info, dict) else {}
                char_name = role_info.get("name_jp") or role_info.get("name", "不明")
                c_total = max(_safe_int(item.get("TotalCnt")), 0)
                c_win_rate = _safe_float(item.get("WinRate")) * 100
                c_wins = min(round(c_total * c_win_rate / 100), c_total)
                c_losses = c_total - c_wins
                line = (
                    f"• **{_bounded_text(char_name, 80)}**: "
                    f"{c_total}戦 {c_wins}勝 {c_losses}敗 "
                    f"(勝率 {c_win_rate:.1f}%)"
                )
                candidate = "\n".join(char_lines + [line])
                if len(candidate) > MAX_EMBED_FIELD_VALUE:
                    char_lines.append("…以降のキャラ別データは省略しました")
                    break
                char_lines.append(line)

            char_stats_text = "\n".join(char_lines) or "このシーズンの使用データがありません。"
            char_stats_text = _bounded_text(char_stats_text)
            embed = discord.Embed(
                title=_bounded_text(
                    f"📊 星の翼 シーズン戦績レポート (Season {target_season_code})",
                    256,
                ),
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="🏆 ランクポイント",
                value=_bounded_text(
                    f"現在: **{arena_score} pt**\n最高: **{max_score} pt**"
                ),
                inline=True,
            )
            embed.add_field(
                name="⚔️ シーズン成績",
                value=_bounded_text(
                    f"{total_cnt}戦 {win_cnt}勝 {loss_cnt}敗\n"
                    f"勝率: **{win_rate_season:.2f}%**\nMVP: **{mvp_cnt}回**"
                ),
                inline=True,
            )
            embed.add_field(
                name="🌐 全シーズン総合",
                value=_bounded_text(
                    f"通算: **{total_fight_all:,}戦**\n"
                    f"総合勝率: **{win_rate_all:.2f}%**"
                ),
                inline=True,
            )
            embed.add_field(
                name="🎮 キャラ別戦績",
                value=char_stats_text,
                inline=False,
            )
            embed.set_footer(text="星の翼 公式連携Bot")
            await ctx.send(embed=embed)

        except Exception as e:
            print(f"❌ シーズン戦績取得エラー: {e}")
            await ctx.send("⚠️ 戦績取得中にエラーが発生しました。")


@show_season_stats.error
async def show_season_stats_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ このコマンドには「メッセージの管理」権限が必要です。")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"⏳ クールダウン中です。{error.retry_after:.0f}秒後に再度お試しください。"
        )
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("⚠️ このコマンドはサーバー内で実行してください。")
    else:
        print(f"❌ コマンドエラー: {error}")
        await ctx.send("⚠️ コマンドの処理中にエラーが発生しました。")


bot.run(BOT_TOKEN)
