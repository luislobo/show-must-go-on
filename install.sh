#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${1:-$HOME/.local/bin}"

mkdir -p "$bin_dir"

ln -sfn "$repo_dir/restore_terminals.py" "$bin_dir/restore"
ln -sfn "$repo_dir/find_session.py" "$bin_dir/find-session"

chmod +x "$repo_dir/restore_terminals.py" "$repo_dir/find_session.py"

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    printf 'Installed, but %s is not on PATH.\n' "$bin_dir"
    printf 'Add this to your shell profile:\n\n'
    printf '  export PATH="%s:$PATH"\n\n' "$bin_dir"
    ;;
esac

# install.sh used to link into ~/bin; links left there can shadow these
for old in "$HOME/bin/restore" "$HOME/bin/find-session"; do
  if [ -L "$old" ] && [ "$(dirname "$old")" != "$bin_dir" ]; then
    printf 'Note: %s is a link from an earlier install; remove it with: rm %s\n' "$old" "$old"
  fi
done

printf 'Installed:\n'
printf '  %s/restore -> %s/restore_terminals.py\n' "$bin_dir" "$repo_dir"
printf '  %s/find-session -> %s/find_session.py\n' "$bin_dir" "$repo_dir"
printf '\nVerify with:\n'
printf '  restore --help\n'
