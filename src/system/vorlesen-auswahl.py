#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Systemweites Vorlesen der Textauswahl mit Edge-TTS und Karaoke-Fenster.

Greift die aktuelle Markierung (X11 PRIMARY-Selection, sonst Zwischenablage)
ab, liest sie mit Microsoft-Edge-TTS vor und zeigt ein schwebendes Fenster,
das den Text anzeigt und Satz (hellgrau) + aktuelles Wort (hellorange)
hervorhebt. Steuerung: Pause/Weiter, Stopp, Satz zurück/vor
(⏮/⏭ bzw. Pfeiltasten ←/→), Tempo −/+.

Aufruf (z. B. über ein globales Tastenkürzel):
    vorlesen-auswahl.py

Konfiguration (Stimme, Tempo):  ~/.config/vorlesen-miniplayer.json
"""

import bisect
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

# Unter Wayland ignoriert GNOME set_keep_above(); über XWayland wird
# "immer im Vordergrund" dagegen respektiert. Muss vor dem Gdk-Import stehen.
if os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("GDK_BACKEND", "x11")

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango  # noqa: E402

CONF_PATH = os.path.expanduser("~/.config/vorlesen-miniplayer.json")
DEFAULT_CONF = {"voice": "de-DE-KillianNeural", "speed": 1.0,
                "highlight": 0xDDDDDD, "word": 0xFFDBB6}
EDGE_TTS = os.path.expanduser("~/.local/bin/edge-tts")
MPV = "/usr/bin/mpv"

_SENT_END = ".!?…"
_TRAIL = "\"')»«’”"
ABBREV = {
    "z", "b", "bzw", "ca", "dr", "prof", "nr", "s", "vgl", "u", "a",
    "etc", "evtl", "ggf", "inkl", "max", "min", "mio", "mrd", "sog",
    "usw", "zb", "o", "ä", "abs", "art", "bspw", "d", "h", "i", "v",
}


def load_conf():
    conf = dict(DEFAULT_CONF)
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            conf.update(json.load(f))
    except Exception:
        pass
    return conf


def save_conf(conf):
    try:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2)
    except Exception:
        pass


def hexcolor(v):
    return "#%06X" % (int(v) & 0xFFFFFF)


def get_selection():
    """Aktuelle Markierung holen: erst PRIMARY, dann Zwischenablage."""
    for args in (["xclip", "-o", "-selection", "primary"],
                 ["xclip", "-o", "-selection", "clipboard"]):
        try:
            out = subprocess.run(args, capture_output=True, timeout=2).stdout
            text = out.decode("utf-8", "replace").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def split_sentences(text):
    """Liste (offset, laenge, satztext) über dem normalisierten Text."""
    spans = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            spans.append((start, i))
            start = i + 1
        elif c in _SENT_END:
            j = i
            while j + 1 < n and text[j + 1] in _SENT_END + _TRAIL:
                j += 1
            nxt = j + 1
            while nxt < n and text[nxt] in " \t":
                nxt += 1
            at_end = nxt >= n or text[nxt] == "\n"
            next_lower = nxt < n and text[nxt].islower()
            m = re.search(r"([A-Za-zÄÖÜäöüß]+)\.$", text[start:i + 1])
            is_abbrev = bool(m) and m.group(1).lower() in ABBREV
            if (at_end or (text[j + 1] in " \t" and not next_lower)) \
                    and not is_abbrev:
                spans.append((start, j + 1))
                start = j + 1
            i = j
        i += 1
    if start < n:
        spans.append((start, n))
    out = []
    for a, b in spans:
        seg = text[a:b]
        stripped = seg.strip()
        if not stripped:
            continue
        a2 = a + (len(seg) - len(seg.lstrip()))
        out.append((a2, len(stripped), stripped))
    return out


def parse_vtt_span(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception:
        return None
    m = re.search(
        r"(\d\d):(\d\d):(\d\d)[,.](\d+)\s*-->\s*"
        r"(\d\d):(\d\d):(\d\d)[,.](\d+)", data)
    if not m:
        return None
    g = [int(x) for x in m.groups()]
    start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
    end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
    return (start, end)


def compute_word_times(txt, start, end):
    """Sprechdauer proportional (nach Wortlänge + Pausen) auf Wörter verteilen."""
    words = [(m.start(), m.group()) for m in re.finditer(r"\S+", txt)]
    if not words:
        return []

    def weight(w):
        letters = sum(1 for c in w if c.isalnum())
        base = max(1, letters)
        if w[-1] in ".!?…":
            return base + 3.0
        if w[-1] in ",;:":
            return base + 1.5
        return base

    weights = [weight(w) for _, w in words]
    total = sum(weights) or 1.0
    span = max(0.05, end - start)
    times = []
    t = start
    for (off, w), wt in zip(words, weights):
        times.append((t, off, len(w)))
        t += span * wt / total
    times[0] = (0.0, times[0][1], times[0][2])
    return times


class Mpv:
    """Persistente mpv-Instanz mit JSON-IPC (Pause/Speed live änderbar)."""

    def __init__(self, sock_path):
        self.proc = subprocess.Popen(
            [MPV, "--idle=yes", "--no-terminal", "--no-video",
             "--input-ipc-server=" + sock_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 5
        while True:
            try:
                self.sock.connect(sock_path)
                break
            except OSError:
                if time.time() > deadline or self.proc.poll() is not None:
                    raise RuntimeError("mpv-IPC nicht erreichbar")
                time.sleep(0.05)
        self.sock.settimeout(0.3)
        self._buf = b""
        self.cmd("observe_property", 1, "time-pos")

    def cmd(self, *args):
        try:
            self.sock.sendall(json.dumps({"command": list(args)}).encode() + b"\n")
        except OSError:
            pass

    def set_prop(self, prop, value):
        self.cmd("set_property", prop, value)

    def load(self, path):
        self.cmd("loadfile", path, "replace")

    def play_watch(self, on_time, should_abort):
        while True:
            if should_abort() or self.proc.poll() is not None:
                return False
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return False
                self._buf += chunk
            except socket.timeout:
                continue
            except OSError:
                return False
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                ev = msg.get("event")
                if ev == "property-change" and msg.get("id") == 1:
                    data = msg.get("data")
                    if data is not None:
                        try:
                            on_time(float(data))
                        except Exception:
                            pass
                elif ev == "end-file":
                    # "stop"/"redirect" stammen vom Spulen bzw. loadfile
                    # replace und wären hier veraltete Ereignisse des
                    # vorherigen Satzes — nur echtes Dateiende zählt.
                    if msg.get("reason") in (None, "eof", "error"):
                        return True

    def quit(self):
        self.cmd("quit")
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


class Player(Gtk.Window):
    """Schwebendes Karaoke-Fenster + Vorlese-Steuerung."""

    def __init__(self, app, text):
        super().__init__(title="Vorlesen")
        self.app = app
        self.conf = load_conf()
        self.speed = float(self.conf.get("speed", 1.0))
        self.voice = self.conf.get("voice", DEFAULT_CONF["voice"])
        self.hl_color = hexcolor(self.conf.get("highlight",
                                               DEFAULT_CONF["highlight"]))
        self.word_color = hexcolor(self.conf.get("word", DEFAULT_CONF["word"]))

        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.sentences = split_sentences(self.text)
        self.ready = [threading.Event() for _ in self.sentences]
        self.word_times = [None] * len(self.sentences)
        self.failed = set()
        self.tmpdir = tempfile.mkdtemp(prefix="vorlesen-sel-")
        self.mpv = None
        self.synth_proc = None
        self.stop = False
        self.paused = False
        self.seek_req = None
        self.done = False

        self._build_ui()
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key)

        threading.Thread(target=self.synth_worker, daemon=True).start()
        threading.Thread(target=self.player_worker, daemon=True).start()

    # ---------- UI ----------

    def _build_ui(self):
        self.set_keep_above(True)
        self.set_default_size(460, 260)
        self.set_border_width(0)
        try:
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        except Exception:
            pass

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(12)
        self.view.set_right_margin(12)
        self.view.set_top_margin(10)
        self.view.set_bottom_margin(10)
        try:
            css = Gtk.CssProvider()
            css.load_from_data(b"textview { font-family: Sans; font-size: 13pt; }")
            self.view.get_style_context().add_provider(
                css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:
            pass
        self.buf = self.view.get_buffer()
        self.buf.set_text(self.text)
        self.tag_sentence = self.buf.create_tag("s", background=self.hl_color)
        self.tag_word = self.buf.create_tag("w", background=self.word_color)
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ctrl.set_border_width(8)
        box.pack_start(ctrl, False, False, 0)

        btn_prev = Gtk.Button(label="⏮")
        btn_prev.set_tooltip_text("Satz zurück (←)")
        btn_prev.connect("clicked", lambda *_: self.seek_sentence(-1))
        ctrl.pack_start(btn_prev, False, False, 0)

        self.btn_pause = Gtk.Button(label="⏸  Pause")
        self.btn_pause.connect("clicked", lambda *_: self.toggle_pause())
        ctrl.pack_start(self.btn_pause, False, False, 0)

        btn_next = Gtk.Button(label="⏭")
        btn_next.set_tooltip_text("Satz vor (→)")
        btn_next.connect("clicked", lambda *_: self.seek_sentence(+1))
        ctrl.pack_start(btn_next, False, False, 0)

        btn_stop = Gtk.Button(label="⏹  Stopp")
        btn_stop.connect("clicked", lambda *_: self.close())
        ctrl.pack_start(btn_stop, False, False, 0)

        self.lbl_status = Gtk.Label(label="⏳ Lädt …")
        self.lbl_status.set_xalign(0.0)
        self.lbl_status.set_ellipsize(Pango.EllipsizeMode.END)
        ctrl.pack_start(self.lbl_status, True, True, 6)

        btn_slow = Gtk.Button(label="−")
        btn_slow.connect("clicked", lambda *_: self.change_speed(-0.1))
        ctrl.pack_start(btn_slow, False, False, 0)

        self.lbl_speed = Gtk.Label(label=self._speed_text())
        self.lbl_speed.set_width_chars(5)
        ctrl.pack_start(self.lbl_speed, False, False, 0)

        btn_fast = Gtk.Button(label="+")
        btn_fast.connect("clicked", lambda *_: self.change_speed(+0.1))
        ctrl.pack_start(btn_fast, False, False, 0)

        self.move(80, 80)

    def _speed_text(self):
        return ("%.1f×" % self.speed).replace(".", ",")

    def on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if ev.keyval == Gdk.KEY_space:
            self.toggle_pause()
            return True
        if ev.keyval == Gdk.KEY_Left:
            self.seek_sentence(-1)
            return True
        if ev.keyval == Gdk.KEY_Right:
            self.seek_sentence(+1)
            return True
        return False

    # ---------- Steuerung ----------

    def toggle_pause(self):
        self.paused = not self.paused
        if self.mpv:
            self.mpv.set_prop("pause", self.paused)
        self.btn_pause.set_label("▶  Weiter" if self.paused else "⏸  Pause")

    def change_speed(self, delta):
        self.speed = round(min(3.0, max(0.5, self.speed + delta)), 1)
        if self.mpv:
            self.mpv.set_prop("speed", self.speed)
        self.lbl_speed.set_label(self._speed_text())
        self.conf["speed"] = self.speed
        save_conf(self.conf)

    def seek_sentence(self, delta):
        """Einen Satz vor- oder zurückspringen (mehrfaches Drücken summiert)."""
        self.seek_req = (self.seek_req or 0) + delta
        if self.mpv:
            self.mpv.cmd("stop")

    def set_status(self, msg):
        self.lbl_status.set_label(msg)

    # ---------- Hervorhebung (im GTK-Haupt-Thread) ----------

    def highlight_sentence(self, off, length):
        self.buf.remove_tag(self.tag_word, self.buf.get_start_iter(),
                            self.buf.get_end_iter())
        self.buf.remove_tag(self.tag_sentence, self.buf.get_start_iter(),
                            self.buf.get_end_iter())
        a = self.buf.get_iter_at_offset(off)
        b = self.buf.get_iter_at_offset(off + length)
        self.buf.apply_tag(self.tag_sentence, a, b)
        self.view.scroll_to_iter(a, 0.15, False, 0, 0)

    def highlight_word(self, off, length):
        self.buf.remove_tag(self.tag_word, self.buf.get_start_iter(),
                            self.buf.get_end_iter())
        if length > 0:
            a = self.buf.get_iter_at_offset(off)
            b = self.buf.get_iter_at_offset(off + length)
            self.buf.apply_tag(self.tag_word, a, b)

    # ---------- Worker-Threads ----------

    def synth_worker(self):
        for i, (_, _, txt) in enumerate(self.sentences):
            if self.stop:
                return
            path = os.path.join(self.tmpdir, "%04d.mp3" % i)
            vtt = os.path.join(self.tmpdir, "%04d.vtt" % i)
            try:
                self.synth_proc = subprocess.Popen(
                    [EDGE_TTS, "--voice", self.voice, "--text", txt,
                     "--write-media", path, "--write-subtitles", vtt],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                rc = self.synth_proc.wait()
            except Exception:
                rc = -1
            if rc != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
                self.failed.add(i)
            else:
                span = parse_vtt_span(vtt)
                if span:
                    try:
                        self.word_times[i] = compute_word_times(
                            txt, span[0], span[1])
                    except Exception:
                        self.word_times[i] = None
            self.ready[i].set()

    def player_worker(self):
        n = len(self.sentences)
        try:
            self.mpv = Mpv(os.path.join(self.tmpdir, "ipc.sock"))
            self.mpv.set_prop("speed", self.speed)
            i = 0
            while i < n:
                if self.stop:
                    break
                if not self.ready[i].is_set():
                    GLib.idle_add(self.set_status,
                                  "⏳ Lädt Satz %d/%d …" % (i + 1, n))
                    while not self.ready[i].wait(timeout=0.2):
                        if self.stop:
                            return
                        if self.seek_req is not None:
                            break
                if self.stop:
                    break
                if self.seek_req is not None:
                    delta, self.seek_req = self.seek_req, None
                    i = max(0, min(n - 1, i + delta))
                    continue
                if i in self.failed:
                    GLib.idle_add(self.set_status,
                                  "⚠ Satz %d übersprungen (Netzwerk?)" % (i + 1))
                    time.sleep(0.8)
                    i += 1
                    continue
                off, ln, _ = self.sentences[i]
                GLib.idle_add(self.highlight_sentence, off, ln)
                GLib.idle_add(self.set_status, "🔊 Liest Satz %d/%d" % (i + 1, n))

                times = self.word_times[i] or []
                starts = [t[0] for t in times]
                cur = [-1]

                def on_time(t, times=times, starts=starts, sent_off=off, cur=cur):
                    if not times:
                        return
                    idx = bisect.bisect_right(starts, t) - 1
                    if idx < 0:
                        idx = 0
                    if idx != cur[0]:
                        cur[0] = idx
                        _, coff, clen = times[idx]
                        GLib.idle_add(self.highlight_word,
                                      sent_off + coff, clen)

                self.mpv.load(os.path.join(self.tmpdir, "%04d.mp3" % i))
                finished = self.mpv.play_watch(
                    on_time, lambda: self.stop or self.seek_req is not None)
                if self.stop:
                    break
                if self.seek_req is not None:
                    delta, self.seek_req = self.seek_req, None
                    i = max(0, min(n - 1, i + delta))
                    continue
                if not finished:
                    break
                i += 1
            if not self.stop:
                GLib.idle_add(self.set_status, "✓ Fertig")
                time.sleep(1.0)
        except Exception as exc:
            GLib.idle_add(self.set_status, "Fehler: %s" % exc)
            time.sleep(1.5)
        finally:
            GLib.idle_add(self.close)

    # ---------- Aufräumen ----------

    def on_destroy(self, *_):
        self.stop = True
        for ev in self.ready:
            ev.set()
        if self.synth_proc and self.synth_proc.poll() is None:
            try:
                self.synth_proc.kill()
            except Exception:
                pass
        if self.mpv:
            self.mpv.quit()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self.app:
            self.app.quit()


def main():
    text = get_selection()
    if not text.strip():
        try:
            subprocess.Popen(["notify-send", "Vorlesen",
                              "Kein Text markiert."])
        except Exception:
            pass
        return
    app = Gtk.Application.new("de.local.vorlesen", 0)

    def on_activate(a):
        win = Player(a, text)
        a.add_window(win)
        win.show_all()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
