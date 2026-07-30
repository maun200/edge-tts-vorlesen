# -*- coding: utf-8 -*-
"""Vorlesen-Miniplayer für LibreOffice Writer.

Liest den markierten Text (oder ab Cursor bis Dokumentende) mit
Microsoft-Edge-TTS vor. Zeigt einen kleinen schwebenden Player mit
Pause/Weiter, Stopp, Satz zurück/vor (⏮/⏭) und
Geschwindigkeitsregelung. Der gerade
gesprochene Satz wird im Dokument markiert.

Konfiguration (Stimme, gespeicherte Geschwindigkeit):
    ~/.config/vorlesen-miniplayer.json
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

CONF_PATH = os.path.expanduser("~/.config/vorlesen-miniplayer.json")
LOG_PATH = os.path.expanduser("~/.config/vorlesen-miniplayer.log")
DEFAULT_CONF = {"voice": "de-DE-KillianNeural", "speed": 1.0,
                "highlight": 0xDDDDDD,   # Hellgrau 4 – ganzer Satz
                "word": 0xFFDBB6}        # Hellorange 4 – aktuelles Wort
NO_HIGHLIGHT = -1  # LibreOffice-Wert für „keine Zeichenhervorhebung“
EDGE_TTS = os.path.expanduser("~/.local/bin/edge-tts")
MPV = "/usr/bin/mpv"


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass

ABBREV = {
    "z", "b", "bzw", "ca", "dr", "prof", "nr", "s", "vgl", "u", "a",
    "etc", "evtl", "ggf", "inkl", "max", "min", "mio", "mrd", "sog",
    "usw", "zb", "o", "ä", "abs", "art", "bspw", "d", "h", "i", "v",
}

_SENT_END = ".!?…"
_TRAIL = "\"')»«’”"


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


def split_sentences(text):
    """Liefert Liste (offset, laenge, satztext) über dem normalisierten Text.

    Offsets zählen einen Absatzumbruch als 1 Zeichen — das entspricht
    einem goRight()-Schritt des Writer-Textcursors.
    """
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
            if (at_end or (text[j + 1] in " \t" and not next_lower)) and not is_abbrev:
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
    """Start- und Endzeit (Sekunden) aus dem einzelnen Untertitel-Cue lesen."""
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
    """Verteilt die Sprechdauer proportional auf die Wörter.

    Da Edge-TTS keine Wort-Zeitstempel mehr liefert, wird die Dauer
    gewichtet nach Wortlänge und Satzzeichen-Pausen aufgeteilt.
    Liefert Liste (t_start_sekunden, zeichen_offset_in_txt, wort_laenge).
    """
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
    # Erstes Wort sofort ab Wiedergabestart hervorheben.
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
        # Wiedergabeposition periodisch beobachten (für Wort-Hervorhebung).
        self.cmd("observe_property", 1, "time-pos")

    def cmd(self, *args):
        try:
            payload = json.dumps({"command": list(args)}).encode() + b"\n"
            self.sock.sendall(payload)
        except OSError:
            pass

    def set_prop(self, prop, value):
        self.cmd("set_property", prop, value)

    def load(self, path):
        self.cmd("loadfile", path, "replace")

    def play_watch(self, on_time, should_abort):
        """Bis end-file blockieren; bei jedem Positions-Update on_time(sek)
        aufrufen. Bricht ab, wenn should_abort()."""
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


class Session:
    """Zustand einer laufenden Vorlese-Sitzung."""

    def __init__(self):
        self.stop = False
        self.paused = False
        self.seek_req = None
        self.done = False
        self.lock = threading.Lock()
        self.conf = load_conf()
        self.speed = float(self.conf.get("speed", 1.0))
        self.voice = self.conf.get("voice", DEFAULT_CONF["voice"])
        self.hl_color = int(self.conf.get("highlight", DEFAULT_CONF["highlight"]))
        self.word_color = int(self.conf.get("word", DEFAULT_CONF["word"]))
        self.tmpdir = tempfile.mkdtemp(prefix="vorlesen-")
        self.mpv = None
        self.synth_proc = None
        self.sentences = []
        self.ready = []          # threading.Event je Satz
        self.word_times = []     # je Satz: Liste (t_sek, zeichen_offset, laenge)
        self.failed = set()
        self.dlg = None
        self.controller = None
        self.doc = None
        self.text = None
        self.anchor = None       # Textcursor am Start des Lesebereichs
        self.ctx = None
        self.smgr = None
        self.async_cb = None     # com.sun.star.awt.AsyncCallback
        self._cb_refs = []       # hält Callback-Objekte bis notify()
        self._saved_hl = []      # [(Cursor, alte Hervorhebung)] des akt. Satzes
        self._word_range = None  # Cursor des aktuell orange markierten Worts
        self._cur_word = -1      # Index des aktuellen Worts im Satz

    # ---------- Haupt-Thread-Dispatcher ----------

    def post(self, fn):
        """Führt fn im Haupt-Thread aus (nötig für Dokument-/UI-Zugriff)."""
        import unohelper
        from com.sun.star.awt import XCallback

        session = self

        class _Call(unohelper.Base, XCallback):
            def notify(self, data):
                try:
                    fn()
                except Exception:
                    import traceback
                    log("FEHLER im Haupt-Thread-Callback:\n"
                        + traceback.format_exc())
                finally:
                    try:
                        session._cb_refs.remove(self)
                    except ValueError:
                        pass

        cb = _Call()
        self._cb_refs.append(cb)
        try:
            self.async_cb.addCallback(cb, None)
        except Exception:
            # Kein Dispatcher verfügbar: direkt ausführen (Notlösung)
            try:
                self._cb_refs.remove(cb)
            except ValueError:
                pass
            fn()

    # ---------- UI-Helfer ----------

    def set_status(self, msg):
        try:
            self.dlg.getControl("lblStatus").getModel().Label = msg
        except Exception:
            pass

    def set_speed_label(self):
        try:
            label = ("%.1f×" % self.speed).replace(".", ",")
            self.dlg.getControl("lblSpeed").getModel().Label = label
        except Exception:
            pass

    def set_pause_label(self):
        try:
            self.dlg.getControl("btnPause").getModel().Label = (
                "▶  Weiter" if self.paused else "⏸  Pause")
        except Exception:
            pass

    # ---------- Aktionen ----------

    def toggle_pause(self):
        self.paused = not self.paused
        if self.mpv:
            self.mpv.set_prop("pause", self.paused)
        self.set_pause_label()

    def change_speed(self, delta):
        self.speed = round(min(3.0, max(0.5, self.speed + delta)), 1)
        if self.mpv:
            self.mpv.set_prop("speed", self.speed)
        self.set_speed_label()
        self.conf["speed"] = self.speed
        save_conf(self.conf)

    def seek_sentence(self, delta):
        """Einen Satz vor- oder zurückspringen (mehrfaches Drücken summiert)."""
        self.seek_req = (self.seek_req or 0) + delta
        if self.mpv:
            self.mpv.cmd("stop")

    def _no_undo(self, fn):
        """fn ausführen, ohne die Rückgängig-Historie des Dokuments zu füllen."""
        um = None
        try:
            um = self.doc.getUndoManager()
            um.lock()
        except Exception:
            um = None
        try:
            fn()
        finally:
            if um is not None:
                try:
                    um.unlock()
                except Exception:
                    pass

    def _intersect(self, portion, rng):
        """Schnittmenge einer Textportion mit rng als Cursor, oder None."""
        cmp = self.text  # unterstützt XTextRangeCompare
        try:
            cs = cmp.compareRegionStarts(portion.getStart(), rng.getStart())
            start = rng.getStart() if cs == 1 else portion.getStart()
            ce = cmp.compareRegionEnds(portion.getEnd(), rng.getEnd())
            end = portion.getEnd() if ce == 1 else rng.getEnd()
            clip = self.text.createTextCursorByRange(start)
            clip.gotoRange(end, True)
            # gültig nur, wenn Start echt vor Ende liegt (nicht leer)
            if cmp.compareRegionStarts(clip.getStart(), clip.getEnd()) != 1:
                return None
            return clip
        except Exception:
            return None

    def highlight(self, offset, length):
        """Vorherige Hervorhebung zurücksetzen, aktuellen Satz hervorheben.

        Läuft im Haupt-Thread (über self.post). Die alte Hervorhebung wird
        pro Textportion gesichert und beim Weiterschalten exakt wiederhergestellt.
        """
        def do():
            self.clear_highlight()
            sent = self.text.createTextCursorByRange(self.anchor.getStart())
            sent.goRight(offset, False)
            sent.goRight(length, True)
            saved = []
            penum = sent.createEnumeration()
            while penum.hasMoreElements():
                para = penum.nextElement()
                if not para.supportsService("com.sun.star.text.Paragraph"):
                    continue
                tpe = para.createEnumeration()
                while tpe.hasMoreElements():
                    portion = tpe.nextElement()
                    clip = self._intersect(portion, sent)
                    if clip is None:
                        continue
                    try:
                        old = clip.CharHighlight
                    except Exception:
                        old = NO_HIGHLIGHT
                    saved.append((clip, old))
            self._saved_hl = saved
            sent.CharHighlight = self.hl_color

        try:
            self._no_undo(do)
        except Exception:
            import traceback
            log("FEHLER beim Hervorheben:\n" + traceback.format_exc())

    def highlight_word(self, doc_off, length):
        """Aktuelles Wort orange markieren, vorheriges auf Satzfarbe zurück.

        Läuft im Haupt-Thread (über self.post).
        """
        def do():
            if self._word_range is not None:
                try:
                    self._word_range.CharHighlight = self.hl_color
                except Exception:
                    pass
                self._word_range = None
            if length > 0:
                w = self.text.createTextCursorByRange(self.anchor.getStart())
                w.goRight(doc_off, False)
                w.goRight(length, True)
                w.CharHighlight = self.word_color
                self._word_range = w

        try:
            self._no_undo(do)
        except Exception:
            pass

    def clear_highlight(self):
        """Gesicherte Hervorhebung des zuletzt markierten Satzes wiederherstellen."""
        self._word_range = None
        for clip, old in self._saved_hl:
            try:
                clip.CharHighlight = old
            except Exception:
                pass
        self._saved_hl = []

    def _ui_teardown(self):
        """Läuft im Haupt-Thread: Hervorhebung entfernen, Fenster schließen."""
        self._no_undo(self.clear_highlight)
        try:
            self.dlg.setVisible(False)
            self.dlg.dispose()
        except Exception:
            pass

    def cleanup(self):
        with self.lock:
            if self.done:
                return
            self.done = True
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
        self.post(self._ui_teardown)
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        global _SESSION
        _SESSION = None

    # ---------- Worker ----------

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

    def status(self, msg):
        """Statuszeile im Haupt-Thread aktualisieren."""
        self.post(lambda: self.set_status(msg))

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
                    self.status("⏳ Lädt Satz %d/%d …" % (i + 1, n))
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
                    self.status("⚠ Satz %d übersprungen (Netzwerk?)" % (i + 1))
                    time.sleep(0.8)
                    i += 1
                    continue
                off, ln, _ = self.sentences[i]
                self.post(lambda o=off, l=ln: self.highlight(o, l))
                self.status("🔊 Liest Satz %d/%d" % (i + 1, n))

                times = self.word_times[i] or []
                starts = [t[0] for t in times]
                self._cur_word = -1

                def on_time(cur, times=times, starts=starts, sent_off=off):
                    if not times:
                        return
                    idx = bisect.bisect_right(starts, cur) - 1
                    if idx < 0:
                        idx = 0
                    if idx != self._cur_word:
                        self._cur_word = idx
                        _, coff, clen = times[idx]
                        self.post(lambda a=sent_off + coff, l=clen:
                                  self.highlight_word(a, l))

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
                self.status("✓ Fertig")
                time.sleep(1.2)
        except Exception as exc:
            self.status("Fehler: %s" % exc)
            time.sleep(2)
        finally:
            self.cleanup()


_SESSION = None


def _build_dialog(ctx, smgr, frame, session):
    import unohelper
    from com.sun.star.awt import XActionListener

    class Btn(unohelper.Base, XActionListener):
        def __init__(self, fn):
            self.fn = fn

        def actionPerformed(self, ev):
            try:
                self.fn()
            except Exception:
                pass

        def disposing(self, ev):
            pass

    dm = smgr.createInstanceWithContext(
        "com.sun.star.awt.UnoControlDialogModel", ctx)
    dm.Width = 212
    dm.Height = 44
    dm.Title = "Vorlesen"
    dm.Closeable = True

    def add(kind, name, x, y, w, h, **props):
        m = dm.createInstance("com.sun.star.awt.UnoControl%sModel" % kind)
        m.Name = name
        m.PositionX = x
        m.PositionY = y
        m.Width = w
        m.Height = h
        for k, v in props.items():
            setattr(m, k, v)
        dm.insertByName(name, m)

    add("FixedText", "lblStatus", 6, 4, 200, 10, Label="⏳ Lädt …")
    add("Button", "btnPrev", 6, 17, 15, 15, Label="⏮")
    add("Button", "btnPause", 25, 17, 52, 15, Label="⏸  Pause")
    add("Button", "btnNext", 81, 17, 15, 15, Label="⏭")
    add("Button", "btnStop", 100, 17, 44, 15, Label="⏹  Stopp")
    add("Button", "btnSlow", 148, 17, 15, 15, Label="−")
    add("FixedText", "lblSpeed", 165, 20, 26, 10, Label="1,0×",
        Align=1)
    add("Button", "btnFast", 191, 17, 15, 15, Label="+")

    dlg = smgr.createInstanceWithContext(
        "com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(dm)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)

    parent_peer = None
    try:
        parent_peer = frame.getContainerWindow()
    except Exception as exc:
        log("kein Elternfenster: %s" % exc)
    try:
        dlg.createPeer(toolkit, parent_peer)
        log("Peer mit Elternfenster erstellt")
    except Exception as exc:
        log("Peer mit Elternfenster fehlgeschlagen (%s), versuche ohne" % exc)
        dlg.createPeer(toolkit, None)

    dlg.getControl("btnPrev").addActionListener(
        Btn(lambda: session.seek_sentence(-1)))
    dlg.getControl("btnPause").addActionListener(Btn(session.toggle_pause))
    dlg.getControl("btnNext").addActionListener(
        Btn(lambda: session.seek_sentence(+1)))
    dlg.getControl("btnStop").addActionListener(Btn(session.cleanup))
    dlg.getControl("btnSlow").addActionListener(
        Btn(lambda: session.change_speed(-0.1)))
    dlg.getControl("btnFast").addActionListener(
        Btn(lambda: session.change_speed(+0.1)))

    # Position: oben rechts innerhalb des Schreibfensters (Koordinaten
    # relativ zum Elternfenster, wenn ein Elternpeer gesetzt ist).
    try:
        dlgsize = dlg.getPosSize()
        if parent_peer is not None:
            win = parent_peer.getPosSize()
            px = max(10, win.Width - dlgsize.Width - 24)
            py = 60
        else:
            px, py = 200, 200
        dlg.setPosSize(px, py, 0, 0, 3)  # 3 = PosSize.POS (X|Y)
        log("Fenster positioniert bei %d,%d (Größe %dx%d)"
            % (px, py, dlgsize.Width, dlgsize.Height))
    except Exception as exc:
        log("Positionierung fehlgeschlagen: %s" % exc)

    dlg.setVisible(True)
    try:
        dlg.toFront()
    except Exception:
        pass
    log("Fenster sichtbar gesetzt")
    return dlg


def start_reading(*args):
    """Markierten Text (oder ab Cursor bis Ende) mit Edge-TTS vorlesen."""
    global _SESSION
    try:
        log("=== start_reading aufgerufen ===")
        if _SESSION is not None:
            _SESSION.cleanup()

        doc = XSCRIPTCONTEXT.getDocument()
        ctx = XSCRIPTCONTEXT.getComponentContext()
        smgr = ctx.getServiceManager()
        controller = doc.getCurrentController()
        otext = doc.getText()

        sel = doc.getCurrentSelection()
        rng = None
        if sel is not None \
                and sel.supportsService("com.sun.star.text.TextRanges") \
                and sel.getCount() > 0:
            cand = sel.getByIndex(0)
            if cand.getString().strip():
                rng = cand
        if rng is None:
            vc = controller.getViewCursor()
            cur = otext.createTextCursorByRange(vc.getStart())
            cur.gotoRange(otext.getEnd(), True)
            rng = cur

        raw = rng.getString()
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        sentences = split_sentences(normalized)
        log("%d Sätze erkannt" % len(sentences))
        if not sentences:
            return

        session = Session()
        session.ctx = ctx
        session.smgr = smgr
        session.async_cb = smgr.createInstanceWithContext(
            "com.sun.star.awt.AsyncCallback", ctx)
        session.controller = controller
        session.doc = doc
        session.text = otext
        session.anchor = otext.createTextCursorByRange(rng.getStart())
        session.sentences = sentences
        session.ready = [threading.Event() for _ in sentences]
        session.word_times = [None] * len(sentences)

        session.dlg = _build_dialog(ctx, smgr, controller.getFrame(), session)
        session.set_speed_label()
        _SESSION = session

        threading.Thread(target=session.synth_worker, daemon=True).start()
        threading.Thread(target=session.player_worker, daemon=True).start()
        log("Threads gestartet")
    except Exception:
        import traceback
        log("FEHLER in start_reading:\n" + traceback.format_exc())


def stop_reading(*args):
    """Laufende Vorlese-Sitzung beenden."""
    if _SESSION is not None:
        _SESSION.cleanup()


g_exportedScripts = (start_reading, stop_reading)
