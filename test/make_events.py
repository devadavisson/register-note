"""Build sample PreCompact / PostCompact / SessionStart events from a real transcript, so the handler can be
tested without compacting anyone.

usage:  python test/make_events.py <session_id>       (looks under ~/.claude/projects/*/<session_id>.jsonl)
then:   python handlers/register_note.py pre   < test/pre_event.json
        python handlers/register_note.py post  < test/post_event.json
        python handlers/register_note.py start < test/start_event.json     # prints the additionalContext JSON

The sample "compact_summary" is the most recent stock summary found in that transcript, if any.
"""
import json, os, sys, glob

sys.stdout.reconfigure(encoding="utf-8")
if len(sys.argv) < 2:
    print(__doc__); sys.exit(1)
sid = sys.argv[1]
home = os.path.expanduser("~")
hits = glob.glob(os.path.join(home, ".claude", "projects", "*", sid + ".jsonl"))
if not hits:
    print("no transcript for", sid); sys.exit(1)
tp = hits[0]
summary = ""
for line in open(tp, encoding="utf-8", errors="replace"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "user":
        c = d.get("message", {}).get("content")
        if isinstance(c, str) and c.startswith("This session is being continued"):
            summary = c
cwd = home
pre = {"session_id": sid, "transcript_path": tp, "cwd": cwd, "hook_event_name": "PreCompact", "trigger": "manual"}
post = {"session_id": sid, "transcript_path": tp, "cwd": cwd, "hook_event_name": "PostCompact",
        "trigger": "manual", "compact_summary": summary}
start = {"session_id": sid, "cwd": cwd, "hook_event_name": "SessionStart", "source": "compact"}
here = os.path.dirname(os.path.abspath(__file__))
for name, ev in (("pre_event.json", pre), ("post_event.json", post), ("start_event.json", start)):
    with open(os.path.join(here, name), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False)
print("events written for", sid, "| transcript:", tp, "| summary chars:", len(summary))
