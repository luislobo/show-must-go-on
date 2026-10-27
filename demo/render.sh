#!/usr/bin/env bash
# Build the demo home, record the demo and render it as GIF and MP4.
#
#   demo/render.sh <out-dir>
#
# Needs agg (brew install agg) and ffmpeg. Everything shown is fictional:
# demo/make_home.py writes the projects and sessions, and demo/bin stands
# in for `claude -p` and `gh`, so nothing is read from your own history.
set -euo pipefail

out="${1:?usage: demo/render.sh <out-dir>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

python3 "$here/make_home.py" "$work/home"
python3 "$here/record.py" "$work/home" "$work/demo.cast"

# Palettes from Ghostty's "GitHub Dark Default" and "GitHub Light Default"
# themes (background, foreground, colours 0-15). agg's built-in github-light
# uses a pale green and orange that fall far below 4.5:1 on white.
# (a function, not an associative array: macOS ships bash 3.2)
palette() {
  case "$1" in
    github-dark)  echo "0d1117,e6edf3,484f58,ff7b72,3fb950,d29922,58a6ff,bc8cff,39c5cf,b1bac4,6e7681,ffa198,56d364,e3b341,79c0ff,d2a8ff,56d4dd,ffffff" ;;
    github-light) echo "ffffff,1f2328,24292f,cf222e,116329,4d2d00,0969da,8250df,1b7c83,6e7781,57606a,a40e26,1a7f37,633c01,218bff,a475f9,3192aa,8c959f" ;;
  esac
}

for theme in github-dark github-light; do
  agg --theme "$(palette "$theme")" --font-size 17 --idle-time-limit 4 --fps-cap 30 \
      --last-frame-duration 5 "$work/demo.cast" "$work/$theme.gif"
  # H.264 needs even dimensions and yuv420p to play everywhere
  ffmpeg -v error -y -i "$work/$theme.gif" -movflags +faststart -pix_fmt yuv420p \
         -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -c:v libx264 -crf 18 -preset slow \
         "$work/$theme.mp4"
done

mv "$work/demo.cast" "$out/show-must-go-on-demo.cast"
mv "$work/github-dark.gif" "$out/show-must-go-on-demo.gif"
mv "$work/github-dark.mp4" "$out/show-must-go-on-demo.mp4"
mv "$work/github-light.mp4" "$out/show-must-go-on-demo-light.mp4"
ls -la "$out"
