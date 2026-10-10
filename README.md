# starward-discord-bot
# 星の翼 (Starward) Discord 戦績通知 Bot

星の翼のランクマッチ戦績を取得し、Discordチャンネルへ通知するBotです。

## 初期設定

1. `requirements.txt` の依存関係をインストールし、`.env.example` を `.env` にコピーして値を設定します。
2. Discord Developer Portal で **Message Content Intent** を有効にしてください。Bot側でも `message_content` intent を要求します。
3. `DISCORD_CHANNEL_ID` は正のDiscordチャンネルIDにします。未設定・0・不正値では起動時に明確なエラーになります。
4. `STARWARD_ALLOWED_USER_IDS` と `STARWARD_ALLOWED_ROLE_IDS` に `!season` の許可ユーザー/ロールIDをカンマ区切りで設定できます。どちらも未設定ならBot管理者のみ利用でき、Bot管理者は常に利用できます。
5. `STARWARD_ROLE_ID` はゲーム側のIDであり、Discord権限設定には使いません。

## 起動とデータ

初回起動では `battles.db` を作成し、`SYNC_SEASONS` の範囲の履歴を保存します。履歴補完で保存した試合と起動前からある全行は通知済みとして扱い、過去試合を一斉通知しません。新規検知で詳細取得まで成功し、自分の結果が得られた試合だけを通知待ちにします。

既存の `battles.db` は削除・置換せず、通知状態列を再実行安全なマイグレーションで追加します。既存行は通知不要として初期化します。通知失敗はDBに保持され、次回の2分定期処理で最大20件ずつ再試行します。Discord送信成功後、送信済み状態を保存する前にプロセスが停止すると、再起動後に重複通知される可能性があります。DiscordとSQLiteをまたぐ完全な一度限り配信は保証できません。

シーズン一覧や戦績APIが失敗した場合は固定シーズンやゼロ件に置き換えず、その処理を中断して次の定期実行で再試行します。ログには例外型など原因の概要のみを記録し、トークンやCookieは出しません。試合詳細の取得に失敗した試合は保存・通知せず、次回一覧取得で再取得します。

`.env` と `*.db` はGit管理対象外です。.env.example は管理対象です。
