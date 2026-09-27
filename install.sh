#!/usr/bin/env bash
# issquatch installer for Omarchy (Lua Hyprland config).
#
#   ./install.sh               install from this checkout
#   curl -fsSL https://raw.githubusercontent.com/squatchware/issquatch/main/install.sh | bash
#                              clone to ~/.local/share/issquatch/src and install
#   ./install.sh --uninstall   remove everything except your settings (~/.config/issquatch)
#
# Environment:
#   ISSQUATCH_KEY="SUPER + ALT + I"   keybinding that opens or focuses the map ("none" to skip)
#   ISSQUATCH_NOTIFY=0                don't enable the pass-notification timer
set -euo pipefail

REPO="https://github.com/squatchware/issquatch.git"
KEY="${ISSQUATCH_KEY:-SUPER + ALT + I}"
NOTIFY="${ISSQUATCH_NOTIFY:-1}"

HYPR="$HOME/.config/hypr"
MODULE="$HYPR/issquatch.lua"
REQUIRE='require("default.hypr.require_optional").module("hypr.issquatch") -- issquatch'
DATA="$HOME/.local/share/issquatch"
VENV="$DATA/venv"
CLONE="$DATA/src"
BIN="$HOME/.local/bin/issquatch"
DESKTOP="$HOME/.local/share/applications/issquatch.desktop"
UNITS="$HOME/.config/systemd/user"

say() { printf '\033[1;33m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }
backup() { [[ -f $1 ]] && cp "$1" "$1.bak.$(date +%Y%m%d-%H%M%S)"; }

uninstall() {
  say "Removing issquatch"
  systemctl --user disable --now issquatch-notify.timer >/dev/null 2>&1 || true
  rm -f "$UNITS/issquatch-notify.service" "$UNITS/issquatch-notify.timer"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  rm -f "$MODULE" "$DESKTOP"
  [[ -L $BIN ]] && rm -f "$BIN"
  if grep -qF -- "-- issquatch" "$HYPR/hyprland.lua" 2>/dev/null; then
    backup "$HYPR/hyprland.lua"
    sed -i '/-- issquatch$/d' "$HYPR/hyprland.lua"
  fi
  hyprctl reload >/dev/null 2>&1 || true
  rm -rf "$DATA"
  say "Done. Your location in ~/.config/issquatch was kept."
}

[[ ${1:-} == --uninstall ]] && { uninstall; exit 0; }

# ---------- preflight ----------
command -v python3 >/dev/null || die "Missing python3."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "issquatch needs Python 3.11 or newer."

# ---------- source ----------
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n $here && -f $here/issquatch.py ]]; then
  dir="$here"
else
  command -v git >/dev/null || die "Missing git. Try: omarchy pkg add git"
  if [[ -d $CLONE/.git ]]; then
    say "Updating $CLONE"
    git -C "$CLONE" pull --ff-only
  else
    say "Cloning into $CLONE"
    git clone --depth 1 "$REPO" "$CLONE"
  fi
  dir="$CLONE"
fi

# ---------- python ----------
say "Setting up Python (sgp4, the standard orbit propagator)"
[[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade 'sgp4>=2.23'

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
ln -sf "$dir/bin/issquatch" "$BIN"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Name=issquatch
Comment=Track the International Space Station
Exec=omarchy-launch-or-focus-tui issquatch
Icon=weather-clear-night
Terminal=false
Type=Application
Categories=Education;Science;Astronomy;
EOF

say "Fetching the ISS orbit"
"$BIN" update

# ---------- Hyprland ----------
if command -v hyprctl >/dev/null && [[ -f $HYPR/hyprland.lua ]]; then
  bind=""
  if [[ $KEY != none ]]; then
    normalized="$(tr -d ' ' <<<"$KEY" | tr '[:lower:]' '[:upper:]')"
    taken="$(omarchy menu keybindings --print 2>/dev/null | awk -F'→' '{print $1}' | tr -d ' ' | tr '[:lower:]' '[:upper:]' |
      awk -v k="$normalized" 'BEGIN{split(k,a,"+"); n=asort(a)} {m=split($0,b,/\+/); if (m!=n) next; asort(b); same=1; for(i=1;i<=n;i++) if(a[i]!=b[i]) same=0; if(same) print}')"
    if [[ -n $taken ]] && ! grep -q "issquatch" "$MODULE" 2>/dev/null; then
      say "Keybinding $KEY is already used; skipping it (set ISSQUATCH_KEY to choose another)."
    else
      bind="o.bind(\"$KEY\", \"ISS tracker (issquatch)\", { tui = \"issquatch\", focus = true })"
    fi
  fi

  cat >"$MODULE" <<EOF
-- issquatch: the ISS, tracked from the woods. Managed by issquatch's install.sh;
-- remove with \`install.sh --uninstall\`. Loaded from hyprland.lua via require_optional.

-- A floating map window, sized for a 2:1 world map plus the pass panel. Opaque, so the
-- windows underneath don't show through the map.
o.window("org.omarchy.issquatch", {
  tag = "-default-opacity",
  float = true,
  center = true,
  size = { 1180, 600 },
  opacity = "1 1",
})

${bind}
EOF

  if ! grep -qF "$REQUIRE" "$HYPR/hyprland.lua"; then
    backup "$HYPR/hyprland.lua"
    printf '\n%s\n' "$REQUIRE" >>"$HYPR/hyprland.lua"
  fi
  if [[ $(hyprctl reload 2>&1) == ok ]] && [[ -z $(hyprctl configerrors 2>/dev/null | tr -d '[:space:]') ]]; then
    say "Hyprland rule loaded ($MODULE)"
  else
    rm -f "$MODULE"
    hyprctl reload >/dev/null 2>&1 || true
    die "Hyprland reported config errors; rolled back $MODULE. Run: hyprctl configerrors"
  fi
else
  say "No Omarchy Lua Hyprland config found; skipping the window rule and keybinding."
fi

# ---------- notifications ----------
if [[ $NOTIFY == 1 ]] && command -v systemctl >/dev/null; then
  mkdir -p "$UNITS"
  install -m 644 "$dir/systemd/issquatch-notify.service" "$dir/systemd/issquatch-notify.timer" "$UNITS/"
  systemctl --user daemon-reload
  systemctl --user enable --now issquatch-notify.timer >/dev/null
  say "Pass notifications on (issquatch-notify.timer). They start once you've set a location."
fi

[[ -f $HOME/.config/issquatch/config.toml ]] || say "Set your location: issquatch setup"
say "Installed. Open the map with: issquatch${bind:+  (or $KEY)}"
