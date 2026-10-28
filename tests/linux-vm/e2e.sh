#!/usr/bin/env bash
# End-to-end test of restore on a real Linux desktop stack: Xvfb, i3 and
# Terminator, plus tmux. Run inside the VM from tests/linux-vm/smgo.yaml:
#
#   limactl shell --workdir / smgo bash /repo/tests/linux-vm/e2e.sh
#
# It uses a fresh home folder with the demo projects (demo/make_home.py) and
# the stand-in agent CLIs in tests/linux-vm/bin, and checks each step
# against i3's own window tree:
#   1. open Terminator windows with sessions on workspaces " 1 ", "2: web"
#      and " 3 " (a claude and a codex session share " 3 ")
#   2. restore --save
#   3. kill every window and session, as a crash would (records stay behind)
#   4. restore --load --execute: every session runs again, on its workspace
#   5. restore --load again: nothing to do, all already running
#   6. kill again; restore --load --target=byobu --execute: one tmux pane each
# Set VNC=1 to watch the X session on vnc://127.0.0.1:5907 from the Mac
# (password: smgo; the server listens on the VM's localhost only).
set -euo pipefail

REPO="${REPO:-/repo}"
RESTORE="$REPO/restore_terminals.py"
work="$(mktemp -d)"
export HOME="$work/home"
export PATH="$REPO/tests/linux-vm/bin:$PATH"   # the stand-in claude and codex
export DISPLAY=:7
failures=0

pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
step() { printf '\n[%3ss] %s\n' "$SECONDS" "$*"; }

# restore's output goes to a file, never a pipe: the Terminator windows it
# starts inherit its stdout, so a reader waiting for end of file would wait
# for them to close. The timeout turns a hang into a failure.
run_restore() {   # $@ = restore arguments; output in $work/restore.out
  if ! timeout 120 python3 "$RESTORE" "$@" > "$work/restore.out" 2>&1; then
    fail "restore $* did not finish cleanly"
  fi
}

cleanup() {
  tmux kill-server 2>/dev/null || true
  pkill -x claude 2>/dev/null || true
  pkill -x codex 2>/dev/null || true
  pkill -f "terminator" 2>/dev/null || true
  pkill -x i3 2>/dev/null || true
  pkill -f "x11vnc -display $DISPLAY" 2>/dev/null || true
  pkill -f "Xvfb $DISPLAY" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# --- desktop ----------------------------------------------------------------
Xvfb "$DISPLAY" -screen 0 1600x1000x24 >/dev/null 2>&1 &
for _ in $(seq 50); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
mkdir -p "$HOME/.config/i3"
printf 'font pango:DejaVu Sans Mono 9\n' > "$HOME/.config/i3/config"
i3 -c "$HOME/.config/i3/config" >/dev/null 2>&1 &
for _ in $(seq 50); do i3-msg -t get_version >/dev/null 2>&1 && break; sleep 0.1; done
if [ "${VNC:-0}" = 1 ]; then
  x11vnc -display "$DISPLAY" -localhost -forever -passwd smgo -rfbport 5907 -quiet >/dev/null 2>&1 &
fi

python3 "$REPO/demo/make_home.py" "$HOME" >/dev/null

# session id, folder, tool, rollout path, for the sessions this test opens
read_session() {   # $1 = project folder name, $2 = claude|codex
  python3 - "$HOME" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
home, proj, tool = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
root = home / (".claude/projects" if tool == "claude" else ".codex/sessions")
for f in sorted(root.rglob("*.jsonl")):
    for line in f.open():
        d = json.loads(line)
        cwd = d.get("cwd") or (d.get("payload") or {}).get("cwd")
        sid = d.get("sessionId") or (d.get("payload") or {}).get("id")
        if cwd and Path(cwd).name == proj and sid:
            print(sid, cwd, f)
            raise SystemExit
raise SystemExit(f"no {tool} session for {proj}")
PY
}

# window → i3 workspace, for every agent process, independent of restore:
# walk from each claude/codex pid up to the Terminator that hosts it.
placements() {
  python3 - <<'PY'
import json, os, subprocess
tree = json.loads(subprocess.run(["i3-msg", "-t", "get_tree"], capture_output=True, text=True).stdout)
ws_of = {}
def walk(n, ws=None):
    if n.get("type") == "workspace":
        ws = n["name"]
    if n.get("window"):
        ws_of[n["window"]] = ws
    for c in n.get("nodes", []) + n.get("floating_nodes", []):
        walk(c, ws)
walk(tree)
pid_ws = {}
for line in subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True).stdout.splitlines():
    wid, _, pid = line.split()[:3]
    pid_ws[int(pid)] = ws_of.get(int(wid, 16))
def parent(pid):
    return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
home = os.environ["HOME"]
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        argv0 = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")[0].decode()
    except OSError:
        continue
    if argv0 not in ("claude", "codex"):
        continue
    pid = int(name)
    if argv0 == "claude":
        sid = json.load(open(f"{home}/.claude/sessions/{pid}.json"))["sessionId"]
    else:
        sid = os.path.basename(os.readlink(f"/proc/{pid}/fd/3"))
    ws, p = None, pid
    for _ in range(6):
        if p in pid_ws:
            ws = pid_ws[p]
            break
        p = parent(p)
    print(f"{sid}\t{ws}")
PY
}

open_window() {   # $1 = folder, $2 = command, $3 = workspace
  before="$(wmctrl -l | awk '{print $1}' | sort)"
  terminator -u --working-directory "$1" -x bash -lc "$2; exec bash" >/dev/null 2>&1 &
  for _ in $(seq 100); do
    new="$(comm -13 <(echo "$before") <(wmctrl -l | awk '{print $1}' | sort) | head -1)"
    [ -n "$new" ] && break
    sleep 0.1
  done
  i3-msg -q "[id=$new] move to workspace \"$3\""
}

crash() {
  pkill -f terminator 2>/dev/null || true
  tmux kill-server 2>/dev/null || true
  pkill -x claude 2>/dev/null || true
  pkill -x codex 2>/dev/null || true
  for _ in $(seq 50); do pgrep -x claude >/dev/null || pgrep -x codex >/dev/null || break; sleep 0.1; done
}

wait_for_sessions() {   # $1 = how many agent processes to expect
  for _ in $(seq 100); do
    [ "$(placements | wc -l)" -ge "$1" ] && return 0
    sleep 0.2
  done
  return 1
}

# --- 1. sessions on named workspaces -----------------------------------------
step "1. open sessions on workspaces \" 1 \", \"2: web\" and \" 3 \""
read -r sid1 cwd1 _ < <(read_session storefront claude)
read -r sid2 cwd2 _ < <(read_session payments-api claude)
read -r sid3 cwd3 _ < <(read_session infra claude)
read -r _ cwd4 rollout4 < <(read_session mobile-app codex)
sid4="$(basename "$rollout4")"
open_window "$cwd1" "claude --resume $sid1" " 1 "
open_window "$cwd2" "claude --resume $sid2" "2: web"
open_window "$cwd3" "claude --resume $sid3" " 3 "
open_window "$cwd4" "codex resume '$rollout4'" " 3 "
wait_for_sessions 4 || true
expected="$(printf '%s\t%s\n' "$sid1" " 1 " "$sid2" "2: web" "$sid3" " 3 " "$sid4" " 3 " | sort)"
actual="$(placements | sort)"
if [ "$actual" = "$expected" ]; then pass "4 sessions open on their workspaces"; else fail "setup: expected
$expected
got
$actual"; fi

# --- 2. save -----------------------------------------------------------------
step "2. restore --save"
run_restore --save
sed 's/^/      /' "$work/restore.out"
snap="$HOME/.ai/snapshots/latest.json"
saved="$(python3 - "$snap" <<'PY'
import json, os, sys
for e in json.load(open(sys.argv[1]))["sessions"]:
    sid = e["session_id"] if e["tool"] == "claude" else os.path.basename(e["jsonl_path"])
    print(f"{sid}\t{e['workspace']}")
PY
)"
saved="$(echo "$saved" | sort)"
if [ "$saved" = "$expected" ]; then pass "snapshot has all 4 sessions with their exact workspace names"; else fail "snapshot: expected
$expected
got
$saved"; fi

# --- 3. crash ----------------------------------------------------------------
step "3. kill every window and session (records stay behind)"
crash
if [ "$(placements | wc -l)" -eq 0 ]; then pass "nothing running"; else fail "sessions still running"; fi
left="$(ls "$HOME/.claude/sessions" | wc -l)"
[ "$left" -ge 3 ] && pass "$left pid records left behind, as after a crash" || fail "expected left-behind records"

# --- 4. load -----------------------------------------------------------------
step "4. restore --load --execute"
run_restore --load --execute --no-ai --no-gh
grep -E 'loaded|spawned|moved|placed|✗' "$work/restore.out" | sed 's/^/      /' || true
wait_for_sessions 4 || true
sleep 1
actual="$(placements | sort)"
if [ "$actual" = "$expected" ]; then pass "all 4 sessions running again, each on its saved workspace"; else fail "after load: expected
$expected
got
$actual"; fi

# --- 5. load again -----------------------------------------------------------
step "5. restore --load again"
run_restore --load --execute --no-ai --no-gh
grep -E 'skipped|already' "$work/restore.out" | sed 's/^/      /' || true
if grep -q "all sessions are already running" "$work/restore.out"; then pass "nothing relaunched"; else fail "expected 'all sessions are already running'"; fi
[ "$(placements | wc -l)" -eq 4 ] && pass "still exactly 4 sessions" || fail "session count changed"

# --- 6. tmux -----------------------------------------------------------------
step "6. crash again, then restore --load --target=byobu --execute"
crash
run_restore --load --target=byobu --execute --no-ai --no-gh
grep -E 'loaded|✓|✗|built' "$work/restore.out" | sed 's/^/      /' || true
for _ in $(seq 100); do [ "$( (pgrep -x claude; pgrep -x codex) | wc -l)" -ge 4 ] && break; sleep 0.2; done
panes="$(tmux list-panes -s -t restore -F '#{pane_id}' 2>/dev/null | wc -l)"
[ "$panes" -eq 4 ] && pass "tmux session 'restore' has 4 panes" || fail "expected 4 tmux panes, got $panes"
running="$(python3 - <<'PY'
import json, os
home = os.environ["HOME"]
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        argv0 = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")[0].decode()
    except OSError:
        continue
    if argv0 == "claude":
        print(json.load(open(f"{home}/.claude/sessions/{name}.json"))["sessionId"])
    elif argv0 == "codex":
        print(os.path.basename(os.readlink(f"/proc/{name}/fd/3")))
PY
)"
want="$(printf '%s\n' "$sid1" "$sid2" "$sid3" "$sid4" | sort)"
[ "$(echo "$running" | sort)" = "$want" ] && pass "the 4 sessions are running in tmux" || fail "tmux sessions: expected
$want
got
$running"

printf '\n'
echo "finished in ${SECONDS}s"
if [ "$failures" -eq 0 ]; then echo "PASS: restore end to end on i3, Terminator and tmux"; else echo "FAIL: $failures check(s) failed"; exit 1; fi
