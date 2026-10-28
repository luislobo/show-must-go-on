# Linux VM test

An end-to-end test of restore on a real Linux desktop stack (Xvfb, i3, Terminator and tmux), in a [Lima](https://lima-vm.io) VM, so it runs on a Mac.

```bash
brew install lima
limactl start --name=smgo --param REPO="$PWD" tests/linux-vm/smgo.yaml   # first start: a few minutes
limactl shell --workdir / smgo bash /repo/tests/linux-vm/e2e.sh
limactl stop smgo        # or: limactl delete smgo
```

`--workdir /` keeps `limactl shell` from trying to enter your current folder, which the VM cannot see.

The VM is Ubuntu 24.04 on Apple's Virtualization framework. Only this repository is shared with it, read-only, at `/repo`; the rest of your home folder, including `~/.claude`, stays out of it.

`e2e.sh` runs in a fresh home folder with the demo projects from `demo/make_home.py`. `bin/claude` and `bin/codex` stand in for the agent CLIs: `claude` writes the per-process record the real CLI writes and becomes a process named `claude`; `codex` holds its rollout file open, which is how restore finds a codex session. No logins or tokens are needed. The script then:

1. opens Terminator windows with sessions on workspaces `" 1 "`, `"2: web"` and `" 3 "` (a claude and a codex session share `" 3 "`)
2. runs `restore --save`
3. kills every window and session, as a crash would, leaving the pid records behind
4. runs `restore --load --execute` and checks every session runs again, on its saved workspace
5. runs `restore --load` again and checks nothing is relaunched
6. kills everything again and runs `restore --load --target=byobu --execute`, checking for one tmux pane per session

A run takes about 30 seconds once the VM is up. Run against the code from before the i3 quoting fix, it fails at step 4: the sessions saved on `" 1 "` and `" 3 "` come back on `"1 "` and `"3 "`.

Each check reads i3's window tree and `/proc` directly, not restore's own output. The script exits non-zero if any check fails.

To watch the X session while it runs, start it with `VNC=1` and open `vnc://127.0.0.1:5907` in Screen Sharing (password `smgo`). The VNC server listens only on the VM's localhost, which Lima forwards to the Mac's:

```bash
limactl shell --workdir / smgo env VNC=1 bash /repo/tests/linux-vm/e2e.sh
```
