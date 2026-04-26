# Discord Pomodoro Bot

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Discord 上の複数チャンネルで同時に走る共同ポモドーロ Bot。
スラッシュコマンドは **`/pomo` の 1 個だけ** で、残りはすべてパネルのボタンから操作します。

## UI の 2 サーフェス

**1. Control Panel(常駐)** — `/pomo` を叩くと置かれる常設メッセージ。時間設定・参加者一覧・起動前/後の状態を表示。

```
🎛 ポモドーロ コントロールパネル
**未開始** — オーナーが ▶️ 開始 を押すと始まります

⏱ 時間設定
25分 作業 / 5分 短休憩 / 15分 長休憩 × 4 サイクルで長休憩

👥 参加者 (3)
👑 @alice — 数学
•  @bob   — 英語
•  @carol — —

[🙋 参加] [🚪 退出] [✍️ タスク] [📊 統計] [❓ 使い方]
[▶️ 開始]  [⚙️ 時間設定] [🔔 メンション] [🔊 ボイス] [🛑 終了]   ← オーナー専用
```

**2. Phase Panel(毎フェーズ投稿)** — ▶️ 開始 後、フェーズ境界(自然終了 / スキップ / プラン更新)でチャンネルに**新しいメッセージ**が届く。ASCII プログレスバーが **`POMO_REFRESH_MINUTES` 分おきに edit で更新**(既定 1 分)されてバーが伸びていく。終了までの残り時間は Discord の `<t:…:R>` で**クライアント側が秒単位でライブカウントダウン**。🔔 メンションが ON のフェーズでは先頭に**スポイラー付きメンション**(`||<@uid> …||`)が乗り、ping を発火しつつユーザ名は隠す。

```
||<@111> <@222>||        ← 🔔 メンション ON のフェーズだけ。クリックで展開、通知は発火済み
⏰ **作業**
`████░░░░░░░░░░░░░░░░ 5分 / 25分`
終了 <t:1735689600:R>    ← "in 20 minutes" のようにクライアントでライブ更新

[✅ 参加] [操作] [🛑 終了]
```

一時停止中(タイムスタンプは消える — 未来時刻のカウントダウンは pause 無視で進み続けるため):

```
⏰ **作業** ⏸ **一時停止中**
`████░░░░░░░░░░░░░░░░ 5分 / 25分`
```

フェーズが終わって次のメッセージが来ると、古い Phase Panel はボタンが剥がされ、`<t:…:R>` 行も除去される(Discord は過去時刻の `<t:…:R>` も「X 分前」と再レンダリングし続けるので、明示的に凍結)。

`操作` を押すと ephemeral サブメニュー(`⏸ 一時停止` / `⏭ スキップ` / `🔄 リセット` / `⚙️ 時間設定`)が開く — オーナーだけが押せる(他は disabled 表示)。

## 特徴

- **Setup / Running の 2 相ライフサイクル** — `/pomo` で作っただけではタイマーは動かない(時間設定を調整できる)。オーナーが `▶️ 開始` を押した瞬間から計時開始
- **マルチルーム / マルチ参加者** — 1 チャンネルに 1 ルーム、複数チャンネルを同時並走。1 ユーザーは同時 1 ルームまで(別チャンネルで参加すると旧ルームから自動退出)
- **参加者ごとの個別タスク** — `✍️ タスク` でモーダルを開いて自分だけのタスクを編集。WORK 完了時、その瞬間の参加者全員の `pomodoros` テーブルに 1 行ずつ記録
- **オーナー制御** — 一時停止 / スキップ / リセット / 終了 / 時間設定はルーム作成者のみ。オーナー退出で最古残留メンバーへ自動委譲、全員抜けたら `auto_empty` で自動終了
- **ボイスチャンネル合図** — `🔊 ボイス` でオーナーの VC に Bot を接続、フェーズ境界 / 残り 1 分 / pause-resume / 終了で `voices/*.wav` を再生。WORK→休憩 は `alarm` + 1 クリップ、休憩→WORK は 1 クリップだけのミニマル設計。VC 未接続なら全パスを no-op で素通り
- **マルチ Bot 設計** — `DISCORD_TOKENS` (CSV) で複数 Bot を 1 プロセスから並走。`bot_user_id` カラムで起動時 reconciliation を相互干渉なくスコープ。Discord の「1 ギルド 1 VC」制約を別 Bot で回避できる
- **Phase-end based loop** — 10 秒毎の tick は廃止。`wait_for(wake_event, timeout=remaining)` で自然終了を待つ。pause/skip/reset/update_plan は wake event を set して loop を起こし、次の sleep を計算し直す。残り 60 秒の合図用に sleep 時間も自動で詰める
- **永続 View** — 各ボタンの `custom_id` にルーム UUID を埋め込み、複数ルーム並走でも dispatch が衝突しない
- **再起動時のポリシー** — 実行中ルームは `bot_restart` で DB 側をクローズ(自分の `bot_user_id` 配下のみ)、古いパネルメッセージはボタンを剥がして「`/pomo` で作り直して」と案内
- **PostgreSQL + Alembic** — スキーマ変更はマイグレーションで管理、起動時に `alembic upgrade head` が自動で走る

### Control Panel のボタン

| ボタン | 権限 | 動作 |
|---|---|---|
| 🙋 参加 | 誰でも | そのルームに入る。別ルーム参加中なら自動で抜ける |
| 🚪 退出 | 参加者 | 抜ける。オーナーなら権限委譲、最後の 1 人なら自動終了 |
| ✍️ タスク | 参加者 | モーダルで自分のタスクを編集 |
| 📊 統計 | 誰でも | 自分の今日/今週/累計完了数を ephemeral 表示 |
| ❓ 使い方 | 誰でも | 全ボタンの役割を ephemeral embed で表示 |
| ▶️ 開始 | オーナー | タイマー開始(setup → running)。起動後は `開始中` で disabled |
| ⚙️ 時間設定 | オーナー | 作業/休憩時間と長休憩頻度をモーダルで変更(ラウンド最初から再開) |
| 🔔 メンション | オーナー | フェーズ別(作業/短休憩/長休憩)にスポイラー付きメンションの ON/OFF を切替。既定は全 ON |
| 🔊 ボイス | オーナー | オーナーが入っている VC に Bot を接続(`connected.wav`)。再押下で切断。VC 未参加で押すと「先に VC に入ってください」 |
| 🛑 終了 | オーナー | ルームを明示的に終了 |

### Phase Panel のボタン

| ボタン | 権限 | 動作 |
|---|---|---|
| ✅ 参加 | 誰でも | このラウンドに参加(内部的には `join` と同じ) |
| 操作 | 誰でも | ephemeral でオーナー用サブメニューを開く(他は disabled) |
| 🛑 終了 | オーナー | ルーム終了 |

### ボイス合図 (`voices/*.wav`)

`🔊 ボイス` で接続中のみ再生されます。VC 未接続なら全クリップ no-op、Bot は黙って動き続けます。

音声合成は **VOICEVOX:ずんだもん** で生成しています(`alarm.wav` のみ効果音)。VOICEVOX 規約に従い、非営利・個人利用の範囲で運用してください。

- 音声合成エンジン: [VOICEVOX](https://voicevox.hiroshiba.jp/)(ヒホ)
- キャラクター: [VOICEVOX:ずんだもん](https://zunko.jp/)(東北ずん子・ずんだもんプロジェクト)

| クリップ | 再生タイミング |
|---|---|
| `connected.wav` | 🔊 押下時(VC 接続成功) |
| `start.wav` | ▶️ 開始 直後 |
| `alarm.wav` → `start-break.wav` | WORK 終了 → 短休憩開始 |
| `alarm.wav` → `start-long-break.wav` | WORK 終了 → 長休憩開始 |
| `end-break.wav` | 短休憩 → WORK(これ単発) |
| `end-long-break.wav` | 長休憩 → WORK(これ単発) |
| 上記の単発クリップ | ⏭ スキップ時(WORK ↔ 休憩 で対応する announcement のみ、alarm 無し) |
| `pause.wav` / `resume.wav` | ⏸ 一時停止 / 再開 |
| `one-minute-left.wav` | フェーズ終了 60 秒前(各フェーズ 1 回) |
| `end.wav` | 🛑 終了(オーナー操作) |
| `auto-end.wav` | 全員退出による自動終了 |

各フェーズ境界は `休憩` を主語にして 1 クリップで announce します(`start.wav` が "開始します" を一度だけ言うので、以降の WORK は `start-task.wav` を重ねません。同じ理由で `end-task.wav` も使いません)。`alarm.wav` は **WORK 終了時のみ** — フォーカスを切る瞬間の注意喚起としては効くが、休憩明けは静かに次に行くほうが自然です。

`bot_restart` / `superseded` / `shutdown` / `error` での終了は無音(聞き手がいない / 急ぎたい)。

## 技術スタック

Python 3.12 / discord.py[voice] >=2.7 + davey (Discord の DAVE E2EE プロトコル実装。これが無いと最近の voice 接続は `4017` で切られる) / ffmpeg + libopus + libsodium / PostgreSQL 17 / SQLAlchemy 2.x (async) + asyncpg / Alembic / pydantic-settings / Docker Compose / Railway

## クイックスタート

### 1. Discord Bot を用意する

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** → **Bot** タブで **Reset Token** を取得
2. **OAuth2 → URL Generator** で Invite URL を生成してサーバに招待:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**: `Send Messages`, `Embed Links`, `Read Message History`, `Connect`, `Speak`(`Connect` / `Speak` は `🔊 ボイス` を使うときだけ必要 — テキストのみで運用するなら省略可)
3. **Privileged Gateway Intents** は **不要**(`voice_states` は default に含まれる)

### 2. ローカルで起動

```bash
cp .env.example .env
# .env の DISCORD_TOKEN を 1 で取得したトークンに差し替える
docker compose up --build
```

Discord で `/pomo` を叩くと Control Panel が出ます。必要なら `⚙️ 時間設定` で調整してから `▶️ 開始`。

> 開発中は `.env` に `DISCORD_GUILD_IDS=<サーバ ID>` を書くとコマンドが即座に同期されます(グローバル同期は最大 1 時間)。

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `DISCORD_TOKEN` | — | 単一 Bot トークン。`DISCORD_TOKENS` を使わない場合は必須 |
| `DISCORD_TOKENS` | — | カンマ区切りで複数 Bot のトークン。指定すると 1 プロセスから N 個の Bot が並走(同ギルドで複数ルームに音声を提供したい場合の回避策) |
| `DISCORD_GUILD_IDS` | 空 | カンマ区切りの guild ID。指定すると即時同期、空ならグローバル(最大 1 時間) |
| `DATABASE_URL` | `postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro` | `postgres://` / `postgresql://` / `postgresql+asyncpg://` すべて受け付け |
| `POMO_WORK_SECONDS` | `1500` (25 分) | WORK フェーズの長さ |
| `POMO_SHORT_BREAK_SECONDS` | `300` (5 分) | 短休憩の長さ |
| `POMO_LONG_BREAK_SECONDS` | `900` (15 分) | 長休憩の長さ |
| `POMO_LONG_BREAK_EVERY` | `4` | N 回目の WORK 完了で長休憩へ |
| `POMO_REFRESH_MINUTES` | `1` | Phase メッセージの ASCII バーを何分おきに更新するか(整数分、最小 1) |
| `LOG_LEVEL` | `INFO` | stdlib logging レベル |

`DISCORD_TOKEN` と `DISCORD_TOKENS` のどちらか一方を指定。両方ある場合は `DISCORD_TOKENS` が優先。

## Railway デプロイ

1. Railway プロジェクトに **PostgreSQL** プラグインを追加
2. Bot サービスの環境変数に `DISCORD_TOKEN` を設定。`DATABASE_URL` は **`${{ Postgres.DATABASE_URL }}`** を明示的に張る(Railway はデフォルトではサービス間で環境変数を共有しない)
3. このリポジトリを接続すると [railway.toml](railway.toml) に従って Dockerfile でビルドされ、[Dockerfile](Dockerfile) の `CMD` (`alembic upgrade head && python -m src.main`) で起動

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `/pomo` がコマンド一覧に出てこない | グローバル同期は最大 1 時間かかる。開発中は `DISCORD_GUILD_IDS` を設定してそのサーバに即時同期 |
| Phase Panel が送れない / 更新されない | Bot に **Send Messages** / **Embed Links** / **Read Message History** 権限があるか確認 |
| ボタンを押すと "This interaction failed" | Bot 再起動直後の古い Panel の可能性。新しく `/pomo` を叩き直せば案内メッセージに置き換わる |
| `/pomo` を叩いても反応しない | `Unknown interaction (10062)` が典型。DB が cold で 3s を越えているか、`DISCORD_TOKEN` が未設定 |
| 🔊 押しても音が出ない | (1) Bot に `Connect` / `Speak` 権限があるか (2) コンテナ / 開発環境に `ffmpeg` が入っているか (3) `voices/*.wav` が同梱されているか (Railway の場合 Dockerfile が COPY しているか) |
| voice ログに `WebSocket closed with 4017` | Discord の DAVE プロトコル未対応で voice gateway に拒否されています。`discord.py[voice]>=2.7` + `davey>=0.1.0` が入っているか確認(古い 2.6.x では DAVE 未対応で必ず失敗) |
| 🔊 押すと「先に VC に入ってください」 | オーナー自身が Discord のボイスチャンネルに入った状態で押す必要があります |
| 同ギルドで 2 つ目のルームを音声付きで使いたい | Discord は Bot 1 体につき 1 ギルド 1 VC が上限。2 体目以降の Bot を `DISCORD_TOKENS` に追加して、別の Bot で `/pomo` を叩く |
| Railway でデプロイしても `localhost:5432 refused` | `DATABASE_URL` が注入されていない。Variables で `${{ Postgres.DATABASE_URL }}` を明示的に張る |

## 設計

レイヤ分離は `bot.py` のコマンドハンドラ薄め / `services/` が `AsyncSession` を受ける async 関数の集合 / `core/` に DB 非依存の純ロジック。Repository クラスは作らず、`async with async_session() as session:` で都度セッションを開いて関数に渡します。

### ディレクトリ

```
src/
├── main.py              # エントリ(N 個の Bot を gather + SIGINT/SIGTERM + DB 接続リトライ)
├── bot.py               # PomodoroBot + /pomo ハンドラ + 起動時の孤立ルーム掃除(自分の bot_user_id 配下のみ)
├── config.py            # pydantic-settings、module-level `settings` (DISCORD_TOKEN 単発 / DISCORD_TOKENS CSV 両対応)
├── constants.py         # 色 / 既定サイクル / VOICES_DIR
├── room_manager.py      # 複数ルーム状態 + phase-end based loop + 音声 hook。業務結果は `OpResult` で返す
├── voice_manager.py     # ギルド単位の VC 接続 / play / disconnect。クリップは voices/<name>.wav
├── core/                # DB 非依存の純ロジック
│   ├── phase.py         #   Phase enum / PhasePlan / next_phase
│   └── room_state.py    #   RoomState / ParticipantState / has_started / wake_event / one_minute_cue_played
├── database/            # engine.py (async_session) + models.py
├── services/            # AsyncSession を受ける async 関数群
│   └── room_service.py  #   create/join/leave/task/end/record_pomodoros/stats(bot_user_id スコープ対応)
└── ui/                  # Discord UI
    ├── embeds.py        #   control_panel_embed / ended_embed / help_embed / phase_content / freeze_phase_content
    └── panel_views.py   #   ControlPanelView + PhasePanelView + OptionsView + NotificationSettingsView + modals
voices/                  # .wav 音声合図(start / end / alarm / pause / resume / one-minute-left / connected / start-break / end-break / start-long-break / end-long-break / auto-end)
alembic/versions/…       # DB マイグレーション
tests/                   # src/ と対のツリー
```

### DB スキーマ

| テーブル | 役割 | 主要な整合性 |
|---|---|---|
| `pomodoro_rooms` | チャンネルに紐づく共有タイマー | `channel_id` に部分 UNIQUE INDEX (`WHERE ended_at IS NULL`) で 1 チャンネル 1 アクティブ。`bot_user_id` カラム + 部分 INDEX で起動時 reconciliation を Bot ごとにスコープ |
| `room_participants` | 誰がいつから / いつまで参加したかの append-only ログ | `(room_id, user_id)` の部分 UNIQUE INDEX (`WHERE left_at IS NULL`) で二重参加防止 |
| `pomodoros` | 完了 🍅 を 1 人 1 行で記録 | `(user_id, completed_at)` にインデックス(stats 用) |
| `room_events` | ライフサイクルの append-only outbox | `phase_completed` / `ownership_transferred` / `timer_started` / `phase_skipped` / `notify_updated` 等を tail 可 |

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
- [tests/services/test_room_service.py](tests/services/test_room_service.py) — create/end/join/leave/task/record_pomodoros/stats の DB 往復、`mark_all_active_rooms_ended` の `bot_user_id` スコープ
- [tests/ui/test_embeds.py](tests/ui/test_embeds.py) — Control Panel embed / Phase message の ASCII バー成長 / 一時停止・完了時のタイムスタンプ挙動 / スポイラーメンション / 終了理由の日本語化 / `freeze_phase_content`
- [tests/ui/test_panel_views.py](tests/ui/test_panel_views.py) — Control Panel(10 ボタン) / Phase Panel(3 ボタン) / モーダル / `NotificationSettingsView` のラベル同期 / 使い方 embed / オーナーガード / 入力バリデーション
- [tests/test_config.py](tests/test_config.py) — URL 正規化 / CSV パース / `DISCORD_TOKEN` 単発 → `DISCORD_TOKENS` 昇格 / 両方未設定で raise
- [tests/test_voice_manager.py](tests/test_voice_manager.py) — VC 接続 / 移動 / 切断 / 一括切断 / 再生中の差し替え / 未接続時の no-op / `connect()` の short timeout / 接続失敗時の partial VoiceClient 強制切断
- [tests/test_room_manager.py](tests/test_room_manager.py) — setup → running 遷移、begin_phases ガード、自然タイムアウトの phase-end path、マルチルーム独立、並行デッドロック回避、オーナー委譲、参加者別 🍅 記録、per-phase メンショントグル、🔊 ボイストグル(owner / 非 VC / 接続失敗 / 別ルームのギルド占有)、フェーズ境界の音声合図(WORK→休憩は alarm + start-X、休憩→WORK は end-X 単発、long-break、pause-resume、skip、one-minute-left、自動終了)、`bot_user_id` 永続化、`IntegrityError` 経路

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

- **lint** — ruff format / ruff check / mypy / yamllint
- **test** — `postgres:17-alpine` サービスコンテナで `alembic upgrade head` → pytest + coverage

## 拡張ポイント

| やりたいこと | どこを触るか |
|---|---|
| 統計グラフ画像 | `pomodoros` に `room_id` / `user_id` / `duration_seconds` / `completed_at` が揃っている。`services/room_service.py` にクエリを足し、matplotlib 等で描画して Phase Panel の `操作` 経由で配信 |
| 動的な音声合成(VOICEVOX 等) | 現状は事前録音 `voices/*.wav`。`VoiceManager.play_clip` を「クリップ名 → 音声ストリーム」に拡張すれば差し替え可。`room_events` を tail して別プロセスで合成 + 配信もアリ |
| ユーザー / ルーム別カスタムサイクル | `PhasePlan` を DB 化。`RoomManager` 生成時に差し替えられる形へ拡張 |
| 再起動時のタイマー復元 | いまは `bot_restart` で閉じるだけ。`pomodoro_rooms` に `phase` / `phase_started_at` / `paused_accumulated_seconds` を永続化すれば resume 可能 |
| アチーブメント / ストリーク | `pomodoros.completed_at` を日付で bucket して連続日数を算出。`room_events` に `achievement_unlocked` を書く別プロセスでも可 |
