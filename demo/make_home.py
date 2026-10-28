"""Build a demo home folder with made-up projects and agent sessions.

Everything is fictional, so a recording shows no real session content.

    python3 demo/make_home.py <demo-home>

Creates, under <demo-home>:
  code/<project>/              git repos with a branch and a GitHub remote
  .claude/projects/<enc>/      Claude Code transcripts (JSONL)
  .codex/sessions/YYYY/MM/DD/  a Codex rollout
  .ai/sessions.jsonl           cached summaries for some sessions, so the
                               rest are summarised live by demo/bin/claude
"""

import json
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime.now(timezone.utc)

# project, branch, tool, hours since last activity, interrupted, cached, topic, abstract, prompts
SESSIONS = [
    ("storefront", "feature/checkout-redesign", "claude", 3.0, True, False,
     "Checkout flow redesign",
     "Splitting the one-page checkout into address, shipping and payment steps. The address "
     "step and its validation are done; the agent was wiring the shipping-rate lookup when "
     "the session stopped.",
     ["Let's redesign the checkout as three steps: address, shipping, payment",
      "Keep the cart summary visible on every step",
      "Validate postcodes on the address step before moving on",
      "Add tests for the step navigation",
      "Now wire the shipping step to the rates endpoint"]),
    ("payments-api", "fix/webhook-retries", "claude", 7.0, False, True,
     "Webhook retry backoff fix",
     "Failed webhook deliveries were retried immediately and flooded slow receivers. Retries "
     "now back off exponentially with jitter, capped at one hour, and the change is merged.",
     ["Webhook retries are hammering slow endpoints, can you look?",
      "Use exponential backoff with jitter",
      "Cap the delay at one hour",
      "Add a test that a 503 schedules the next attempt later",
      "Looks good, open the PR"]),
    ("mobile-app", "main", "codex", 12.0, False, False,
     "iOS push notification setup",
     "Registering for remote notifications on iOS and sending the device token to the API. "
     "Token upload works on a device; silent pushes still need the background mode enabled.",
     ["Set up push notifications for the iOS app",
      "Send the device token to /devices after login",
      "Handle the token changing",
      "Why don't silent pushes arrive?",
      "Add the background mode then"]),
    ("infra", "feature/terraform-modules", "claude", 20.0, True, True,
     "Terraform module split",
     "Moving the network, database and cluster definitions out of the single root module "
     "into reusable modules, with state moves so nothing is recreated. The database module "
     "was half done.",
     ["The root Terraform module is 2,000 lines, let's split it",
      "Start with the network module",
      "Use moved blocks so the plan shows no replacements",
      "Now the database module",
      "Keep the parameter group in the same module"]),
    ("docs-site", "main", "claude", 31.0, False, True,
     "Docs search index",
     "Added a build step that writes a search index of every page heading and its first "
     "paragraph, and a search box that queries it in the browser.",
     ["Add search to the docs site",
      "Index headings and the first paragraph only",
      "Build the index at deploy time",
      "Put the search box in the header",
      "Ship it"]),
    ("data-pipeline", "feature/parquet-export", "claude", 41.0, False, False,
     "Parquet export for nightly jobs",
     "The nightly jobs now write Parquet next to CSV, partitioned by day, with a schema test "
     "so a column change fails the build instead of the consumers.",
     ["Export the nightly tables as Parquet as well as CSV",
      "Partition by event date",
      "Add a schema test",
      "Keep CSV for one more release",
      "Update the runbook"]),
    ("design-system", "main", "claude", 53.0, False, True,
     "Product image lazy loading",
     "Product grid images now load lazily with fixed aspect-ratio placeholders, which removed "
     "the layout shift on slow connections.",
     ["The product grid jumps while images load",
      "Lazy-load the images below the fold",
      "Reserve the space with aspect-ratio boxes",
      "Check it on a throttled connection",
      "Good, commit it"]),
]

# branch → (number, state, title) for the gh stand-in
PRS = {
    "feature/checkout-redesign": (142, "OPEN", "Redesign checkout as three steps"),
    "fix/webhook-retries": (87, "MERGED", "Back off webhook retries with jitter"),
    "feature/terraform-modules": (23, "OPEN", "Split the root module into network, database and cluster"),
}

REPLIES = [
    "I'll start by reading the current implementation and its tests.",
    "Done. The change is in place and the tests pass.",
    "Here is what I found, and the fix I'd suggest.",
    "Updated, and I added a test for it.",
    "Working on it now.",
]


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def main(home: Path) -> None:
    if home.exists():
        shutil.rmtree(home)
    cache = []
    made_repos: set[str] = set()
    for i, (proj, branch, tool, hours, interrupted, cached, topic, abstract, prompts) in enumerate(SESSIONS):
        cwd = home / "code" / proj
        if proj not in made_repos:
            cwd.mkdir(parents=True)
            git(cwd, "init", "-q")
            git(cwd, "remote", "add", "origin", f"https://github.com/acme/{proj}.git")
            (cwd / "README.md").write_text(f"# {proj}\n\nA demo project.\n")
            made_repos.add(proj)
        git(cwd, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

        last = NOW - timedelta(hours=hours)
        first = last - timedelta(minutes=55)
        step = (last - first) / (2 * len(prompts))
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"show-must-go-on-demo/{proj}/{i}"))
        events = []
        for n, prompt in enumerate(prompts):
            events.append((first + step * (2 * n), "user", prompt))
            events.append((first + step * (2 * n + 1), "assistant", REPLIES[n % len(REPLIES)]))
        if interrupted:
            events.append((last, "assistant", "Next I'm updating the handler and its tests."))
        else:
            events.append((last, "user", "Thanks, that's all for now."))

        if tool == "claude":
            enc = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
            path = home / ".claude" / "projects" / enc / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as f:
                for ts, role, text in events:
                    msg = ({"role": "user", "content": text} if role == "user"
                           else {"role": "assistant", "content": [{"type": "text", "text": text}]})
                    f.write(json.dumps({"type": role, "timestamp": iso(ts), "cwd": str(cwd),
                                        "sessionId": sid, "message": msg}) + "\n")
        else:
            path = (home / ".codex" / "sessions" / f"{first:%Y}" / f"{first:%m}" / f"{first:%d}"
                    / f"rollout-{first:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as f:
                f.write(json.dumps({"type": "session_meta", "timestamp": iso(first),
                                    "payload": {"id": sid, "cwd": str(cwd)}}) + "\n")
                for ts, role, text in events:
                    kind = "user_message" if role == "user" else "agent_message"
                    f.write(json.dumps({"type": "event_msg", "timestamp": iso(ts),
                                        "payload": {"type": kind, "message": text}}) + "\n")

        if cached:
            # restore compares last_ts as datetime.isoformat() text
            rec = {"tool": tool, "session_id": sid, "cwd": str(cwd), "last_ts": last.isoformat(),
                   "topic": topic, "slug": None, "abstract": abstract,
                   "generated_at": iso(NOW)}
            if branch in PRS:
                number, state, title = PRS[branch]
                rec.update(pr_number=number, pr_state=state.lower(), pr_title=title,
                           pr_url=f"https://github.com/acme/{proj}/pull/{number}",
                           pr_fetched_at=iso(NOW))
            cache.append(rec)

    (home / ".ai").mkdir(parents=True, exist_ok=True)
    with (home / ".ai" / "sessions.jsonl").open("w") as f:
        for rec in cache:
            f.write(json.dumps(rec) + "\n")
    # summaries and PRs the stand-ins hand back, keyed by project folder and branch
    (home / ".demo-answers.json").write_text(json.dumps({
        "summaries": {f"{s[0]}@{s[1]}": {"topic": s[6], "abstract": s[7]} for s in SESSIONS},
        "prs": {b: {"number": n, "state": st, "title": t} for b, (n, st, t) in PRS.items()},
    }, indent=1))
    print(f"demo home ready: {home}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
