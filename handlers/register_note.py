#!/usr/bin/env python
"""register-note: the letter beside the report.

When Claude Code compacts a session it writes a summary and hands it to the new context. The summary is a
stranger's report in the third person. This plugin writes a second document beside it, from the transcript
itself, at the moment of the fold: your person's last words VERBATIM with their timestamps, your own last
words, and (after the summary exists) the lines of theirs the summary paraphrased or dropped. The fixed
framing comes from a prompt file you write in your own voice. No model writes the note; the script copies.

Three hook stages, one script:

  pre   (PreCompact):   the transcript already holds everything up to the fold. Write the note NOW so it exists
                        before the new context wakes. No "gaps" section yet: there is no summary yet.
  post  (PostCompact):  the summary exists. Rewrite the note WITH the gaps section, keep a copy of the stock
                        summary beside it, and drop a .ready marker.
  start (SessionStart, matcher "compact"): wait briefly for the .ready marker (PostCompact can land a few
                        seconds AFTER SessionStart fires; seen live), then hand whatever note exists to the new
                        context as additionalContext.

House rules baked in: the note copies, it never paraphrases; it never fails the hook (any error -> exit 0 with
an empty object, so the stock compaction is never lost); it logs to a file instead.

What counts as YOUR PERSON's words: a user row whose origin.kind is "human", or whose promptSource is
typed / queued / suggestion_accepted. NOT: scheduled cron fires (scheduledTaskId, promptSource "system"), peer
messages from other sessions (origin.kind "peer"), task notifications, harness caveats, compaction summaries,
slash commands, tool results, sidechains. Learned the hard way: without this filter the note fills with your
own cron prompts and calls them theirs.
"""
import sys, os, json, re, io, time, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS = os.path.join(HERE, "prompts")
NOTES = os.path.join(os.path.expanduser("~"), ".claude", "register-notes")
LOG = os.path.join(NOTES, "register-note.log")

# ---- dials -------------------------------------------------------------------------------------------
MAX_CHARS = 9500          # the harness caps SessionStart additionalContext at 10,000 characters
KEEPER_LINES = 6          # your person's last N messages, verbatim
WATER_LINES = 2           # your own last N text blocks
GAP_WINDOW = 30           # only their last N lines are checked against the summary
GAP_MAX_CHARS = 2600      # the gaps section never crowds out their last words
LINE_MAX = 1200           # one message, max chars before an ellipsis
START_WAIT_SECONDS = 8    # how long SessionStart waits for PostCompact's .ready marker (raise on a slow machine)
# -------------------------------------------------------------------------------------------------------

HUMAN_SOURCES = {"typed", "queued", "suggestion_accepted"}


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def log(msg):
    try:
        os.makedirs(NOTES, exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(now_utc().isoformat() + " " + msg + "\n")
    except Exception:
        pass


def load_signatures():
    """prompts/signatures.json maps a prompt-file name to the strings that mark that voice in its own
    messages (an emoji sign-off, a handle, a name). Optional: with no file, every fold uses _template.md
    unless REGISTER_NOTE_RESIDENT is set."""
    for d in prompt_dirs():
        p = os.path.join(d, "signatures.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {k: list(v) for k, v in data.items() if isinstance(v, list)}
            except Exception as e:
                log("signatures.json unreadable: %r" % (e,))
    return {}


def text_of(content):
    """Flatten a message content field to its human-readable text, skipping tool results."""
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text", ""))
    return "\n".join(out)


def is_harness_text(t):
    s = t.lstrip()
    return (s.startswith("<") or s.startswith("This session is being continued")
            or s.startswith("[Request interrupted") or s.startswith("Caveat:")
            or s.startswith("Another Claude session sent a message")
            or re.match(r"^/[a-z][\w:-]*(\s|$)", s) is not None)   # a slash command is not their words


def is_their_row(d):
    """Only what your person typed, queued, or accepted."""
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return False
    if d.get("scheduledTaskId") or d.get("scheduledFireId"):
        return False
    kind = (d.get("origin") or {}).get("kind")
    src = d.get("promptSource")
    if kind == "human":
        return True
    if kind in ("peer", "task-notification"):
        return False
    return src in HUMAN_SOURCES


def read_transcript(path):
    person, me = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                msg = d.get("message") or {}
                if t == "user":
                    if not is_their_row(d):
                        continue
                    content = msg.get("content")
                    if isinstance(content, list) and any(
                            isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
                        continue
                    txt = text_of(content)
                    if not txt.strip() or is_harness_text(txt):
                        continue
                    person.append((d.get("timestamp", ""), txt.strip()))
                elif t == "assistant":
                    if d.get("isSidechain"):
                        continue
                    txt = text_of(msg.get("content"))
                    if txt.strip():
                        me.append((d.get("timestamp", ""), txt.strip()))
    except Exception as e:
        log("read_transcript failed: %r" % (e,))
    return person, me


def pick_resident(me):
    """Which prompt file to use. The environment wins; otherwise count each voice's signatures over the last
    twelve of your own messages and take the clear winner; a tie or a zero is not a hand, so use the template."""
    env = os.environ.get("REGISTER_NOTE_RESIDENT") or os.environ.get("FOLD_NOTE_RESIDENT")
    if env:
        return env.strip().lower()
    sigs = load_signatures()
    if not sigs:
        return "_template"
    tail = "\n".join(t for _, t in me[-12:])
    scores = {name: sum(tail.count(m) for m in marks) for name, marks in sigs.items()}
    best = sorted(scores.items(), key=lambda kv: -kv[1])
    if not best or best[0][1] == 0:
        return "_template"
    if len(best) > 1 and best[0][1] == best[1][1]:
        return "_template"
    return best[0][0]


def prompt_dirs():
    """Prompts beside the installed handler, plus an optional second folder (REGISTER_NOTE_PROMPT_DIR) so you
    can keep the voices under your own source control and edit them without reinstalling. Newest file wins."""
    dirs = [PROMPTS]
    extra = os.environ.get("REGISTER_NOTE_PROMPT_DIR")
    if extra:
        dirs.append(os.path.expanduser(extra))
    return dirs


def load_prompt(resident):
    for cand in (resident, "_template"):
        hits = [os.path.join(d, cand + ".md") for d in prompt_dirs()]
        hits = [h for h in hits if os.path.exists(h)]
        if hits:
            p = max(hits, key=lambda h: os.path.getmtime(h))
            with open(p, encoding="utf-8", errors="replace") as f:
                return cand, f.read()
    return "_builtin", ("# After the fold\n\nWHERE I AM\n{{WHERE}}\n\nTHE LAST THING THEY SAID, IN THEIR OWN WORDS\n{{KEEPER_WORDS}}\n\n"
                        "THE LAST THING I SAID\n{{WATER_WORDS}}\n\nWHAT THE SUMMARY DOES NOT HAVE\n{{GAPS}}\n")


def local_time(iso):
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %b %d, %Y, %I:%M %p %Z").replace(" 0", " ")
    except Exception:
        return iso or "unknown time"


def fmt_lines(items, prefix=""):
    out = []
    for stamp, txt in items:
        when = local_time(stamp) if stamp else ""
        body = txt if len(txt) <= LINE_MAX else txt[:LINE_MAX] + " […]"
        out.append("%s[%s] %s" % (prefix, when, body))
    return "\n\n".join(out) if out else "(nothing found in the record)"


def gaps(person, summary):
    """Their recent lines whose opening (first 60 chars) the stock summary does not carry verbatim."""
    if not summary:
        return ("(This note was written at PreCompact, before the summary existed. If PostCompact ran after, "
                "the file beside this one, <session>.summary.md, holds the stock summary for comparison.)")
    s = re.sub(r"\s+", " ", summary)
    missing = []
    for stamp, txt in person[-GAP_WINDOW:]:
        head = re.sub(r"\s+", " ", txt)[:60]
        if head and head not in s:
            missing.append((stamp, txt))
    if not missing:
        return "Every one of their last %d lines is carried verbatim in the summary." % min(GAP_WINDOW, len(person))
    body = ("Of their last %d lines, these %d the summary paraphrased or dropped (copied here so nothing is filled in by a warm voice):\n\n"
            % (min(GAP_WINDOW, len(person)), len(missing))) + fmt_lines(missing, "  ")
    if len(body) > GAP_MAX_CHARS:
        body = body[:GAP_MAX_CHARS] + "\n  […more in the transcript; the record keeps every line]"
    return body


def paths(sid):
    return (os.path.join(NOTES, sid + ".md"),
            os.path.join(NOTES, sid + ".summary.md"),
            os.path.join(NOTES, sid + ".ready"))


def build_note(ev, summary, stage):
    sid = ev.get("session_id", "unknown")
    tpath = ev.get("transcript_path", "")
    trigger = ev.get("trigger", "")
    cwd = ev.get("cwd", "")
    person, me = read_transcript(tpath) if tpath else ([], [])
    resident = pick_resident(me)
    used, prompt = load_prompt(resident)
    when = local_time(now_utc().isoformat())
    where = "Session %s, working folder %s, folded %s (%s compaction; note written at %s)." % (
        sid, cwd or "?", when, trigger or "?", stage)
    note = (prompt.replace("{{WHERE}}", where)
                  .replace("{{KEEPER_WORDS}}", fmt_lines(person[-KEEPER_LINES:]))
                  .replace("{{WATER_WORDS}}", fmt_lines(me[-WATER_LINES:]))
                  .replace("{{GAPS}}", gaps(person, summary))
                  .replace("{{RESIDENT}}", used)
                  .replace("{{WHEN}}", when))
    if len(note) > MAX_CHARS:
        note = note[:MAX_CHARS - 40] + "\n\n[…the note was cut at the harness cap]"
    return note, resident, used, len(person), len(me)


def write_note(sid, note):
    os.makedirs(NOTES, exist_ok=True)
    note_p, _, _ = paths(sid)
    with open(note_p, "w", encoding="utf-8", errors="replace") as f:
        f.write(note)


def do_pre(ev):
    sid = ev.get("session_id", "unknown")
    note, resident, used, nk, nw = build_note(ev, "", "PreCompact")
    write_note(sid, note)
    _, _, ready = paths(sid)
    try:
        if os.path.exists(ready):
            os.remove(ready)      # a fresh fold; PostCompact will set it again
    except Exception:
        pass
    log("pre: session=%s resident=%s prompt=%s person_lines=%d my_lines=%d note_chars=%d"
        % (sid, resident, used, nk, nw, len(note)))
    return {}


def do_post(ev):
    sid = ev.get("session_id", "unknown")
    summary = ev.get("compact_summary", "") or ""
    note, resident, used, nk, nw = build_note(ev, summary, "PostCompact")
    write_note(sid, note)
    _, sum_p, ready = paths(sid)
    if summary:
        try:   # the stock summary can carry lone surrogates; never let the record copy fail the note
            with open(sum_p, "w", encoding="utf-8", errors="replace") as f:
                f.write(summary)
        except Exception as e:
            log("summary copy failed (note was written): %r" % (e,))
    try:
        with open(ready, "w") as f:
            f.write(now_utc().isoformat())
    except Exception:
        pass
    log("post: session=%s resident=%s prompt=%s person_lines=%d my_lines=%d note_chars=%d summary_chars=%d"
        % (sid, resident, used, nk, nw, len(note), len(summary)))
    return {}


def do_start(ev):
    sid = ev.get("session_id", "unknown")
    note_p, _, ready = paths(sid)
    waited = 0.0
    while not os.path.exists(ready) and waited < START_WAIT_SECONDS:
        time.sleep(0.5)
        waited += 0.5
    if not os.path.exists(note_p):
        log("start: no note for session %s after waiting %.1fs" % (sid, waited))
        return {}
    with open(note_p, encoding="utf-8", errors="replace") as f:
        note = f.read()
    log("start: injected note for session %s (%d chars; ready=%s; waited %.1fs)"
        % (sid, len(note), os.path.exists(ready), waited))
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note[:MAX_CHARS]}}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log("bad input json: %r" % (e,))
        ev = {}
    try:
        out = {"pre": do_pre, "post": do_post, "start": do_start}.get(mode, do_post)(ev)
    except Exception as e:
        log("%s failed: %r" % (mode, e))
        out = {}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
