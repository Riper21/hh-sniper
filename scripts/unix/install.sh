#!/usr/bin/env bash
# Автозапуск hh-snpr на Linux/macOS (этап 2 кроссплатформенности, 18.10).
#
# Linux:  systemd user-timer — каждую минуту `daemon --once` (модель
#         Windows-джобы: процесс не висит в памяти, systemd будит сам).
#         Автозапуск после ребута: да (timers.target + linger).
# macOS:  launchd LaunchAgent com.hh.snpr — StartInterval 60 с,
#         автозапуск при логине (~/Library/LaunchAgents).
#
# Без автозапуска (лёгкий путь): tmux new -s hh-snpr \
#   "<корень>/.venv/bin/python -X utf8 -m snpr daemon"
# Сигнал «есть на approve» на не-Windows — BEL терминала, где живёт демон
# (в tmux при тишине: tmux set -g monitor-bell on).
#
# Установка:  bash scripts/unix/install.sh
# Снятие:     bash scripts/unix/install.sh --remove
# (демон, запущенный руками, этими командами не управляется — только юнит)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ОШИБКА: python3 не найден (и нет .venv/bin/python)."
    exit 1
  fi
  PY="$(command -v python3)"
fi
DATA_DIR="$ROOT/data"
mkdir -p "$DATA_DIR"

# systemd-экранирование пути в юните: % → %%, " → \"
sysd_quote() {
  local s="$1"
  s="${s//%/%%}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# XML-экранирование для plist
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  printf '%s' "$s"
}

remove() {
  case "$(uname)" in
    Linux)
      systemctl --user disable --now hh-snpr.timer 2>/dev/null || true
      rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hh-snpr.timer" \
            "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hh-snpr.service"
      systemctl --user daemon-reload 2>/dev/null || true
      echo "Снято: systemd user-timer hh-snpr."
      ;;
    Darwin)
      launchctl unload "$HOME/Library/LaunchAgents/com.hh.snpr.plist" 2>/dev/null || true
      rm -f "$HOME/Library/LaunchAgents/com.hh.snpr.plist"
      echo "Снято: launchd com.hh.snpr."
      ;;
    *)
      echo "Не знаю, как снимать на $(uname)."
      exit 1
      ;;
  esac
  echo "Демон, запущенный руками, не тронут (если был)."
}

install_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "ОШИБКА: systemctl не найден — это Linux без systemd?"
    echo "Используйте демон руками: $PY -X utf8 -m snpr daemon (или в tmux)."
    exit 1
  fi
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"

  cat > "$unit_dir/hh-snpr.service" <<EOF
[Unit]
Description=hh-snpr: один тик конвейера (sne --mode park)

[Service]
Type=oneshot
# тик сам пишет лог: $DATA_DIR/scheduler.log (subprocess с выводом туда)
WorkingDirectory=$(sysd_quote "$ROOT")
ExecStart=$(sysd_quote "$PY") -X utf8 -m snpr daemon --once
EOF

  cp "$SCRIPT_DIR/hh-snpr.timer" "$unit_dir/hh-snpr.timer"

  systemctl --user daemon-reload
  systemctl --user enable --now hh-snpr.timer

  # тик без открытой сессии (после логаута / на VPS): linger
  local linger="не проверен"
  if command -v loginctl >/dev/null 2>&1; then
    if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo no)" = "yes" ]; then
      linger="включён"
    elif loginctl enable-linger "$USER" 2>/dev/null; then
      linger="включён"
    else
      linger="нет — для тика без сессии: sudo loginctl enable-linger $USER"
    fi
  fi

  echo "Установлено: systemd user-timer hh-snpr (каждую минуту; linger: $linger)."
  echo "Проверка:   systemctl --user list-timers hh-snpr.timer"
  echo "Такт сразу: systemctl --user start hh-snpr.service (или: python -m snpr job run)"
  echo "Лог тика:   tail -f $DATA_DIR/scheduler.log"
}

install_launchd() {
  local la_dir="$HOME/Library/LaunchAgents"
  mkdir -p "$la_dir"
  launchctl unload "$la_dir/com.hh.snpr.plist" 2>/dev/null || true

  cat > "$la_dir/com.hh.snpr.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hh.snpr</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "$PY")</string>
        <string>-X</string>
        <string>utf8</string>
        <string>-m</string>
        <string>snpr</string>
        <string>daemon</string>
        <string>--once</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml_escape "$ROOT")</string>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$DATA_DIR/launchd.out.log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$DATA_DIR/launchd.out.log")</string>
</dict>
</plist>
EOF

  launchctl load "$la_dir/com.hh.snpr.plist"
  echo "Установлено: launchd com.hh.snpr (StartInterval 60 с, при логине)."
  echo "Проверка:   launchctl list | grep com.hh.snpr"
  echo "Лог тика:   tail -f $DATA_DIR/scheduler.log (сводки демона: launchd.out.log)"
}

ARG="${1:-}"
if [ "$ARG" = "--remove" ]; then
  remove
elif [ "$ARG" = "" ] || [ "$ARG" = "install" ]; then
  case "$(uname)" in
    Linux)  install_systemd ;;
    Darwin) install_launchd ;;
    *)
      echo "ОС $(uname) без автозапуска — демон руками:"
      echo "  $PY -X utf8 -m snpr daemon   (Ctrl-C — стоп)"
      exit 1
      ;;
  esac
else
  echo "Использование: bash scripts/unix/install.sh [--remove]"
  exit 2
fi
