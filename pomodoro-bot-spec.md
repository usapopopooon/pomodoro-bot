# Discord ポモドーロBot 実装指示書

VCインチャットでボタン操作できるシンプルなポモドーロBotを作る。既存の `voicevox-discord` と同じく Railway でホスティングする前提。

## 技術スタック

- Python 3.11+
- discord.py (2.x, app_commands + ui.View)
- SQLite (永続化) ※Railway Volumeにマウント
- Docker Compose（ローカル開発用）
- Railway deploy

## MVP機能スコープ

以下の最小構成で作る。共同ポモドーロ・統計グラフ・ランキング等は後回し。

1. スラッシュコマンドでセッション開始
2. Embedメッセージ1枚にタイマー状態を表示
3. ボタンで一時停止・スキップ・リセット
4. 作業25分 → 短休憩5分 × 4セット → 長休憩15分のループ
5. フェーズ切替時のVC通知（VOICEVOXとの連携は今回やらず、テキスト通知のみ）
6. 完了ポモドーロ数をユーザー単位でSQLiteに記録

## コマンド仕様

- `/pomo start [task]` セッション開始。taskは任意の文字列
- `/pomo stop` セッション終了
- `/pomo stats` 今日・今週の完了数を返す（表形式のEmbed）

## Embed表示仕様

```
🍅 ポモドーロ中 - 作業
━━━━━━━━━━━━━━━━━━━━
⏱  18:42 / 25:00
█████████████░░░░░░░  67%

🍅🍅🍅🍅 今日の完了数
🎯 タスク: <ユーザー入力>

[⏸ 一時停止] [⏭ スキップ] [🔄 リセット] [🚪 退出]
```

- プログレスバーは `█` × n + `░` × (20-n) の20段階
- フェーズごとにEmbedの `color` を変える
  - 作業中: `0xe74c3c`
  - 短休憩: `0x2ecc71`
  - 長休憩: `0x3498db`
- 残り時間の更新は10秒間隔でEmbedを編集（Discord APIのレート制限を考慮）
- ボタンは `discord.ui.View` でタイムアウト無効化

## 状態管理

- セッションはユーザーIDをキーにメモリ上で管理（`dict[user_id, Session]`）
- Session構造
  - task: str
  - phase: Enum(WORK, SHORT_BREAK, LONG_BREAK)
  - started_at: datetime
  - paused_at: datetime | None
  - completed_count: int（このセッション内の完了🍅数）
  - message: discord.Message（編集対象）
- プロセス再起動で失われてよい（完了履歴のみDB保存）

## DBスキーマ

```sql
CREATE TABLE pomodoros (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  guild_id TEXT NOT NULL,
  task TEXT,
  completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_user_completed ON pomodoros(user_id, completed_at);
```

## タイマーループ実装方針

- `asyncio.create_task` でユーザーごとにバックグラウンドループを起動
- 10秒sleep → 残り時間計算 → Embed編集、を繰り返す
- フェーズ終了時は次フェーズへ遷移してVCテキストチャンネルに通知メッセージ
- 一時停止中は `paused_at` を見てsleepを継続するだけ

## ディレクトリ構成

```
pomodoro-discord/
├── bot/
│   ├── __init__.py
│   ├── main.py          # エントリポイント
│   ├── cog.py           # /pomoコマンド群
│   ├── session.py       # Session / PhaseManager
│   ├── views.py         # Buttonコンポーネント
│   ├── embeds.py        # Embed生成
│   └── db.py            # SQLite wrapper (aiosqlite)
├── Dockerfile
├── docker-compose.yml
├── railway.json
├── requirements.txt
└── .env.example
```

## Railway設定

- 環境変数: `DISCORD_TOKEN`, `DB_PATH` (デフォルト `/data/pomodoro.db`)
- Railway Volumeを `/data` にマウントしてDB永続化
- `railway.json` で `startCommand: python -m bot.main`

## 注意点

- Embed編集のレート制限: 同一メッセージへの編集は5秒あたり5回が上限目安。10秒間隔なら余裕
- ボタンインタラクションは3秒以内に応答しないとタイムアウトエラーになるので、重い処理は `interaction.response.defer()` してから
- 複数ユーザーが同時にセッションを持てる設計にする（グローバルなタイマーにしない）
- discord.py 2.x 系を使うこと（1.x系はスラッシュコマンド非対応）

## やらないこと（スコープ外）

- 共同ポモドーロ
- matplotlibでの統計グラフ画像生成
- サーバー内ランキング
- VOICEVOX連携による音声通知
- スレッド連携でのタスク記録

これらは MVP 動作確認後に追加する。
