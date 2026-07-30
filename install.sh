#!/bin/bash
# Installer for edge-tts-vorlesen (Ubuntu/GNOME/X11).
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
LO_SCRIPT_DIR="$HOME/.config/libreoffice/4/user/Scripts/python"
LO_POPUPMENU_DIR="$HOME/.config/libreoffice/4/user/config/soffice.cfg/modules/swriter/popupmenu"
LO_POPUPMENU_FILE="$LO_POPUPMENU_DIR/text.xml"
SD_MODULE_DIR="$HOME/.config/speech-dispatcher/modules"
CONF_FILE="$HOME/.config/vorlesen-miniplayer.json"

echo "==> Installiere System-Pakete (apt, benötigt sudo)"
sudo apt-get update -qq
sudo apt-get install -y \
    mpv xclip \
    python3-gi gir1.2-gtk-3.0 \
    libreoffice-script-provider-python \
    speech-dispatcher speech-dispatcher-audio-plugins \
    python3-pip

echo "==> Installiere edge-tts (Python, --user)"
if ! python3 -m pip install --user --upgrade edge-tts 2>/tmp/edge-tts-pip.log; then
    if grep -q "externally-managed-environment" /tmp/edge-tts-pip.log; then
        python3 -m pip install --user --break-system-packages --upgrade edge-tts
    else
        cat /tmp/edge-tts-pip.log >&2
        exit 1
    fi
fi
rm -f /tmp/edge-tts-pip.log

echo "==> Kopiere Programmdateien nach $BIN_DIR"
mkdir -p "$BIN_DIR"
install -m 755 "$REPO_DIR/src/system/vorlesen-auswahl.py" "$BIN_DIR/vorlesen-auswahl.py"
install -m 755 "$REPO_DIR/src/system/edge-tts-wrapper.sh" "$BIN_DIR/edge-tts-wrapper.sh"

echo "==> Installiere LibreOffice-Makro"
mkdir -p "$LO_SCRIPT_DIR"
install -m 644 "$REPO_DIR/src/libreoffice/vorlesen.py" "$LO_SCRIPT_DIR/vorlesen.py"

echo "==> Trage Kontextmenü-Eintrag 'Jetzt vorlesen' ein"
mkdir -p "$LO_POPUPMENU_DIR"
MENU_ENTRY='  <menu:menuitem menu:id="vnd.sun.star.script:vorlesen.py$start_reading?language=Python&amp;location=user" menu:label="Jetzt vorlesen"/>'
if [[ -f "$LO_POPUPMENU_FILE" ]]; then
    if ! grep -q 'vorlesen.py\$start_reading' "$LO_POPUPMENU_FILE"; then
        # Eintrag direkt nach der öffnenden <menu:menupopup ...> Zeile einfügen.
        awk -v entry="$MENU_ENTRY" '
            { print }
            /<menu:menupopup[^>]*>/ && !done { print entry; done=1 }
        ' "$LO_POPUPMENU_FILE" > "$LO_POPUPMENU_FILE.tmp"
        mv "$LO_POPUPMENU_FILE.tmp" "$LO_POPUPMENU_FILE"
        echo "    Eintrag ergänzt in bestehender $LO_POPUPMENU_FILE"
    else
        echo "    Eintrag bereits vorhanden, überspringe"
    fi
else
    install -m 644 "$REPO_DIR/libreoffice/popupmenu-text.xml" "$LO_POPUPMENU_FILE"
    echo "    Referenz-Kontextmenü installiert (keine bestehende Datei gefunden)"
fi

echo "==> Installiere speech-dispatcher-Modul 'edge'"
mkdir -p "$SD_MODULE_DIR"
install -m 644 "$REPO_DIR/config/speech-dispatcher/edge.conf" "$SD_MODULE_DIR/edge.conf"

echo "==> Konfiguration"
if [[ -f "$CONF_FILE" ]]; then
    echo "    $CONF_FILE existiert bereits, wird nicht überschrieben"
else
    install -m 644 "$REPO_DIR/config/vorlesen-miniplayer.json" "$CONF_FILE"
fi

echo "==> Setze globalen Tastaturkürzel Strg+Alt+V (GNOME)"
if command -v gsettings >/dev/null 2>&1; then
    BASE_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
    BASE_PATH="/org/gnome/settings-daemon/plugins/media-keys"
    KB_PATH="$BASE_PATH/custom-keybindings/vorlesen/"
    existing="$(gsettings get "$BASE_SCHEMA" custom-keybindings)"
    if [[ "$existing" != *"$KB_PATH"* ]]; then
        if [[ "$existing" == "@as []" || "$existing" == "[]" ]]; then
            new_list="['$KB_PATH']"
        else
            new_list="${existing%]}, '$KB_PATH']"
        fi
        gsettings set "$BASE_SCHEMA" custom-keybindings "$new_list"
    fi
    gsettings set "$BASE_SCHEMA.custom-keybinding:$KB_PATH" name "Vorlesen (Markierung)"
    gsettings set "$BASE_SCHEMA.custom-keybinding:$KB_PATH" command "python3 $BIN_DIR/vorlesen-auswahl.py"
    gsettings set "$BASE_SCHEMA.custom-keybinding:$KB_PATH" binding "<Control><Alt>v"
    echo "    Strg+Alt+V eingerichtet"
else
    echo "    gsettings nicht gefunden — Tastaturkürzel manuell in den"
    echo "    GNOME-Einstellungen unter 'Tastatur > Eigene Tastenkombinationen' anlegen:"
    echo "    Befehl: python3 $BIN_DIR/vorlesen-auswahl.py"
fi

echo
echo "Fertig. LibreOffice ggf. neu starten, damit das Kontextmenü und"
echo "das Makro geladen werden. Systemweites Vorlesen: Text markieren,"
echo "dann Strg+Alt+V."
