# edge-tts-vorlesen

A "Read Aloud" system for Ubuntu built on top of [`edge-tts`](https://github.com/rany2/edge-tts) (Microsoft Edge's neural text-to-speech voices), with a floating karaoke-style player that highlights the sentence and word currently being spoken.

Built because LibreOffice has no native Read Aloud feature (still true as of 25.8), and the official "Read Text" extension has no player controls or word highlighting.

## Features

- Natural-sounding neural TTS voices (via `edge-tts`), any language Edge TTS supports
- Live sentence + word highlighting synced to actual playback position
- Playback controls: pause/resume, stop, jump to previous/next sentence, live speed control (0.5x–3.0x)
- Two integrations that share one config file:
  - **LibreOffice Writer**: right-click → "Jetzt vorlesen" ("Read aloud now"), highlights text directly in the document
  - **System-wide**: select any text anywhere (browser, PDF viewer, terminal, ...), press **Ctrl+Alt+V**, get a floating GTK window that reads it aloud
- Optional `speech-dispatcher` module, so any application/AT that speaks through Speech Dispatcher can use Edge voices too

## How it works

| Component | Location | Purpose |
|---|---|---|
| `src/libreoffice/vorlesen.py` | LibreOffice user macro | Reads the current selection (or cursor-to-end), synthesizes each sentence with `edge-tts`, plays it with `mpv` over JSON-IPC, highlights the active sentence/word via `CharHighlight` directly in the Writer document |
| `src/system/vorlesen-auswahl.py` | `~/.local/bin` | Same synthesis/playback pipeline, but reads the X11 `PRIMARY` selection (falls back to clipboard) and shows a standalone GTK3 karaoke window instead of writing into a document |
| `src/system/edge-tts-wrapper.sh` | `~/.local/bin` | Thin wrapper so `speech-dispatcher`'s generic module can pipe text into `edge-playback` |
| `config/speech-dispatcher/edge.conf` | speech-dispatcher module | Registers `edge-tts` as a Speech Dispatcher output module with a few preset voices |
| `config/vorlesen-miniplayer.json` | shared config | Voice, playback speed, highlight colors |

Edge-TTS no longer returns per-word timestamps from the server (only sentence-boundary/VTT data), so word highlighting is **estimated**: each sentence's spoken duration is distributed across its words proportionally to word length plus a small pause weight for punctuation. It tracks actual media time, so it stays in sync even when you change playback speed mid-sentence.

## Requirements

- Ubuntu 24.04 or 26.04 (or similar), X11 session (GNOME tested; the system-wide hotkey uses `xclip`/GNOME custom keybindings and PRIMARY-selection semantics, so it needs X11 — Wayland is untested and the selection-grab likely won't work under it)
- Internet connection (Edge TTS synthesizes server-side)
- Packages installed automatically by `install.sh`: `mpv`, `xclip`, `python3-gi`, `gir1.2-gtk-3.0`, `libreoffice-script-provider-python`, `speech-dispatcher`, `speech-dispatcher-audio-plugins`, `pipx`
- Python package `edge-tts` (installed via `pipx`)

## Installation

```bash
git clone https://github.com/<your-username>/edge-tts-vorlesen.git
cd edge-tts-vorlesen
./install.sh
```

The script is idempotent — safe to re-run. It will:

1. Install the required apt packages (asks for `sudo`)
2. Install/upgrade the `edge-tts` Python package for your user
3. Copy the two Python programs into `~/.local/bin` and the LibreOffice user scripts folder
4. Add the "Jetzt vorlesen" entry to LibreOffice Writer's text context menu (patches your existing menu file if you have one, otherwise installs the reference file in `libreoffice/popupmenu-text.xml`)
5. Install the `speech-dispatcher` Edge voice module
6. Write a default `~/.config/vorlesen-miniplayer.json` if none exists yet (won't overwrite an existing one)
7. Register the **Ctrl+Alt+V** global shortcut via `gsettings` (GNOME custom keybinding)

Restart LibreOffice afterwards so it picks up the macro and the context menu entry.

To remove everything again: `./uninstall.sh` (leaves apt packages, `edge-tts` and your config file in place).

## Usage

**In LibreOffice Writer:** select some text (or just place the cursor — it will read to the end of the document), right-click, choose **"Jetzt vorlesen"**.

**Anywhere else:** select/highlight text with the mouse, press **Ctrl+Alt+V**. A small floating window appears with the text, reading progress highlighted sentence-by-sentence and word-by-word.

Controls in both players: `⏮`/`⏭` previous/next sentence, space or the pause button to pause/resume, `−`/`+` to change speed, Esc or Stop to close. In the system-wide window, arrow keys ←/→ also jump sentences.

## Configuration

Edit `~/.config/vorlesen-miniplayer.json`:

```json
{
  "voice": "de-DE-KillianNeural",
  "speed": 1.0,
  "highlight": 14540253,
  "word": 16767926
}
```

- `voice`: any Edge TTS voice name. List them with `edge-tts --list-voices`.
- `speed`: initial playback speed (0.5–3.0), also updated live whenever you change it in the player.
- `highlight` / `word`: sentence/word highlight colors as decimal RGB integers (e.g. `0xDDDDDD` = `14540253`).

## Limitations

- Word-level timing is estimated, not exact (see "How it works" above) — it's usually good enough to follow along, but won't perfectly match consonant-heavy or very short words.
- Relies on Microsoft's Edge Read Aloud backend through the unofficial, reverse-engineered [`edge-tts`](https://github.com/rany2/edge-tts) client. It's not an official API and could change or stop working without notice; this project isn't affiliated with Microsoft.
- X11-only; not tested under Wayland.
- UI strings in both players are German (the author's language) — pull requests adding i18n/English strings are welcome.

## Troubleshooting

**The player window opens and closes again immediately.** Synthesis or playback failed silently. This typically happens after an Ubuntu release upgrade (e.g. 24.04 → 26.04): the Python version changes, which breaks the `edge-tts` virtualenv, and `mpv` may get removed. Check:

```bash
edge-tts --text "Test" --write-media /tmp/test.mp3   # must create the file
which mpv                                             # must print a path
```

Fix by re-running `./install.sh`, or manually:

```bash
pipx reinstall --python python3 edge-tts
sudo apt install mpv
```

## License

MIT — see [LICENSE](LICENSE).
