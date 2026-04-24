# Discord Pomodoro Bot

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Discord 上の複数チャンネルで同時に走る共同ポモドーロ Bot。
スラッシュコマンドは **`/pomo` の 1 個だけ**、残りはコントロールパネルのボタンで完結します。

```
🍅 ポモドーロ - 作業
━━━━━━━━━━━━━━━━━━━━
⏱  12:34 / 25:00
██████████░░░░░░░░░░  50%

🍅 このラウンドの完了: 3 個

👥 参加者 (3)
👑 @alice — 数学
•  @bob   — 英語
•  @carol — —

[🙋 参加] [🚪 退出] [✍️ タスク] [📊 統計]
[⏸ 一時停止] [⏭ スキップ] [🔄 リセット] [🛑 終了]   ← オーナー専用
```

## 特徴

- **マルチルーム / マルチ参加者** — 1 チャンネルに 1 ルーム、複数チャンネルを同時並走。1 ユーザーは同時 1 ルームまで(ルーム移動時は自動退出)
- **参加者ごとの個別タスク** — パネルのボタン → モーダルで自分だけのタスクを編集。WORK フェーズ終了時、その瞬間の参加者全員の `pomodoros` に 1 行ずつ記録されるので個人統計もそのまま残る
- **オーナー制御** — `一時停止 / スキップ / リセット / 終了` はルーム作成者のみ。オーナーが退出したら最も早く参加した残留メンバーへ権限委譲、全員抜けたら自動終了
- **永続 View** — 各ボタンの `custom_id` にルーム UUID を埋め込み、多ルーム並走でも衝突しない
- **再起動時のポリシー** — 実行中ルームは `bot_restart` で DB 側を閉じ、古いパネルメッセージはボタンを剥がして「`/pomo` で作り直して」と案内する
- **PostgreSQL + Alembic** — スキーマ変更はマイグレーションで管理。起動時に `alembic upgrade head` が自動で走る

### パネルのボタン

ボタンは 2 行構成(Discord の Action Row 上限 5 個に収めるため)。

| ボタン | 権限 | 動作 |
|---|---|---|
| 🙋 参加 | 誰でも | そのルームに入る。別ルーム参加中なら自動で抜ける |
| 🚪 退出 | 参加者 | 抜ける。オーナーなら権限委譲、最後の 1 人なら自動終了 |
| ✍️ タスク | 参加者 | モーダルで自分のタスクを編集 |
| 📊 統計 | 誰でも | 自分の今日/今週/累計完了数を ephemeral で表示 |
| ⏸ 一時停止 | オーナー | タイマーを一時停止 / 再開 |
| ⏭ スキップ | オーナー | 次のフェーズへ(🍅 はカウントせず) |
| 🔄 リセット | オーナー | 今のフェーズを頭から |
| 🛑 終了 | オーナー | ルームを明示的に終了 |

## 技術スタック

Python 3.12 / discord.py ~2.6 / PostgreSQL 17 / SQLAlchemy 2.x (async) + asyncpg / Alembic / pydantic-settings / Docker Compose / Railway

## クイックスタート

### 1. Discord Bot を用意する

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** → 作成した Application の **Bot** タブから **Reset Token** でトークンを取得(後で `.env` に入れる)
2. **OAuth2 → URL Generator** で以下を選択して **Invite URL** を生成、サーバに招待:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**: `Send Messages`, `Embed Links`, `Read Message History`
3. **Privileged Gateway Intents** は **不要**(`Intents.default()` で完結。`Message Content` などは触らなくてよい)

### 2. ローカルで起動

```bash
cp .env.example .env
# .env の DISCORD_TOKEN を 1 で取得したトークンに差し替える
docker compose up --build
```

Discord サーバで `/pomo` を叩くとパネルが出ます。

`/pomo` は時間指定にも対応しています(未指定なら環境変数の既定値):

- `/pomo` — 既定の 25/5/15, 4セット
- `/pomo work_minutes:50 short_break_minutes:10 long_break_minutes:20 long_break_every:3`

> 開発中は `.env` に `DISCORD_GUILD_IDS=<サーバ ID>` を書くとコマンドが即座に同期されます(グローバル同期は最大 1 時間かかる)。

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `DISCORD_TOKEN` | *必須* | Discord Bot トークン |
| `DISCORD_GUILD_IDS` | 空 | カンマ区切りの guild ID。指定するとその guild にだけ即座に commands を同期。空ならグローバル(反映に最大 1 時間) |
| `DATABASE_URL` | `postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro` | `postgres://` / `postgresql://` / `postgresql+asyncpg://` すべて受け付け |
| `POMO_WORK_SECONDS` | `1500` (25 分) | WORK フェーズの長さ |
| `POMO_SHORT_BREAK_SECONDS` | `300` (5 分) | 短休憩の長さ |
| `POMO_LONG_BREAK_SECONDS` | `900` (15 分) | 長休憩の長さ |
| `POMO_LONG_BREAK_EVERY` | `4` | N 回目の WORK 完了で長休憩へ |
| `POMO_TICK_SECONDS` | `10` | Embed を更新する間隔 |
| `LOG_LEVEL` | `INFO` | stdlib logging レベル |

## Railway デプロイ

1. Railway プロジェクトに **PostgreSQL** プラグインを追加
2. サービスの環境変数に `DISCORD_TOKEN` を設定(`DATABASE_URL` は Railway が `postgresql://…` として自動注入される。`src/config.py` が asyncpg 形式へ正規化する)
3. このリポジトリを接続すると [railway.toml](railway.toml) に従って Dockerfile でビルドされ、[Dockerfile](Dockerfile) の `CMD` (`alembic upgrade head && python -m src.main`) で起動する

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `/pomo` がコマンド一覧に出てこない | グローバル同期は最大 1 時間かかる。開発中は `DISCORD_GUILD_IDS` を設定してそのサーバに即時同期する |
| パネルの Embed が更新されない / パネルが送れない | Bot に **Send Messages** / **Embed Links** 権限があるか確認 |
| ボタンを押すと "This interaction failed" | Bot 再起動直後の古いパネルの可能性。新しく `/pomo` を叩けば案内メッセージに置き換わる |
| `/pomo` を叩いても反応しない | サーバでアプリの **Use Application Commands** が無効化されている、または最近作成した Application で権限が未同期。ロール設定を確認 |
| このチャンネルにはすでにアクティブなルームがあります | 既存ルームの `🛑 終了` を押してから `/pomo` を再実行。または誰も見ていない放棄ルームなら DB のその行を手動で閉じる |

## 設計

レイヤ分離の方針は `discord-util-bot` に揃えています: **`bot.py` のコマンドハンドラは薄く**、**`services/` が `AsyncSession` を受ける async 関数の集合**、**`core/` に DB 非依存の純ロジック**。Repository クラスは作らず、`async with async_session() as session:` で都度セッションを開いて関数に渡します。

### ディレクトリ

```
src/
├── main.py              # エントリ(SIGINT/SIGTERM ハンドラ + DB 接続リトライ)
├── bot.py               # PomodoroBot + /pomo ハンドラ + 起動時の孤立ルーム掃除
├── config.py            # pydantic-settings、module-level `settings`
├── constants.py         # 色 / プログレスバー / 既定サイクル
├── room_manager.py      # 複数ルーム状態 + タイマーループ。業務結果は `OpResult`(失敗理由 enum)で返す
├── core/                # DB 非依存の純ロジック
│   ├── phase.py         #   Phase enum / PhasePlan / next_phase
│   └── room_state.py    #   RoomState / ParticipantState
├── database/            # engine.py (async_session) + models.py
├── services/            # AsyncSession を受ける async 関数群
│   └── room_service.py  #   create/join/leave/task/end/record_pomodoros/stats
└── ui/                  # Discord UI
    ├── embeds.py        #   room_embed / ended_embed / stats_embed
    └── room_panel.py    #   RoomPanelView(persistent) + TaskModal
alembic/versions/…       # DB マイグレーション
tests/                   # src/ と対のツリー(64 件)
```

### DB スキーマ

| テーブル | 役割 | 主要な整合性 |
|---|---|---|
| `pomodoro_rooms` | チャンネルに紐づく共有タイマー | `channel_id` に部分 UNIQUE INDEX (`WHERE ended_at IS NULL`) で 1 チャンネル 1 アクティブ |
| `room_participants` | 誰がいつから/いつまで参加したかの append-only ログ | `(room_id, user_id)` の部分 UNIQUE INDEX (`WHERE left_at IS NULL`) で二重参加防止 |
| `pomodoros` | 完了 🍅 を 1 人 1 行で記録 | `(user_id, completed_at)` にインデックス(stats 用) |
| `room_events` | ライフサイクルの append-only outbox | 将来の通知 / 分析連携のための tail 対象 |

### マイグレーション

```bash
# モデルを変更したあと
alembic revision -m "add foo" --autogenerate
alembic upgrade head
```

ファイル名規約: `{YYYYMMDD}_{HHMMSS}_{rev}_{slug}.py`。

## 開発

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' yamllint

# CI と同じチェック一式
ruff format --check .
ruff check src tests
mypy src
yamllint -s .
pytest                  # DB が必要なテストは接続不可なら自動 skip
```

### テスト

- [tests/core/test_phase.py](tests/core/test_phase.py) — 次フェーズ判定、長休憩境界
- [tests/core/test_room_state.py](tests/core/test_room_state.py) — タイマー計算 / pause / resume / 参加者操作 / オーナー委譲
- [tests/services/test_room_service.py](tests/services/test_room_service.py) — create/end/join/leave/task/record_pomodoros/stats の DB 往復
- [tests/ui/test_embeds.py](tests/ui/test_embeds.py) — 色 / プログレスバー / 参加者一覧 / 👑
- [tests/ui/test_room_panel.py](tests/ui/test_room_panel.py) — `TaskModal` のインスタンス隔離 / `RoomPanelView` の `custom_id` 一意性
- [tests/test_config.py](tests/test_config.py) — URL 正規化 / CSV パース / token 必須
- [tests/test_room_manager.py](tests/test_room_manager.py) — マルチルーム独立、ルーム間移動の並行デッドロック回避(regression)、オーナー権限、権限委譲、自動終了、参加者別 🍅 記録、`IntegrityError` 経路

DB ありで走らせる場合:

```bash
docker compose up -d db
DISCORD_TOKEN=test \
DATABASE_URL='postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro_test' \
  alembic upgrade head
DISCORD_TOKEN=test \
DATABASE_URL='postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro_test' \
  pytest
```

### CI

[.github/workflows/ci.yml](.github/workflows/ci.yml) は 2 ジョブ:

- **backend-lint** — ruff format / ruff check / mypy / yamllint
- **backend-test** — `postgres:17-alpine` サービスコンテナを立てて `alembic upgrade head` → pytest + coverage

## 拡張ポイント

| やりたいこと | どこを触るか |
|---|---|
| 統計グラフ画像 | `pomodoros` に `room_id` / `user_id` / `duration_seconds` / `completed_at` が揃っているので、`services/room_service.py` にクエリを足すだけ |
| VOICEVOX / 外部通知 | `room_events` が append-only outbox。別プロセスで tail して `phase_completed` などを拾う |
| ユーザー / ルーム別のカスタムサイクル | `PhasePlan` を DB 化し、`RoomManager` 生成時に差し替えられる形へ拡張 |
| 再起動時のタイマー復元 | いまは `bot_restart` で閉じるだけ。`pomodoro_rooms` に `phase` / `phase_started_at` / `paused_accumulated_seconds` を永続化すれば resume 可能 |
