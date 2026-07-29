# DMX → MIDI Bridge

Bridges DMX512 input — either an **Enttec DMX USB Pro** widget (serial/USB) or
**Art-Net** (DMX over UDP) — to **MIDI note on/off** output.

日本語の説明は[このページの下部](#dmx--midi-ブリッジ)にあります。

## What it does

- Reads DMX512 data from either:
  - an Enttec DMX USB Pro widget over USB (serial), or
  - Art-Net (ArtDMX UDP packets) on the network
- For each DMX channel you map in `config.yaml`:
  - value changes to **> 0** → sends **Note On**, velocity scaled from the
    DMX value (0-255 → 1-127, never 0 while "on")
  - value changes to **0** → sends **Note Off**
- Outputs to a virtual ALSA MIDI port (for testing/DAWs) or a real USB MIDI
  interface.
- Optional live curses dashboard (`--ui`) showing all 512 channels or just
  the mapped ones, plus a recent MIDI event log.

## Requirements

- Python 3, a virtualenv is included in `venv/` with dependencies already
  installed (`pyserial`, `mido`, `python-rtmidi`, `PyYAML`). To reinstall:

  ```bash
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt
  ```

## Configuration (`config.yaml`)

```yaml
input:
  mode: usb        # "usb" (Enttec DMX USB Pro) or "artnet"

serial:             # used when input.mode: usb
  port: /dev/ttyUSB0
  baudrate: 57600    # dummy value for the widget's virtual COM port

artnet:              # used when input.mode: artnet
  bind_address: 0.0.0.0
  port: 6454
  universe: 0         # Net<<8 | SubNet<<4 | Universe

midi:
  output: virtual              # "virtual", or a substring of a real port name
  virtual_port_name: "DMX2MIDI"

mappings:
  - dmx_channel: 1    # 1-512
    midi_channel: 1   # 1-16
    note: 60          # 0-127

  # Range form: expands to one mapping per channel, with notes assigned
  # consecutively starting at note_start. This maps channel 1->note 60,
  # channel 2->note 61, ..., channel 16->note 75.
  - dmx_channel_start: 1
    dmx_channel_end: 16
    midi_channel: 1
    note_start: 60
```

Add as many entries under `mappings:` as you need. Each is independent, and
you can freely mix single mappings and range mappings in the same list.

## Usage

```bash
# find your Enttec USB device path
./venv/bin/python dmx_midi_bridge.py --list-serial-ports

# find MIDI output ports (to target a real USB MIDI interface instead of virtual)
./venv/bin/python dmx_midi_bridge.py --list-midi-ports

# run the bridge
./venv/bin/python dmx_midi_bridge.py --config config.yaml

# with the live dashboard
./venv/bin/python dmx_midi_bridge.py --config config.yaml --ui

# test without any hardware/QLC+ (generates a synthetic DMX ramp)
./venv/bin/python dmx_midi_bridge.py --config config.yaml --simulate
```

### CLI flags

| Flag | Description |
|---|---|
| `--config PATH` | Config file to use (default `config.yaml`) |
| `--list-serial-ports` | List serial ports and exit |
| `--list-midi-ports` | List MIDI output ports and exit |
| `--simulate` | Generate a synthetic DMX ramp instead of reading real input (for testing the MIDI/UI side without hardware) |
| `--verbose` | Debug logging: logs every distinct received DMX frame and every MIDI message sent |
| `--ui` | Live curses dashboard instead of scrolling console logs |
| `--log-file PATH` | Where logs go when `--ui` is active (default `dmx_midi_bridge.log`), since the dashboard owns the terminal |

Stop the bridge with `Ctrl+C` (or `q` while `--ui` is active).

### Switching USB ↔ Art-Net

Just change `input.mode` in `config.yaml` between `usb` and `artnet` and
restart the bridge. No code changes needed.

### The `--ui` dashboard

- Header: input mode, MIDI output port, frame rate, uptime.
- **All-channels view** (default): a scrollable grid of all 512 DMX
  channels. Mapped channels are highlighted (yellow = mapped & on, cyan =
  mapped & off, green = unmapped & on).
  - `↑`/`↓`, `PageUp`/`PageDown`, `Home` — scroll
- **Mapped view**: a table of just the mapped channels (value, scaled
  velocity, on/off state) plus a log of recent MIDI events.
- `a` — toggle between the two views.
- `q` — quit.

Since `--ui` takes over the terminal, run it inside `tmux`/`screen` if
you're connecting over SSH — that way the bridge keeps running even if the
SSH connection drops:

```bash
tmux new -s dmxbridge
./venv/bin/python dmx_midi_bridge.py --config config.yaml --ui
# Ctrl-b d to detach; tmux attach -t dmxbridge to come back
```

### Known limitation: no signal-loss timeout

The bridge only updates state when a new DMX frame actually arrives. If the
sender (e.g. QLC+) stops transmitting entirely (rather than sending an
all-zero frame) — for example when using QLC+'s Blackout — the bridge has
no way to know and will keep holding the last received values instead of
turning notes off. Art-Net's own spec calls this per-node behavior
"Failsafe state" and explicitly lists "all outputs to zero" as one option,
but implementing it is optional and not something senders guarantee. This
bridge does not currently implement a receive-timeout fallback.

---

# DMX → MIDI ブリッジ

DMX512の入力(**Enttec DMX USB Pro**ウィジェット、または**Art-Net**)を
**MIDIのNote On/Off**出力に変換するブリッジです。

## 何をするか

- 以下のいずれかからDMX512データを受信します。
  - Enttec DMX USB ProウィジェットをUSB(シリアル)経由で
  - Art-Net(ArtDMX UDPパケット)をネットワーク経由で
- `config.yaml`でマッピングした各DMXチャンネルについて、
  - 値が変化して**0より大きく**なった → **Note On**を送信(velocityはDMX値0-255を1-127にスケーリング、ON中は0にならない)
  - 値が変化して**0**になった → **Note Off**を送信
- 出力先は仮想ALSA MIDIポート(テスト・DAW用)、または実際のUSB MIDIインターフェース。
- ライブダッシュボード(`--ui`)で全512チャンネル、またはマッピング済みチャンネルのみを表示可能。

## 必要なもの

- Python 3。依存関係(`pyserial`, `mido`, `python-rtmidi`, `PyYAML`)は`venv/`に導入済みです。再インストールする場合は次の通り。

  ```bash
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt
  ```

## 設定(`config.yaml`)

```yaml
input:
  mode: usb        # "usb"(Enttec DMX USB Pro) または "artnet"

serial:             # input.mode: usb の時に使用
  port: /dev/ttyUSB0
  baudrate: 57600    # ウィジェットの仮想COMポート用のダミー値

artnet:              # input.mode: artnet の時に使用
  bind_address: 0.0.0.0
  port: 6454
  universe: 0         # Net<<8 | SubNet<<4 | Universe

midi:
  output: virtual              # "virtual"、または実ポート名の一部
  virtual_port_name: "DMX2MIDI"

mappings:
  - dmx_channel: 1    # 1-512
    midi_channel: 1   # 1-16
    note: 60          # 0-127

  # レンジ指定：チャンネルごとに1つずつ展開され、note_startから連番でノートが
  # 割り当てられます。この例ではch1→note60, ch2→note61, ..., ch16→note75。
  - dmx_channel_start: 1
    dmx_channel_end: 16
    midi_channel: 1
    note_start: 60
```

`mappings:`の下に必要な数だけエントリを追加できます。それぞれ独立して動作し、単発指定とレンジ指定は同じリスト内で自由に混在できます。

## 使い方

```bash
# EnttecのUSBデバイスパスを確認
./venv/bin/python dmx_midi_bridge.py --list-serial-ports

# MIDI出力ポートを確認(仮想ではなく実機のUSB MIDIを使う場合)
./venv/bin/python dmx_midi_bridge.py --list-midi-ports

# ブリッジを起動
./venv/bin/python dmx_midi_bridge.py --config config.yaml

# ライブダッシュボード付きで起動
./venv/bin/python dmx_midi_bridge.py --config config.yaml --ui

# 実機/QLC+なしでテスト(疑似DMXランプ波形を生成)
./venv/bin/python dmx_midi_bridge.py --config config.yaml --simulate
```

### CLIオプション

| オプション | 説明 |
|---|---|
| `--config PATH` | 使用する設定ファイル(デフォルト`config.yaml`) |
| `--list-serial-ports` | シリアルポート一覧を表示して終了 |
| `--list-midi-ports` | MIDI出力ポート一覧を表示して終了 |
| `--simulate` | 実際の入力の代わりに疑似DMXランプを生成(実機なしでMIDI/UI側をテストする用) |
| `--verbose` | デバッグログ有効化。受信した個々のDMXフレームと送信した全MIDIメッセージを記録 |
| `--ui` | コンソールへの流れ続けるログの代わりに、ライブcursesダッシュボードを表示 |
| `--log-file PATH` | `--ui`使用時のログ出力先(デフォルト`dmx_midi_bridge.log`)。ダッシュボードが端末を占有するため |

`Ctrl+C`(または`--ui`使用中は`q`)で停止します。

### USB ⇄ Art-Netの切り替え

`config.yaml`の`input.mode`を`usb`と`artnet`の間で書き換えて再起動するだけです。コードの変更は不要です。

### `--ui`ダッシュボードについて

- ヘッダー：入力モード、MIDI出力ポート、フレームレート、稼働時間
- **全チャンネル表示**(デフォルト)：全512 DMXチャンネルのスクロール可能なグリッド。マッピング済みチャンネルは色分け表示(黄=マッピング済み&点灯、シアン=マッピング済み&消灯、緑=未マッピング&点灯)
  - `↑`/`↓`、`PageUp`/`PageDown`、`Home` — スクロール
- **マッピング済み表示**：マッピングしたチャンネルのみのテーブル(値・velocity・ON/OFF状態)と、直近のMIDIイベント履歴
- `a` — 2つの表示を切り替え
- `q` — 終了

`--ui`は端末を占有するため、SSH経由で使う場合は`tmux`/`screen`の中で起動するのがおすすめです。SSH接続が切れてもブリッジ自体は動き続けます。

```bash
tmux new -s dmxbridge
./venv/bin/python dmx_midi_bridge.py --config config.yaml --ui
# Ctrl-b d でデタッチ、tmux attach -t dmxbridge でいつでも復帰
```

### 既知の制限：受信途絶時のタイムアウトがない

このブリッジは、新しいDMXフレームが実際に届いた時だけ状態を更新します。送信元(QLC+など)が(全チャンネル0のフレームを送るのではなく)**送信そのものを完全に停止**した場合——たとえばQLC+のBlackoutを使った時——ブリッジ側にはそれを知る手段がなく、最後に受信した値を保持し続けてしまいます(Note Offになりません)。Art-Netの規格自体はノード側のこの挙動を「Failsafe state」と呼び、「全出力を0にする」を選択肢の一つとして明示していますが、実装は任意であり、送信側がそれを保証するものでもありません。このブリッジは現状、受信タイムアウトによるフォールバックを実装していません。
