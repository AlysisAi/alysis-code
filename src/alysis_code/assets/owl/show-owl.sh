#!/usr/bin/env bash
# alysis terminal owl playback.

ESC=$'\e'
RESET="${ESC}[0m"
BOLD="${ESC}[1m"
HIDE="${ESC}[?25l"
SHOW="${ESC}[?25h"
CLEAR="${ESC}[2J${ESC}[H"
EL="${ESC}[K"

DIR="$(cd "$(dirname "$0")" && pwd)"
CANONICAL_ASCII_DIR="${OWL_ASCII_DIR:-$DIR/ascii}"
ASCII_DIR="$CANONICAL_ASCII_DIR"
SPEED="${OWL_SPEED:-0.10}"
THEME="${OWL_THEME:-auto}"
LOOPS=0
SHOW_TEXT=1
CLEAR_ON_EXIT=1

usage() {
  cat <<EOF
Usage: ./show-owl.sh [options]

Options:
  --speed SECONDS   Delay between frames. Default: $SPEED
  --loops N         Play N loops, then exit. Default: forever
  --once            Same as --loops 1, leaving the final frame visible
  --theme THEME     Theme: auto, light, dark, or neutral. Default: $THEME
  --no-text         Show only the owl animation
  -h, --help        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --speed)
      SPEED="${2:-}"
      shift 2
      ;;
    --loops)
      LOOPS="${2:-}"
      shift 2
      ;;
    --once)
      LOOPS=1
      CLEAR_ON_EXIT=0
      shift
      ;;
    --theme)
      THEME="${2:-}"
      shift 2
      ;;
    --no-text)
      SHOW_TEXT=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$SPEED" in
  ''|*[!0-9.]*)
    echo "--speed must be a number of seconds, for example 0.08" >&2
    exit 2
    ;;
esac

case "$LOOPS" in
  ''|*[!0-9]*)
    echo "--loops must be a positive integer" >&2
    exit 2
    ;;
esac

case "$THEME" in
  auto|light|dark|neutral)
    ;;
  *)
    echo "--theme must be auto, light, dark, or neutral" >&2
    exit 2
    ;;
esac

if [[ -n "${ALYSIS_NO_OWL:-}" || -n "${ALYSIS_NO_INTRO:-}" ]]; then
  exit 0
fi
if [[ -n "${CI:-}" || -n "${ALYSIS_CI:-}" ]]; then
  exit 0
fi
if [[ ! -t 1 || "${TERM:-}" == "dumb" ]]; then
  exit 0
fi

hex_to_8bit() {
  local h="$1"
  local max=1
  local i
  for ((i=0; i<${#h}; i++)); do
    max=$((max * 16))
  done
  max=$((max - 1))
  printf '%s' $(( (16#$h) * 255 / max ))
}

terminal_color_count() {
  local colors
  colors="$(tput colors 2>/dev/null || printf '0')"
  case "$colors" in
    ''|*[!0-9-]*)
      printf '0'
      ;;
    *)
      printf '%s' "$colors"
      ;;
  esac
}

terminal_supports_truecolor() {
  case "${COLORTERM:-}" in
    truecolor|24bit)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_wsl() {
  if [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]]; then
    return 0
  fi
  if [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    return 0
  fi
  return 1
}

set_text_palette() {
  local theme="$1"

  if (( ! USE_COLOR )); then
    BOLD=""
    INK=""
    ACC=""
    TAG_INK=""
    GREY=""
    return 0
  fi

  if [[ "$theme" == "neutral" || "$theme" == "auto" ]]; then
    INK=""
    ACC=""
    TAG_INK=""
    GREY=""
    return 0
  fi

  if terminal_supports_truecolor; then
    case "$theme" in
      light)
        INK="${ESC}[38;2;30;30;30m"
        ACC="${ESC}[38;2;230;102;32m"
        TAG_INK="${ESC}[38;2;30;30;30m"
        GREY="${ESC}[38;2;30;30;30m"
        ;;
      dark)
        INK="${ESC}[38;2;230;230;230m"
        ACC="${ESC}[38;2;255;145;77m"
        TAG_INK="${ESC}[38;2;230;230;230m"
        GREY="${ESC}[38;2;230;230;230m"
        ;;
    esac
    return 0
  fi

  if (( COLOR_COUNT >= 256 )); then
    case "$theme" in
      light)
        INK="${ESC}[38;5;16m"
        ACC="${ESC}[38;5;202m"
        TAG_INK="${ESC}[38;5;16m"
        GREY="${ESC}[38;5;16m"
        ;;
      dark)
        INK="${ESC}[38;5;255m"
        ACC="${ESC}[38;5;215m"
        TAG_INK="${ESC}[38;5;255m"
        GREY="${ESC}[38;5;255m"
        ;;
    esac
    return 0
  fi

  case "$theme" in
    light)
      INK="${ESC}[30m"
      ACC="${ESC}[31m"
      TAG_INK="${ESC}[30m"
      GREY="${ESC}[30m"
      ;;
    dark)
      INK="${ESC}[97m"
      ACC="${ESC}[33m"
      TAG_INK="${ESC}[97m"
      GREY="${ESC}[97m"
      ;;
  esac
}

strip_ansi_file() {
  python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

csi = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
print(csi.sub("", Path(sys.argv[1]).read_text(errors="replace")), end="")
PY
}

white_panel_file() {
  python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

bg = "\x1b[48;5;231m"
reset = "\x1b[0m"
csi = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
lines = Path(sys.argv[1]).read_text(errors="replace").splitlines()
width = max((len(csi.sub("", line)) for line in lines), default=0)
side_pad = "  "
blank = f"{bg}{' ' * (width + 4)}{reset}"
painted = [blank]

# Runtime-only contrast remap for light owl grays on the white panel.
panel_gray_map = {
    "238": "16",
    "239": "16",
    "240": "16",
    "241": "234",
    "242": "234",
    "243": "234",
    "244": "236",
    "245": "236",
    "246": "236",
    "247": "238",
    "248": "238",
}

def remap_panel_grays(line):
    return re.sub(
        r"\x1b\[38;5;(238|239|240|241|242|243|244|245|246|247|248)m",
        lambda match: f"\x1b[38;5;{panel_gray_map[match.group(1)]}m",
        line,
    )

for line in lines:
    contrast = remap_panel_grays(line)
    safe = contrast.replace(reset, f"{reset}{bg}")
    pad = " " * max(width - len(csi.sub("", line)), 0)
    painted.append(f"{bg}{side_pad}{safe}{pad}{side_pad}{reset}")
painted.append(blank)
print("\n".join(painted), end="")
PY
}

detect_terminal_theme() {
  local bg response old_tty r g b brightness

  # Some terminals expose foreground/background ANSI color indexes here.
  if [[ -n "${COLORFGBG:-}" ]]; then
    bg="${COLORFGBG##*;}"
    case "$bg" in
      ''|*[!0-9]*)
        ;;
      *)
        if (( bg <= 6 || bg == 8 )); then
          printf 'dark'
        else
          printf 'light'
        fi
        return 0
        ;;
    esac
  fi

  # OSC 11 asks the terminal for its current background color. iTerm2,
  # Apple Terminal, kitty, WezTerm, and many xterm-compatible terminals
  # answer with rgb:RRRR/GGGG/BBBB.
  if [[ "${ALYSIS_ENABLE_OSC11:-}" =~ ^(1|true|yes|on)$ ]] && ! is_wsl && tty -s && [[ -r /dev/tty && -w /dev/tty ]]; then
    old_tty="$(stty -g < /dev/tty 2>/dev/null || true)"
    if [[ -n "$old_tty" ]]; then
      stty raw -echo min 0 time 5 < /dev/tty 2>/dev/null || true
      printf '\033]11;?\007' > /dev/tty
      response="$(dd bs=1 count=128 2>/dev/null < /dev/tty || true)"
      stty "$old_tty" < /dev/tty 2>/dev/null || true
      if [[ "$response" =~ rgb:([0-9A-Fa-f]+)/([0-9A-Fa-f]+)/([0-9A-Fa-f]+) ]]; then
        r="$(hex_to_8bit "${BASH_REMATCH[1]}")"
        g="$(hex_to_8bit "${BASH_REMATCH[2]}")"
        b="$(hex_to_8bit "${BASH_REMATCH[3]}")"
        brightness=$(( (r * 299 + g * 587 + b * 114) / 1000 ))
        if (( brightness >= 128 )); then
          printf 'light'
        else
          printf 'dark'
        fi
        return 0
      fi
    fi
  fi

  if [[ -n "${WT_SESSION:-}" || -n "${ALYSIS_WINDOWS_TERMINAL_SETTINGS:-}" ]] || is_wsl; then
    local wt_theme
    wt_theme="$(python3 <<'PY' 2>/dev/null || true
import glob
import json
import os
import re
from pathlib import Path


def strip_json_comments(value):
    output = []
    in_string = escaped = in_line = in_block = False
    index = 0
    while index < len(value):
        char = value[index]
        nxt = value[index + 1] if index + 1 < len(value) else ""
        if in_line:
            if char == "\n":
                in_line = False
                output.append(char)
            index += 1
            continue
        if in_block:
            if char == "*" and nxt == "/":
                in_block = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            in_line = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            in_block = True
            index += 2
            continue
        output.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(output))


def settings_paths():
    override = os.environ.get("ALYSIS_WINDOWS_TERMINAL_SETTINGS")
    if override:
        return [Path(override).expanduser()]
    paths = []
    drives = [*glob.glob("/mnt/[a-z]"), *glob.glob("/mnt/host/[a-z]")]
    for drive in sorted(dict.fromkeys(drives)):
        for user in sorted(glob.glob(f"{drive}/Users/*")):
            local = Path(user) / "AppData" / "Local"
            paths.extend(
                Path(path)
                for path in sorted(
                    glob.glob(
                        str(
                            local
                            / "Packages"
                            / "Microsoft.WindowsTerminal*_8wekyb3d8bbwe"
                            / "LocalState"
                            / "settings.json"
                        )
                    )
                )
            )
            paths.append(local / "Microsoft" / "Windows Terminal" / "settings.json")
    return paths


def background_theme(value):
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        return None
    red = int(raw[0:2], 16)
    green = int(raw[2:4], 16)
    blue = int(raw[4:6], 16)
    brightness = (red * 299 + green * 587 + blue * 114) // 1000
    return "light" if brightness >= 128 else "dark"


def builtin_theme(name):
    known = {
        "atom one dark": "dark",
        "atom one light": "light",
        "ayu dark": "dark",
        "ayu light": "light",
        "ayu mirage": "dark",
        "campbell": "dark",
        "campbell powershell": "dark",
        "dark+": "dark",
        "dracula": "dark",
        "dracula+": "dark",
        "github dark": "dark",
        "github light": "light",
        "gruvbox dark": "dark",
        "gruvbox light": "light",
        "light+": "light",
        "material dark": "dark",
        "material light": "light",
        "monokai": "dark",
        "monokai pro": "dark",
        "nord": "dark",
        "one half dark": "dark",
        "one half light": "light",
        "powershell": "dark",
        "solarized dark": "dark",
        "solarized light": "light",
        "tango dark": "dark",
        "tango light": "light",
        "tokyo night": "dark",
        "tokyo night light": "light",
        "tokyo night storm": "dark",
        "ubuntu": "dark",
        "vintage": "dark",
    }
    normalized = re.sub(r"[^a-z0-9+]+", " ", name.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return known.get(normalized)


def scheme_name(settings):
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile_list = profiles.get("list") if isinstance(profiles.get("list"), list) else []
    defaults = profiles.get("defaults") if isinstance(profiles.get("defaults"), dict) else {}
    profile_id = os.environ.get("WT_PROFILE_ID", "").strip().strip("{}").lower()
    profile = None
    if profile_id:
        for candidate in profile_list:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("guid", "")).strip().strip("{}").lower() == profile_id:
                profile = candidate
                break
    if profile is None:
        default_profile = str(settings.get("defaultProfile", "")).strip().strip("{}").lower()
        for candidate in profile_list:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("guid", "")).strip().strip("{}").lower() == default_profile:
                profile = candidate
                break
    value = (profile or {}).get("colorScheme")
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = defaults.get("colorScheme")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Campbell"


for path in settings_paths():
    try:
        settings = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
    except Exception:
        continue
    name = scheme_name(settings)
    if not name:
        continue
    schemes = settings.get("schemes") if isinstance(settings.get("schemes"), list) else []
    for scheme in schemes:
        if not isinstance(scheme, dict):
            continue
        if str(scheme.get("name", "")).strip().lower() != name.lower():
            continue
        theme = background_theme(scheme.get("background"))
        if theme:
            print(theme)
            raise SystemExit
    theme = builtin_theme(name)
    if theme:
        print(theme)
        raise SystemExit
PY
)"
    case "$wt_theme" in
      light|dark)
        printf '%s' "$wt_theme"
        return 0
        ;;
    esac
    if [[ -n "${WT_SESSION:-}" ]]; then
      printf 'dark'
      return 0
    fi
  fi

  if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]] && command -v defaults >/dev/null 2>&1; then
    local apple_style
    apple_style="$(defaults read -g AppleInterfaceStyle 2>/dev/null || true)"
    if [[ "$apple_style" == "Dark" ]]; then
      printf 'dark'
    else
      printf 'light'
    fi
    return 0
  fi

  case "${ALYSIS_FALLBACK_THEME:-}" in
    light|dark)
      printf '%s' "$ALYSIS_FALLBACK_THEME"
      return 0
      ;;
  esac
  case "${OWL_FALLBACK_THEME:-}" in
    light|dark)
      printf '%s' "$OWL_FALLBACK_THEME"
      return 0
      ;;
  esac
  printf 'neutral'
}

case "$THEME" in
  auto)
    THEME="$(detect_terminal_theme)"
    ;;
  light|dark|neutral)
    ;;
  *)
    echo "--theme must be auto, light, dark, or neutral" >&2
    exit 2
    ;;
esac

USE_COLOR=1
STRIP_FRAME_COLORS=0
COLOR_COUNT="$(terminal_color_count)"
if [[ -n "${NO_COLOR:-}" ]]; then
  USE_COLOR=0
  STRIP_FRAME_COLORS=1
elif ! terminal_supports_truecolor && (( COLOR_COUNT < 256 )); then
  STRIP_FRAME_COLORS=1
fi

case "$THEME" in
  light)
    ASCII_DIR="$CANONICAL_ASCII_DIR"
    ;;
  dark)
    ASCII_DIR="$CANONICAL_ASCII_DIR"
    ;;
  neutral)
    ASCII_DIR="$CANONICAL_ASCII_DIR"
    ;;
esac
set_text_palette "$THEME"
WHITE_OWL_PANEL=0
if [[ "$THEME" == "dark" ]] && (( USE_COLOR )) && (( ! STRIP_FRAME_COLORS )); then
  WHITE_OWL_PANEL=1
fi

cleanup() {
  printf '%s%s' "$RESET" "$SHOW"
  if (( CLEAR_ON_EXIT )); then
    printf '%s' "$CLEAR"
  fi
}
trap cleanup EXIT
trap 'trap - EXIT; cleanup; exit 130' INT TERM
RESIZED=0
trap 'RESIZED=1' WINCH

if [[ ! -d "$ASCII_DIR" ]] || ! ls "$ASCII_DIR"/f-*.txt >/dev/null 2>&1; then
  echo "Pre-rendered frames not found in $ASCII_DIR" >&2
  echo "Run ./build_terminal_owl.sh first." >&2
  exit 1
fi

# Pre-load frames into array (bash 3.2-compatible)
FRAMES=()
FRAME_ROWS=0
FRAME_COLS=0
for f in "$ASCII_DIR"/f-*.txt; do
  if (( STRIP_FRAME_COLORS )); then
    FRAMES+=("$(strip_ansi_file "$f")")
  elif (( WHITE_OWL_PANEL )); then
    FRAMES+=("$(white_panel_file "$f")")
  else
    FRAMES+=("$(cat "$f")")
  fi
  rows=$(awk 'END { print NR }' "$f")
  if (( rows > FRAME_ROWS )); then
    FRAME_ROWS=$rows
  fi
done

read frame_cols frame_rows <<EOF
$(python3 - "$ASCII_DIR" <<'PY'
import re
import sys
from pathlib import Path

csi = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
cols = rows = 0
for path in sorted(Path(sys.argv[1]).glob("f-*.txt"))[:1]:
    lines = csi.sub("", path.read_text(errors="replace")).splitlines()
    rows = len(lines)
    cols = max((len(line) for line in lines), default=0)
print(cols or 50, rows or 13)
PY
)
EOF

FRAME_COLS="${frame_cols:-50}"
FRAME_ROWS="${frame_rows:-$FRAME_ROWS}"
if (( WHITE_OWL_PANEL )); then
  FRAME_COLS=$((FRAME_COLS + 4))
  FRAME_ROWS=$((FRAME_ROWS + 2))
fi
TEXT_WIDTH=35
TEXT_ROWS=4
TEXT_GAP=2

term_cols() { tput cols 2>/dev/null || printf '80'; }
term_rows() { tput lines 2>/dev/null || printf '24'; }

compute_layout() {
  COLS="$(term_cols)"
  ROWS="$(term_rows)"

  if (( SHOW_TEXT && ROWS < FRAME_ROWS + TEXT_GAP + TEXT_ROWS )); then
    TEXT_VISIBLE=0
  else
    TEXT_VISIBLE="$SHOW_TEXT"
  fi

  if (( TEXT_VISIBLE )); then
    CANVAS_WIDTH=$(( FRAME_COLS > TEXT_WIDTH ? FRAME_COLS : TEXT_WIDTH ))
    CANVAS_HEIGHT=$(( FRAME_ROWS + TEXT_GAP + TEXT_ROWS ))
  else
    CANVAS_WIDTH="$FRAME_COLS"
    CANVAS_HEIGHT="$FRAME_ROWS"
  fi

  if (( COLS > CANVAS_WIDTH )); then
    CANVAS_LEFT=$(( (COLS - CANVAS_WIDTH) / 2 + 1 ))
  else
    CANVAS_LEFT=1
  fi

  if (( ROWS > CANVAS_HEIGHT )); then
    CANVAS_TOP=$(( (ROWS - CANVAS_HEIGHT) / 2 + 1 ))
  else
    CANVAS_TOP=1
  fi

  FRAME_LEFT=$(( CANVAS_LEFT + (CANVAS_WIDTH - FRAME_COLS) / 2 ))
  FRAME_TOP="$CANVAS_TOP"
  TEXT_LEFT=$(( CANVAS_LEFT + (CANVAS_WIDTH - TEXT_WIDTH) / 2 ))
  TEXT_TOP=$(( FRAME_TOP + FRAME_ROWS + TEXT_GAP ))
  LAYOUT_KEY="${COLS}:${ROWS}:${CANVAS_LEFT}:${CANVAS_TOP}:${CANVAS_WIDTH}:${CANVAS_HEIGHT}:${TEXT_VISIBLE}"
}

clear_canvas() {
  local width=$1
  local height=$2
  local row=$3
  local left=$4
  local i
  for ((i=0; i<height; i++)); do
    printf "${ESC}[%s;%sH%*s" "$((row + i))" "$left" "$width" ""
  done
}

draw_text() {
  (( TEXT_VISIBLE )) || return 0
  printf "${ESC}[%s;%sH%*s" "$TEXT_TOP" "$TEXT_LEFT" "$TEXT_WIDTH" ""
  printf "${ESC}[%s;%sH       ${BOLD}${ACC}■${RESET}  ${BOLD}${INK}alysis${RESET}" "$TEXT_TOP" "$TEXT_LEFT"
  printf "${ESC}[%s;%sH%*s" "$((TEXT_TOP + 1))" "$TEXT_LEFT" "$TEXT_WIDTH" ""
  printf "${ESC}[%s;%sH          ${TAG_INK}the autonomous coding agent${RESET}" "$((TEXT_TOP + 1))" "$TEXT_LEFT"
  printf "${ESC}[%s;%sH%*s" "$((TEXT_TOP + 2))" "$TEXT_LEFT" "$TEXT_WIDTH" ""
  printf "${ESC}[%s;%sH          ${GREY}.  ${ACC}alysis ai${GREY}  .${RESET}" "$((TEXT_TOP + 2))" "$TEXT_LEFT"
  printf "${ESC}[%s;%sH%*s" "$((TEXT_TOP + 3))" "$TEXT_LEFT" "$TEXT_WIDTH" ""
  printf "${ESC}[%s;%sH       ${GREY}-----------------------------${RESET}" "$((TEXT_TOP + 3))" "$TEXT_LEFT"
}

draw_frame() {
  local frame=$1
  local row="$FRAME_TOP"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    printf "${ESC}[%s;%sH%*s" "$row" "$FRAME_LEFT" "$FRAME_COLS" ""
    printf "${ESC}[%s;%sH%s${RESET}" "$row" "$FRAME_LEFT" "$line"
    row=$((row + 1))
  done <<< "$frame"
}

# Setup screen
printf '%s%s' "$HIDE" "$CLEAR"
compute_layout
clear_canvas "$CANVAS_WIDTH" "$CANVAS_HEIGHT" "$CANVAS_TOP" "$CANVAS_LEFT"
draw_text
LAST_LAYOUT_KEY="$LAYOUT_KEY"

# Animation loop: redraw frame at absolute positions each tick.
# GIF is ~10fps, so sleep 0.1s per frame.
loop=0
while true; do
  for frame in "${FRAMES[@]}"; do
    compute_layout
    if [[ "${LAYOUT_KEY}" != "${LAST_LAYOUT_KEY:-}" ]] || (( RESIZED )); then
      printf '%s' "$CLEAR"
      clear_canvas "$CANVAS_WIDTH" "$CANVAS_HEIGHT" "$CANVAS_TOP" "$CANVAS_LEFT"
      LAST_LAYOUT_KEY="$LAYOUT_KEY"
      RESIZED=0
    else
      clear_canvas "$CANVAS_WIDTH" "$FRAME_ROWS" "$FRAME_TOP" "$CANVAS_LEFT"
    fi
    draw_frame "$frame"
    draw_text
    sleep "$SPEED"
  done
  if (( LOOPS > 0 )); then
    loop=$((loop + 1))
    if (( loop >= LOOPS )); then
      break
    fi
  fi
done
