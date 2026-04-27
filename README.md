# Discord Pomodoro Bot

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

複数人で同じタイマー(25 分作業 → 5 分休憩)を共有できる Discord ポモドーロ Bot。スラッシュコマンドは **`/pomo` の 1 個だけ**、残りはパネルのボタンで操作します。

利用者向けの最小ガイド: [USAGE.md](USAGE.md)。

---

## 使い方

### はじめかた

1. テキストチャンネルで `/pomo` → コントロールパネルが出る
2. **🙋 参加** を押す
3. **▶️ 開始** を押す(押した人がオーナーになる)

あとは自動で作業 → 休憩を繰り返す。同じサーバの別チャンネルで `/pomo` を立てれば、別グループとして並行して走らせられます。

### パネルのボタン

```
[🙋 参加] [🚪 退出] [✍️ タスク] [📊 統計] [❓ 使い方]
[▶️ 開始]  [⚙️ 時間設定] [🔔 メンション] [🔊 ボイス] [🛑 終了]
```

**誰でも(上段)**
- **🙋 参加 / 🚪 退出**
- **✍️ タスク** — 自分の作業を書く(他の人にも見える)
- **📊 統計** — 自分の完了数(今日 / 今週 / 累計)
- **❓ 使い方** — ヘルプを表示

**オーナー専用(下段)**
- **▶️ 開始 / 🛑 終了**
- **⚙️ 時間設定** — 作業 / 休憩の長さや長休憩頻度
- **🔔 メンション** — フェーズ開始時に参加者にスポイラー付きメンション(フェーズ別 ON/OFF)
- **🔊 ボイス** — オーナーが入っている VC に Bot を呼んで音声合図(下記)

各フェーズ境界では新しい **フェーズパネル** がチャンネルに投稿され、`████░░░░ 5分 / 25分` のプログレスバーが 1 分おきに更新、終了予定がライブカウントダウンします。途中参加・操作は ✅ 参加 / 操作 / 🛑 終了 で。

### ボイス機能

🔊 ボイス を押すと Bot が VC に参加し、各フェーズ境界 / 残り 1 分 / 終了などで音声合図(**VOICEVOX:ずんだもん**)を流します。再押下で抜けます。

> ⚠️ **同じサーバ内ではボイスは同時に 1 ルームまで** — 別チャンネルがボイス使用中なら、空くまで待つかボイス無しで使ってください(タイマーやメンションは動きます)。`DISCORD_TOKENS` で複数 Bot 並走させると回避できます。

### こまかい挙動

- 同じチャンネルで `/pomo` をやり直すと、前のセッションは置き換わる(1 チャンネル = 1 ポモドーロ)
- 1 人につき同時に 1 つのポモドーロにしか参加できない
- オーナーが抜けたら最古参加メンバーに自動でオーナー権が移譲、全員抜けたら自動終了
- Bot が再起動すると進行中のセッションは終了 → `/pomo` で作り直し

---

## デプロイ

### 1. Discord Bot を用意

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** → **Bot** タブで **Reset Token**
2. **OAuth2 → URL Generator** で招待 URL を生成:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**: `Send Messages`, `Embed Links`, `Read Message History`(ボイス使うなら + `Connect`, `Speak`)
3. **Privileged Gateway Intents** は不要

### 2. ローカルで起動

```bash
cp .env.example .env  # .env の DISCORD_TOKEN を埋める
docker compose up --build
```

開発中は `.env` に `DISCORD_GUILD_IDS=<サーバ ID>` を書くとコマンドが即時同期されます(グローバル同期は最大 1 時間)。

### 3. Railway

1. PostgreSQL プラグインを追加
2. Bot サービスの環境変数に `DISCORD_TOKEN` を設定。`DATABASE_URL` は **`${{ Postgres.DATABASE_URL }}`** を明示的に張る
3. Push すると [railway.toml](railway.toml) → [Dockerfile](Dockerfile) で起動(`alembic upgrade head && python -m src.main`)

## 環境変数

| 変数 | 既定 | 説明 |
|---|---|---|
| `DISCORD_TOKEN` / `DISCORD_TOKENS` | — | 単一トークン または CSV で複数 Bot を 1 プロセス並走 |
| `DISCORD_GUILD_IDS` | 空 | カンマ区切りの guild ID。指定すると即時コマンド同期 |
| `DATABASE_URL` | localhost の pomodoro | `postgres://` / `postgresql://` / `+asyncpg` 全部受ける |
| `POMO_WORK_SECONDS` | `1500` | 作業フェーズ秒数(既定 25 分) |
| `POMO_SHORT_BREAK_SECONDS` | `300` | 短休憩 |
| `POMO_LONG_BREAK_SECONDS` | `900` | 長休憩 |
| `POMO_LONG_BREAK_EVERY` | `4` | N 回の作業で長休憩 |
| `POMO_REFRESH_MINUTES` | `1` | バー更新間隔(分、最小 1) |
| `LOG_LEVEL` | `INFO` | logging レベル |

`DISCORD_TOKEN` と `DISCORD_TOKENS` のどちらか一方を指定。両方ある場合は `DISCORD_TOKENS` 優先。

---

## 設計メモ

### ディレクトリ

```
src/
├── main.py              # N 個の Bot を gather + signals + DB 接続リトライ
├── bot.py               # PomodoroBot + /pomo ハンドラ + 起動時の孤立ルーム掃除
├── config.py            # pydantic-settings
├── room_manager.py      # 複数ルーム状態 + phase-end based loop + 音声 hook
├── voice_manager.py     # ギルド単位の VC 接続 / play / disconnect
├── core/                # DB 非依存ロジック(Phase, RoomState)
├── database/            # engine + models
├── services/            # AsyncSession を受ける async 関数群
└── ui/                  # embeds + panel views
voices/                  # .wav 音声合図
alembic/versions/…       # マイグレーション
```

### DB スキーマ

| テーブル | 役割 | 主要な制約 |
|---|---|---|
| `pomodoro_rooms` | チャンネルに紐づく共有タイマー | `channel_id` の部分 UNIQUE INDEX (`WHERE ended_at IS NULL`)。`bot_user_id` でマルチ Bot のスコープ |
| `room_participants` | 参加履歴 (append-only) | `(room_id, user_id)` の部分 UNIQUE で二重参加防止 |
| `pomodoros` | 完了 🍅 を 1 人 1 行で記録 | stats 用に `(user_id, completed_at)` |
| `room_events` | ライフサイクル outbox | `phase_completed` / `notify_updated` 等を tail 可 |

タイマー状態は memory 保持(再起動で `bot_restart` クローズ)、ルームは setup → running の 2 相、wake_event ベースの phase loop で 10 秒ティック無し。

### 音声(VOICEVOX:ずんだもん)

`voices/*.wav` を `🔊` 接続中のみ再生。同梱クリップ: `start` / `end` / `auto-end` / `alarm` / `connected` / `start-break` / `end-break` / `start-long-break` / `end-long-break` / `pause` / `resume` / `one-minute-left`。WORK→休憩 は `alarm + start-X`、休憩→WORK は `end-X` 単発のミニマル設計。

クレジット: [VOICEVOX](https://voicevox.hiroshiba.jp/) / [VOICEVOX:ずんだもん](https://zunko.jp/)。

---

## 開発

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' yamllint

# CI と同じ
ruff format --check .
ruff check src tests
mypy src
yamllint -s .
pytest                  # DB 必須テストは接続不可なら skip
```

DB 有りで:
```bash
docker compose up -d db
DISCORD_TOKEN=test \
DATABASE_URL='postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro_test' \
  alembic upgrade head && pytest
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)): lint(ruff / mypy / yamllint) + test(`postgres:17-alpine` でマイグレ → pytest + coverage)。

スキーマ変更:
```bash
alembic revision -m "add foo" --autogenerate
alembic upgrade head
```

## 技術スタック

Python 3.12 / discord.py[voice] >=2.7 + davey(DAVE E2EE プロトコル — 無いと voice gateway が `4017` で切る)/ ffmpeg + libopus + libsodium / PostgreSQL 17 / SQLAlchemy 2.x async + asyncpg / Alembic / pydantic-settings / Docker Compose / Railway
