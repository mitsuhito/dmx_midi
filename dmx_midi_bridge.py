#!/usr/bin/env python3
"""Bridge: DMX512 input (Enttec DMX USB Pro or Art-Net) -> MIDI note on/off output.

USB protocol reference: Enttec "DMX USB Pro API" v1.44.
Frame format: 0x7E, label, len_lsb, len_msb, data[len], 0xE7
Label 5 = "Received DMX Packet", sent unsolicited by the widget whenever
it receives DMX on its input port (this is the widget's default mode, so
no configuration request needs to be sent first).

Art-Net input listens for ArtDMX (OpCode 0x5000) UDP packets on port 6454.
"""

import argparse
import collections
import curses
import logging
import queue
import random
import socket
import threading
import time
from dataclasses import dataclass

import mido
import serial
import serial.tools.list_ports
import yaml

logger = logging.getLogger("dmx_midi_bridge")

FRAME_START = 0x7E
FRAME_END = 0xE7
LABEL_RECEIVED_DMX_PACKET = 5


class EnttecDMXReader(threading.Thread):
    """Reads DMX frames from an Enttec DMX USB Pro over serial and invokes
    on_frame(channels: bytes) for each valid received packet. channels[0]
    is DMX channel 1."""

    def __init__(self, port, baudrate, on_frame):
        super().__init__(daemon=True)
        self._port_name = port
        self._baudrate = baudrate
        self._on_frame = on_frame
        self._stop_event = threading.Event()
        self._ser = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        self._ser = serial.Serial(self._port_name, self._baudrate, timeout=1)
        logger.info("Opened serial port %s", self._port_name)
        buf = bytearray()
        try:
            while not self._stop_event.is_set():
                chunk = self._ser.read(1024)
                if chunk:
                    buf.extend(chunk)
                    self._consume(buf)
        finally:
            self._ser.close()

    def _consume(self, buf):
        while True:
            start = buf.find(FRAME_START)
            if start == -1:
                buf.clear()
                return
            if start > 0:
                del buf[:start]
            if len(buf) < 4:
                return
            length = buf[2] | (buf[3] << 8)
            frame_size = 4 + length + 1
            if len(buf) < frame_size:
                return
            if buf[4 + length] != FRAME_END:
                del buf[0:1]  # resync: this wasn't really a frame start
                continue
            label = buf[1]
            data = bytes(buf[4:4 + length])
            del buf[:frame_size]
            if label == LABEL_RECEIVED_DMX_PACKET:
                self._handle_dmx_packet(data)

    def _handle_dmx_packet(self, data):
        if len(data) < 2:
            return
        status = data[0]
        if status != 0:
            logger.warning("DMX receive status error: 0x%02x", status)
            return
        # data[1] is the DMX start code (usually 0x00); channels follow it
        self._on_frame(data[2:])


ARTNET_HEADER = b"Art-Net\x00"
ARTNET_OP_DMX = 0x5000
ARTNET_OP_SYNC = 0x5200
ARTNET_OP_POLL = 0x2000
ARTNET_OP_POLL_REPLY = 0x2100
ARTNET_SYNC_TIMEOUT = 4.0  # seconds; per spec, revert to non-sync mode after this
ARTNET_STYLE_NODE = 0x00


class ArtNetReceiver(threading.Thread):
    """Listens for Art-Net ArtDMX UDP packets and invokes on_frame(channels)
    for packets matching the configured universe. channels[0] is DMX
    channel 1 (Art-Net DMX data has no leading start code, unlike the
    Enttec USB frame).

    Also supports:
    - ArtSync (0x5200): a node starts in non-synchronous mode (ArtDmx
      applied immediately on receipt). Once it receives an ArtSync packet,
      it switches to synchronous mode: subsequent ArtDmx is buffered and
      only applied when the next ArtSync arrives. If no ArtSync is
      received for 4+ seconds, it reverts to non-synchronous mode. An
      ArtSync is ignored if it comes from a different source IP than the
      most recent ArtDmx (multi-controller guard, per spec).
    - ArtPoll (0x2000): replies with a unicast ArtPollReply (0x2100) so
      this bridge shows up as a discoverable node in QLC+/OLA/etc,
      honoring Targeted Mode's Port-Address range if set. Per spec, the
      reply is sent after a random 0-1s delay to avoid reply storms; this
      is scheduled on a timer rather than blocking the receive loop.
    """

    def __init__(self, on_frame, bind_address="0.0.0.0", port=6454, universe=0):
        super().__init__(daemon=True)
        self._on_frame = on_frame
        self._bind_address = bind_address
        self._port = port
        self._universe = universe
        self._stop_event = threading.Event()
        self._sock = None
        self._sync_mode = False
        self._last_sync_at = 0.0
        self._pending_channels = None
        self._last_dmx_source = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Give the kernel more room to queue bursts (e.g. a fader being
        # dragged quickly) while this thread is busy handling a packet.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._sock.bind((self._bind_address, self._port))
        self._sock.settimeout(1.0)
        logger.info(
            "Listening for Art-Net on %s:%d (universe %d)",
            self._bind_address,
            self._port,
            self._universe,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    packet, addr = self._sock.recvfrom(1024)
                except socket.timeout:
                    self._check_sync_timeout()
                    continue
                self._handle_packet(packet, addr)
        finally:
            self._sock.close()

    def _check_sync_timeout(self):
        if self._sync_mode and (time.time() - self._last_sync_at) > ARTNET_SYNC_TIMEOUT:
            self._sync_mode = False

    def _handle_packet(self, packet, addr):
        if len(packet) < 10 or not packet.startswith(ARTNET_HEADER):
            return
        opcode = packet[8] | (packet[9] << 8)
        if opcode == ARTNET_OP_SYNC:
            self._handle_sync(addr[0])
            return
        if opcode == ARTNET_OP_POLL:
            self._handle_poll(packet, addr)
            return
        if opcode != ARTNET_OP_DMX or len(packet) < 18:
            return
        sub_uni = packet[14]
        net = packet[15]
        universe = (net << 8) | sub_uni
        if universe != self._universe:
            return
        length = (packet[16] << 8) | packet[17]
        channels = packet[18 : 18 + length]
        self._last_dmx_source = addr[0]
        self._check_sync_timeout()
        if self._sync_mode:
            self._pending_channels = channels
        else:
            self._on_frame(channels)

    def _handle_sync(self, source_ip):
        if self._last_dmx_source is not None and source_ip != self._last_dmx_source:
            return
        self._sync_mode = True
        self._last_sync_at = time.time()
        if self._pending_channels is not None:
            self._on_frame(self._pending_channels)
            self._pending_channels = None

    def _handle_poll(self, packet, addr):
        flags = packet[12] if len(packet) > 12 else 0
        targeted = bool(flags & 0x20)
        if targeted and len(packet) >= 18:
            top = (packet[14] << 8) | packet[15]
            bottom = (packet[16] << 8) | packet[17]
            if not (bottom <= self._universe <= top):
                return
        # Random 0-1s delay per spec, to avoid reply bunching; scheduled on
        # a timer so it never blocks this thread from reading more packets.
        delay = random.uniform(0, 1.0)
        threading.Timer(delay, self._send_poll_reply, args=(addr,)).start()

    def _local_ip_for(self, remote_ip):
        """Best-effort: which local IP would the OS use to reach remote_ip?
        Uses a UDP "connect" (route lookup only, sends nothing) so the
        ArtPollReply reports the correct interface on a multi-homed host."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((remote_ip, self._port))
            return s.getsockname()[0]
        except OSError:
            return "0.0.0.0"
        finally:
            s.close()

    @staticmethod
    def _padded(data, size):
        data = data[: size - 1]
        return data + b"\x00" * (size - len(data))

    def _build_poll_reply(self, local_ip):
        ip_bytes = bytes(int(part) for part in local_ip.split("."))
        net_switch = (self._universe >> 8) & 0x7F
        sub_switch = (self._universe >> 4) & 0x0F
        sw_out_low = self._universe & 0x0F

        p = bytearray()
        p += ARTNET_HEADER  # ID[8]
        p += bytes([0x00, 0x21])  # OpCode = OpPollReply, low byte first
        p += ip_bytes  # IP Address[4]
        p += bytes([0x36, 0x19])  # Port = 0x1936, low byte first
        p += bytes([0, 1])  # VersInfoH, VersInfoL
        p += bytes([net_switch, sub_switch])  # NetSwitch, SubSwitch
        p += bytes([0, 0])  # OemHi, Oem(Lo)
        p += bytes([0])  # UbeaVersion
        p += bytes([0])  # Status1
        p += bytes([0, 0])  # EstaManLo, EstaManHi
        p += self._padded(b"DMX2MIDI Bridge", 18)  # PortName[18]
        p += self._padded(b"DMX to MIDI Bridge (dmx_midi_bridge.py)", 64)  # LongName[64]
        p += self._padded(b"#0001 [0001] Ready", 64)  # NodeReport[64]
        p += bytes([0, 1])  # NumPortsHi, NumPortsLo (1 port)
        p += bytes([0x80, 0, 0, 0])  # PortTypes[4]: bit7 set = output-from-Art-Net, DMX512
        p += bytes([0, 0, 0, 0])  # GoodInput[4] (we have no input-to-network port)
        p += bytes([0, 0, 0, 0])  # GoodOutputA[4]
        p += bytes([0, 0, 0, 0])  # SwIn[4]
        p += bytes([sw_out_low, 0, 0, 0])  # SwOut[4]
        p += bytes([100])  # AcnPriority
        p += bytes([0])  # SwMacro
        p += bytes([0])  # SwRemote
        p += bytes([0, 0, 0])  # Spare x3
        p += bytes([ARTNET_STYLE_NODE])  # Style
        p += bytes([0, 0, 0, 0, 0, 0])  # MAC[6] (0 = not supplied)
        p += bytes([0, 0, 0, 0])  # BindIp[4]
        p += bytes([0])  # BindIndex
        p += bytes([0])  # Status2
        p += bytes([0, 0, 0, 0])  # GoodOutputB[4]
        p += bytes([0])  # Status3: failsafe = 00 (hold last state, matches our behavior)
        p += bytes([0, 0, 0, 0, 0, 0])  # DefaultRespUID[6]
        p += bytes([0, 0])  # UserHi, UserLo
        p += bytes([0, 44])  # RefreshRateHi, RefreshRateLo (44Hz)
        p += bytes([0])  # BackgroundQueuePolicy
        p += bytes(10)  # Filler
        return bytes(p)

    def _send_poll_reply(self, addr):
        if self._sock is None:
            return
        try:
            local_ip = self._local_ip_for(addr[0])
            reply = self._build_poll_reply(local_ip)
            self._sock.sendto(reply, addr)
        except OSError:
            logger.exception("Failed to send ArtPollReply to %s", addr)


class DMXSimulator(threading.Thread):
    """Generates a synthetic DMX ramp (0 -> 255 -> 0) on the given channels,
    so the MIDI output side can be exercised without real Enttec hardware."""

    def __init__(self, on_frame, channels, fps=15):
        super().__init__(daemon=True)
        self._on_frame = on_frame
        self._channels = channels
        self._fps = fps
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        logger.info("Simulating DMX input on channel(s) %s", self._channels)
        ramp = list(range(0, 256, 17)) + list(range(255, -1, -17))
        frame = bytearray(512)
        i = 0
        while not self._stop_event.is_set():
            value = ramp[i % len(ramp)]
            for ch in self._channels:
                frame[ch - 1] = value
            self._on_frame(bytes(frame))
            i += 1
            time.sleep(1.0 / self._fps)


@dataclass
class Mapping:
    dmx_channel: int
    midi_channel: int
    note: int


def _expand_mapping_entry(m):
    """A mapping entry is either a single mapping (dmx_channel/note) or a
    range (dmx_channel_start/dmx_channel_end/note_start), which expands to
    one Mapping per channel, with notes assigned consecutively starting at
    note_start."""
    midi_channel = int(m["midi_channel"])
    if not 1 <= midi_channel <= 16:
        raise ValueError(f"midi_channel out of range (1-16): {midi_channel}")

    if "dmx_channel" in m:
        dmx_channel = int(m["dmx_channel"])
        note = int(m["note"])
        if not 1 <= dmx_channel <= 512:
            raise ValueError(f"dmx_channel out of range (1-512): {dmx_channel}")
        if not 0 <= note <= 127:
            raise ValueError(f"note out of range (0-127): {note}")
        return [Mapping(dmx_channel, midi_channel, note)]

    start = int(m["dmx_channel_start"])
    end = int(m["dmx_channel_end"])
    note_start = int(m["note_start"])
    if not 1 <= start <= 512 or not 1 <= end <= 512:
        raise ValueError(f"dmx_channel range out of range (1-512): {start}-{end}")
    if start > end:
        raise ValueError(f"dmx_channel_start ({start}) must be <= dmx_channel_end ({end})")
    count = end - start + 1
    note_end = note_start + count - 1
    if not 0 <= note_start <= 127 or not 0 <= note_end <= 127:
        raise ValueError(
            f"note range out of range (0-127): {note_start}-{note_end} "
            f"(from note_start={note_start} over {count} channels)"
        )
    return [
        Mapping(start + i, midi_channel, note_start + i) for i in range(count)
    ]


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    mappings = []
    for m in cfg.get("mappings", []):
        mappings.extend(_expand_mapping_entry(m))
    return cfg, mappings


def open_midi_output(midi_cfg):
    output = midi_cfg.get("output", "virtual")
    virtual_name = midi_cfg.get("virtual_port_name", "DMX2MIDI")
    if output == "virtual":
        return mido.open_output(virtual_name, virtual=True)
    available = mido.get_output_names()
    matches = [p for p in available if output in p]
    if not matches:
        if not midi_cfg.get("fallback_to_virtual", True):
            raise RuntimeError(
                f"No MIDI output port matching '{output}'. Available ports: {available}"
            )
        logger.warning(
            "No MIDI output port matching '%s' (available: %s); "
            "falling back to virtual port '%s'",
            output,
            available,
            virtual_name,
        )
        return mido.open_output(virtual_name, virtual=True)
    return mido.open_output(matches[0])


def send_all_notes_off(midi_out):
    """Sends Note Off for every note (0-127) on every MIDI channel (0-15),
    to clear any stuck notes. Called at startup and shutdown."""
    for channel in range(16):
        for note in range(128):
            midi_out.send(mido.Message("note_off", channel=channel, note=note, velocity=0))


class MidiBridge:
    """Turns DMX channel value changes into MIDI note on/off messages.

    A change to a value > 0 sends Note On with velocity scaled from the
    DMX value (1-255 -> 1-127, clamped so it's never 0 while "on").
    A change to 0 sends Note Off.
    """

    def __init__(self, midi_out, mappings, on_message=None):
        self._out = midi_out
        self._mappings = mappings
        self._last_values = {}
        self._on_message = on_message

    @staticmethod
    def _scale_velocity(value):
        if value <= 0:
            return 0
        return max(1, round(value * 127 / 255))

    def on_dmx_frame(self, channels):
        for m in self._mappings:
            idx = m.dmx_channel - 1
            if idx >= len(channels):
                continue
            value = channels[idx]
            if self._last_values.get(m.dmx_channel) == value:
                continue
            self._last_values[m.dmx_channel] = value
            midi_ch = m.midi_channel - 1
            if value > 0:
                msg = mido.Message(
                    "note_on",
                    channel=midi_ch,
                    note=m.note,
                    velocity=self._scale_velocity(value),
                )
            else:
                msg = mido.Message(
                    "note_off", channel=midi_ch, note=m.note, velocity=0
                )
            self._out.send(msg)
            logger.debug("DMX ch%d=%d -> %s", m.dmx_channel, value, msg)
            if self._on_message is not None:
                self._on_message(m, value, msg)


class ConsoleUI:
    """Live curses dashboard, in place of an endless scrolling debug log.
    Runs on its own thread; feed it via on_frame()/on_message() the same
    way AsyncFrameLogger/MidiBridge are fed.

    Two views, toggled with 'a':
    - "all": a scrollable grid of all 512 DMX channels, with mapped
      channels highlighted.
    - "mapped": a table of just the mapped channels plus a recent MIDI
      event log.
    """

    CELL_WIDTH = 8  # "001:000 "

    def __init__(self, mappings, mode_label, midi_port_name):
        self._mappings = mappings
        self._mapped_channels = {m.dmx_channel for m in mappings}
        self._mode_label = mode_label
        self._midi_port_name = midi_port_name
        self._lock = threading.Lock()
        self._channels = bytearray(512)
        self._events = collections.deque(maxlen=50)
        self._frame_count = 0
        self._fps = 0.0
        self._start_time = time.time()
        self._stop_event = threading.Event()
        self._thread = None
        self._view = "all"
        self._scroll = 0

    def on_frame(self, channels):
        with self._lock:
            self._frame_count += 1
            n = min(len(channels), 512)
            self._channels[:n] = channels[:n]

    def on_message(self, mapping, value, msg):
        ts = time.strftime("%H:%M:%S")
        kind = "ON " if msg.type == "note_on" else "OFF"
        with self._lock:
            self._events.appendleft(
                f"{ts}  ch{mapping.dmx_channel:<3} -> note {mapping.note:<3} "
                f"{kind} vel={msg.velocity:<3} (midi ch{mapping.midi_channel})"
            )

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def is_stopped(self):
        return self._stop_event.is_set()

    def _run(self):
        curses.wrapper(self._main)

    def _main(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        has_colors = curses.has_colors()
        if has_colors:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)  # unmapped, on
            curses.init_pair(2, curses.COLOR_YELLOW, -1)  # mapped, on
            curses.init_pair(3, curses.COLOR_CYAN, -1)  # mapped, off

        last_frame_count = 0
        last_check = time.time()
        while not self._stop_event.is_set():
            try:
                key = stdscr.getch()
            except curses.error:
                key = -1

            if key in (ord("q"), ord("Q")):
                self._stop_event.set()
                break
            elif key in (ord("a"), ord("A"), ord("m"), ord("M")):
                self._view = "mapped" if self._view == "all" else "all"
            elif key == curses.KEY_UP:
                self._scroll = max(0, self._scroll - 1)
            elif key == curses.KEY_DOWN:
                self._scroll += 1
            elif key == curses.KEY_PPAGE:
                self._scroll = max(0, self._scroll - 10)
            elif key == curses.KEY_NPAGE:
                self._scroll += 10
            elif key == curses.KEY_HOME:
                self._scroll = 0

            now = time.time()
            if now - last_check >= 1.0:
                with self._lock:
                    self._fps = (self._frame_count - last_frame_count) / (
                        now - last_check
                    )
                    last_frame_count = self._frame_count
                last_check = now

            try:
                if self._view == "all":
                    self._draw_all(stdscr, has_colors)
                else:
                    self._draw_mapped(stdscr)
            except Exception:
                logger.exception("Error drawing the UI; continuing")
            time.sleep(0.05)

    def _safe_addstr(self, stdscr, y, x, text, attr=curses.A_NORMAL):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and w > x:
            try:
                stdscr.addstr(y, x, text[: w - x - 1], attr)
            except curses.error:
                pass  # bottom-right corner write; harmless to skip

    def _header(self, frame_count, fps, extra):
        elapsed = time.time() - self._start_time
        return (
            f" DMX->MIDI Bridge | input={self._mode_label} | "
            f"midi_out={self._midi_port_name} | frames={frame_count} "
            f"({fps:.1f}/s) | up {elapsed:.0f}s | {extra} "
        )

    def _draw_all(self, stdscr, has_colors):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        with self._lock:
            channels = bytes(self._channels)
            fps = self._fps
            frame_count = self._frame_count

        header = self._header(
            frame_count, fps, "a=mapped view  ↑↓/PgUp/PgDn=scroll  q=quit"
        )
        self._safe_addstr(stdscr, 0, 0, header, curses.A_REVERSE)

        per_row = max(1, (w - 1) // self.CELL_WIDTH)
        total_rows = (512 + per_row - 1) // per_row
        visible_rows = max(1, h - 3)
        max_scroll = max(0, total_rows - visible_rows)
        self._scroll = min(self._scroll, max_scroll)

        self._safe_addstr(
            stdscr,
            2,
            0,
            f"All 512 channels, rows {self._scroll + 1}-"
            f"{min(self._scroll + visible_rows, total_rows)} of {total_rows} "
            "(yellow=mapped+on, cyan=mapped+off, green=on)",
            curses.A_UNDERLINE,
        )

        for r in range(visible_rows):
            row_index = self._scroll + r
            if row_index >= total_rows:
                break
            y = 3 + r
            start_ch = row_index * per_row + 1
            x = 0
            for c in range(per_row):
                ch = start_ch + c
                if ch > 512:
                    break
                value = channels[ch - 1]
                text = f"{ch:03d}:{value:03d} "
                is_mapped = ch in self._mapped_channels
                attr = curses.A_NORMAL
                if has_colors:
                    if is_mapped:
                        attr = curses.color_pair(2 if value > 0 else 3)
                        if value > 0:
                            attr |= curses.A_BOLD
                    elif value > 0:
                        attr = curses.color_pair(1)
                else:
                    if is_mapped:
                        attr |= curses.A_UNDERLINE
                    if value > 0:
                        attr |= curses.A_BOLD
                self._safe_addstr(stdscr, y, x, text, attr)
                x += self.CELL_WIDTH

        stdscr.refresh()

    def _draw_mapped(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        with self._lock:
            channels = bytes(self._channels)
            events = list(self._events)
            fps = self._fps
            frame_count = self._frame_count

        header = self._header(frame_count, fps, "a=all-channel view  q=quit")
        self._safe_addstr(stdscr, 0, 0, header, curses.A_REVERSE)

        self._safe_addstr(
            stdscr,
            2,
            0,
            f"{'DMX ch':>7} {'Note':>5} {'MIDI ch':>8} {'Value':>6} "
            f"{'Velocity':>9}  State",
            curses.A_UNDERLINE,
        )
        row = 3
        max_table_row = max(3, h - 4)
        for m in self._mappings:
            if row >= max_table_row:
                self._safe_addstr(stdscr, row, 0, "...")
                row += 1
                break
            value = channels[m.dmx_channel - 1] if m.dmx_channel <= len(channels) else 0
            velocity = 0 if value <= 0 else max(1, round(value * 127 / 255))
            state = "ON " if value > 0 else "off"
            attr = curses.A_BOLD if value > 0 else curses.A_DIM
            line = (
                f"{m.dmx_channel:>7} {m.note:>5} {m.midi_channel:>8} "
                f"{value:>6} {velocity:>9}  {state}"
            )
            self._safe_addstr(stdscr, row, 0, line, attr)
            row += 1

        event_header_row = row + 1
        self._safe_addstr(
            stdscr, event_header_row, 0, "Recent MIDI events:", curses.A_UNDERLINE
        )
        for i, ev in enumerate(events):
            y = event_header_row + 1 + i
            if y >= h - 1:
                break
            self._safe_addstr(stdscr, y, 0, ev)

        stdscr.refresh()


class LatestFrameProcessor(threading.Thread):
    """Runs frame handling (debug logging + MIDI send) on its own thread,
    decoupled from whatever is receiving raw frames (serial or UDP socket).

    Without this, a slow handler call (console logging, MIDI I/O) blocks the
    reader thread from going back to recvfrom()/serial read in time, so the
    OS-level receive buffer fills up and packets get silently dropped during
    bursts (e.g. quickly dragging a fader in QLC+). Only the most recent
    frame is kept: DMX/Art-Net is continuously-refreshed state, not a queue
    of discrete events, so it's fine (and desirable) to skip stale frames
    rather than fall further and further behind.
    """

    def __init__(self, on_frame):
        super().__init__(daemon=True)
        self._on_frame = on_frame
        self._queue = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)  # unblock a pending get()
        except queue.Full:
            pass

    def submit(self, channels):
        try:
            self._queue.put_nowait(channels)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop the stale pending frame
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(channels)
            except queue.Full:
                pass

    def run(self):
        while not self._stop_event.is_set():
            channels = self._queue.get()
            if channels is not None:
                try:
                    self._on_frame(channels)
                except Exception:
                    logger.exception(
                        "Error while processing a DMX frame; continuing"
                    )


def _summarize_frame(channels, max_items=16):
    nonzero = [(i + 1, v) for i, v in enumerate(channels) if v]
    if not nonzero:
        return "(all channels zero)"
    text = ", ".join(f"ch{c}={v}" for c, v in nonzero[:max_items])
    if len(nonzero) > max_items:
        text += f", ... (+{len(nonzero) - max_items} more)"
    return text


class AsyncFrameLogger(threading.Thread):
    """Logs every distinct incoming DMX frame at debug level, on its own
    thread, deliberately NOT coalesced the way LatestFrameProcessor is.

    If this were fed through the same single-slot mailbox used for MIDI
    output, fast-changing frames (e.g. dragging a fader in QLC+) would look
    like "missing" reception in the debug log purely because intermediate
    frames were intentionally skipped for MIDI purposes -- not because they
    were actually lost. Logging every frame independently means a gap here
    is trustworthy evidence that a frame never reached the process (lost on
    the wire, or dropped by the OS receive buffer), rather than an artifact
    of MIDI-side coalescing.

    The receiving thread only does a cheap byte-compare + enqueue; the
    (slower) string formatting and logger call happen here so the receiver
    is never slowed down by logging.
    """

    def __init__(self, maxsize=2000):
        super().__init__(daemon=True)
        self._queue = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()
        self._last = None

    def stop(self):
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def submit(self, channels):
        if channels == self._last:
            return
        self._last = bytes(channels)
        try:
            self._queue.put_nowait(self._last)
        except queue.Full:
            logger.warning(
                "Frame log queue full; dropped a debug log line "
                "(MIDI output is unaffected, this only affects logging)"
            )

    def run(self):
        while True:
            item = self._queue.get()
            if item is None:
                if self._stop_event.is_set():
                    return
                continue
            logger.debug("DMX frame received: %s", _summarize_frame(item))


def fan_out(*callbacks):
    """Calls every callback with the same frame. This runs directly on the
    serial/UDP reader thread, so an exception in one callback must not be
    allowed to kill that thread -- that would silently stop all DMX
    reception (every other callback, and the socket/serial read loop
    itself, would never run again)."""

    def dispatch(channels):
        for cb in callbacks:
            try:
                cb(channels)
            except Exception:
                logger.exception(
                    "Error in a DMX frame callback (%s); continuing", cb
                )

    return dispatch


def _build_artnet_source(cfg, on_frame):
    artnet_cfg = cfg.get("artnet", {})
    return ArtNetReceiver(
        on_frame,
        bind_address=artnet_cfg.get("bind_address", "0.0.0.0"),
        port=artnet_cfg.get("port", 6454),
        universe=artnet_cfg.get("universe", 0),
    )


def build_dmx_source(cfg, on_frame):
    input_cfg = cfg.get("input", {})
    mode = input_cfg.get("mode", "usb")
    if mode == "usb":
        serial_cfg = cfg.get("serial", {})
        port = serial_cfg["port"]
        baudrate = serial_cfg.get("baudrate", 57600)
        try:
            probe = serial.Serial(port, baudrate)
            probe.close()
        except serial.SerialException as e:
            if not input_cfg.get("fallback_to_artnet", True):
                raise
            logger.warning(
                "Could not open USB serial port '%s' (%s); falling back to Art-Net",
                port,
                e,
            )
            return _build_artnet_source(cfg, on_frame)
        return EnttecDMXReader(port, baudrate, on_frame)
    if mode == "artnet":
        return _build_artnet_source(cfg, on_frame)
    raise ValueError(f"Unknown input.mode '{mode}' (expected 'usb' or 'artnet')")


def main():
    parser = argparse.ArgumentParser(
        description="DMX (Enttec USB Pro or Art-Net) -> USB MIDI bridge"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--list-serial-ports", action="store_true", help="List serial ports and exit"
    )
    parser.add_argument(
        "--list-midi-ports", action="store_true", help="List MIDI output ports and exit"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Generate synthetic DMX data instead of reading the serial widget "
        "(for testing the MIDI output side without hardware)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Show a live curses dashboard (mapped-channel state + recent MIDI "
        "events) instead of a scrolling console log",
    )
    parser.add_argument(
        "--log-file",
        default="dmx_midi_bridge.log",
        help="Where to write logs when --ui is active (the dashboard owns the "
        "terminal, so logs can't go to the console)",
    )
    args = parser.parse_args()

    log_kwargs = dict(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.ui:
        log_kwargs["filename"] = args.log_file
    logging.basicConfig(**log_kwargs)

    if args.list_serial_ports:
        for p in serial.tools.list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return

    if args.list_midi_ports:
        for name in mido.get_output_names():
            print(name)
        return

    cfg, mappings = load_config(args.config)
    if not mappings:
        logger.warning("No mappings configured; nothing will be sent to MIDI.")

    midi_out = open_midi_output(cfg.get("midi", {}))
    logger.info("MIDI output ready: %s", midi_out.name)
    send_all_notes_off(midi_out)

    mode_label = "simulate" if args.simulate else cfg.get("input", {}).get("mode", "usb")
    ui = ConsoleUI(mappings, mode_label, midi_out.name) if args.ui else None

    bridge = MidiBridge(midi_out, mappings, on_message=ui.on_message if ui else None)
    processor = LatestFrameProcessor(bridge.on_dmx_frame)
    frame_logger = AsyncFrameLogger()
    callbacks = [frame_logger.submit, processor.submit]
    if ui:
        callbacks.append(ui.on_frame)
    on_frame = fan_out(*callbacks)

    if args.simulate:
        channels = sorted({m.dmx_channel for m in mappings}) or [1]
        source = DMXSimulator(on_frame, channels)
    else:
        source = build_dmx_source(cfg, on_frame)

    frame_logger.start()
    processor.start()
    if ui:
        ui.start()
    source.start()
    logger.info("Bridge running. Press Ctrl+C to stop.")
    try:
        while True:
            if ui and ui.is_stopped():
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        source.join(timeout=2)
        processor.stop()
        processor.join(timeout=2)
        frame_logger.stop()
        frame_logger.join(timeout=2)
        if ui:
            ui.stop()
        send_all_notes_off(midi_out)
        midi_out.close()
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
