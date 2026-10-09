# starward-discord-bot

星の翼 (Starward) のランクマッチ戦績を取得し、Discordチャンネルへ通知するBotです。SQLiteに戦績の必要項目を保存し、2分ごとに新しい試合を確認します。

## 必要なもの

- Python 3.10 以上
- Discord Botトークン
- 星の翼APIを利用するためのユーザー名、Role ID、API token、Cookie
- Botを招待できるDiscordサーバー

## セットアップ

1. リポジトリを取得し、プロジェクトディレクトリへ移動します。
2. 仮想環境を作り、有効化します。

Windows PowerShell:

~~~powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
~~~

Windowsのcmd:

~~~bat
py -3.10 -m venv .venv
.venv\Scripts\activate.bat
~~~

macOS / Linux:

~~~sh
python3 -m venv .venv
source .venv/bin/activate
~~~

3. 固定バージョンの依存関係をインストールします。

~~~sh
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

4. .env.example を .env にコピーし、各値を設定します。

~~~text
DISCORD_BOT_TOKEN=Discord Bot token
DISCORD_CHANNEL_ID=通知先チャンネルID
STARWARD_USERNAME=ゲーム内ユーザー名
STARWARD_ROLE_ID=ゲーム内Role ID
STARWARD_API_TOKEN=星の翼API token
STARWARD_COOKIE=星の翼API Cookie
~~~

.env は秘密情報を含むため、Gitへコミットしないでください。

5. Botを起動します。

~~~sh
python main.py
~~~

Windowsでは run.bat から起動することもできます。データベース battles.db は初回起動時に作成されます。

## Discordの設定と権限

Discord Developer PortalでBotの **Message Content Intent** を有効にしてください。Botはプレフィックスコマンド !season を読むため、この特権Intentが必要です。

Botをサーバーに招待するときは、次のOAuth2スコープと権限を付与します。

- OAuth2 scope: bot
- View Channels
- Send Messages
- Embed Links
- Read Message History

通知先チャンネルでBotが上記のチャンネル権限を持つことを確認してください。Bot自身にManage Messages権限は不要です。

!season コマンドを実行するユーザーには、サーバーの **Manage Messages（メッセージの管理）** 権限が必要です。コマンドはサーバー内でのみ実行でき、同じユーザーは60秒に1回まで利用できます。

## コマンド

- !season: 最新シーズンの戦績を表示します。
- !season 14: 指定したシーズン番号の戦績を表示します。引数は1〜6桁の数字です。

## 保存データ

試合ID、シーズン、試合時刻、結果、キャラクター、レート、ダメージ、キル数、デス数、試合時間など、通知と集計に必要な項目のみSQLiteへ保存します。APIの生レスポンスは保存しません。旧DBに生レスポンス列がある場合、起動時に内容を消去します。
