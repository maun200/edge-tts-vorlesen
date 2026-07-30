#!/bin/bash
# Removes files and settings installed by install.sh.
# System packages (mpv, xclip, ...) are left in place since other
# software may depend on them.
set -Eeuo pipefail

BIN_DIR="$HOME/.local/bin"
LO_SCRIPT_DIR="$HOME/.config/libreoffice/4/user/Scripts/python"
LO_POPUPMENU_FILE="$HOME/.config/libreoffice/4/user/config/soffice.cfg/modules/swriter/popupmenu/text.xml"
SD_MODULE_FILE="$HOME/.config/speech-dispatcher/modules/edge.conf"

echo "==> Entferne Programmdateien"
rm -f "$BIN_DIR/vorlesen-auswahl.py" "$BIN_DIR/edge-tts-wrapper.sh"
rm -f "$LO_SCRIPT_DIR/vorlesen.py"
rm -f "$SD_MODULE_FILE"

echo "==> Entferne Kontextmenü-Eintrag"
if [[ -f "$LO_POPUPMENU_FILE" ]] && grep -q 'vorlesen.py\$start_reading' "$LO_POPUPMENU_FILE"; then
    grep -v 'vorlesen.py\$start_reading' "$LO_POPUPMENU_FILE" > "$LO_POPUPMENU_FILE.tmp"
    mv "$LO_POPUPMENU_FILE.tmp" "$LO_POPUPMENU_FILE"
fi

echo "==> Entferne globales Tastaturkürzel Strg+Alt+V"
if command -v gsettings >/dev/null 2>&1; then
    BASE_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
    BASE_PATH="/org/gnome/settings-daemon/plugins/media-keys"
    KB_PATH="$BASE_PATH/custom-keybindings/vorlesen/"
    existing="$(gsettings get "$BASE_SCHEMA" custom-keybindings)"
    if [[ "$existing" == *"$KB_PATH"* ]]; then
        python3 - "$existing" "$KB_PATH" "$BASE_SCHEMA" <<'PYEOF'
import ast, subprocess, sys
existing, kb_path, schema = sys.argv[1], sys.argv[2], sys.argv[3]
paths = ast.literal_eval(existing.replace("@as ", ""))
paths = [p for p in paths if p != kb_path]
value = "[" + ", ".join("'%s'" % p for p in paths) + "]" if paths else "@as []"
subprocess.run(["gsettings", "set", schema, "custom-keybindings", value], check=True)
PYEOF
    fi
fi

echo
echo "Fertig. Konfigurationsdatei ~/.config/vorlesen-miniplayer.json und"
echo "das Python-Paket edge-tts wurden absichtlich NICHT entfernt."
