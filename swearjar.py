#!/usr/bin/env python3
"""Swear Jar: where your Claude tokens go, for one product of yours.

Reads Claude Code's local transcripts (~/.claude/projects/**/*.jsonl), keeps the
sessions that worked on your product, sorts every turn into a bucket (features,
bug fixes, releases...), prices it at API rates, and shows it all on a live page.

    python3 swearjar.py setup        # once: pick your product, buckets, where to publish
    python3 swearjar.py              # live page at http://localhost:8642
    python3 swearjar.py --install    # keep it running from login (and publishing, if set up)
    python3 swearjar.py --uninstall  # undo --install

Publishing (optional): every few minutes this computer's totals, plus the page
itself, go to the `swear-jar` branch of a GitHub repo you choose, which GitHub
Pages serves as your dashboard. Several computers add up without overwriting
each other. Only totals per session, day and bucket go up, never prompts.

No installs needed (Python 3.9+ standard library only). Settings live in
~/.swear-jar/config.json; `setup` writes it and you can edit it by hand.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("SWEAR_JAR_HOME", Path.home() / ".swear-jar"))
CONFIG_FILE = HOME / "config.json"
TRANSCRIPTS = Path.home() / ".claude" / "projects"
DATA_BRANCH = "swear-jar"                     # the branch Pages serves the dashboard from
PUBLISH_EVERY = 5 * 60                        # seconds
PORT = 8642
ALWAYS_EXCLUDE = ["swear-jar", ".swear-jar", "coin-op", ".coin-op"]   # Swear Jar's own work never counts

# API list prices, $ per million tokens (Anthropic first-party, 2026-09).
# Cache writes: 1.25x input (5-minute cache) or 2x input (1-hour cache).
PRICES = {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "read": 0.20},
    "claude-opus-5": {"in": 5.0, "out": 25.0, "read": 0.50},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0, "read": 0.50},
    "claude-opus-4-7": {"in": 5.0, "out": 25.0, "read": 0.50},
    "claude-opus-4-6": {"in": 5.0, "out": 25.0, "read": 0.50},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0, "read": 0.25},
    "claude-fable-5": {"in": 10.0, "out": 50.0, "read": 1.00},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "read": 0.20},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "read": 0.20},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "read": 0.30},
    "claude-sonnet-4-5": {"in": 3.0, "out": 15.0, "read": 0.30},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "read": 0.10},
}
DEFAULT_MODEL = "claude-opus-5-5"

# ---------------------------------------------------------------- settings
# Buckets: each turn (one message + all the work Claude did for it) lands in one.
# Words in the message count most; files Claude touched break ties and decide
# short replies like "yes do it". Themes group buckets; themes marked "roadmap"
# are compared with the roadmap, and a roadmap item joins the first theme whose
# "items" pattern matches its title (a theme with no pattern takes the rest).

GENERAL = {
    "product": "",
    "match": [],
    "exclude": [],
    "roadmap": "",
    "roadmap_link": "",
    "roadmap_rule": "",
    "publish_repo": "",
    "default_bucket": "features",
    "buckets": [
        {"id": "features", "name": "Features & UI", "color": "#4dd2ff", "theme": "build",
         "words": r"add|feature|build|make|design|layout|button|page|screen|style|colou?r|theme|setting|ui\b|ux\b",
         "files": r"\.(?:css|scss|html|tsx|jsx|vue|svelte|swift)$|components?/"},
        {"id": "bugs", "name": "Bug squashing", "color": "#ff6b3d", "theme": "upkeep", "weight": 4,
         "words": r"bug|crash|broken|not working|doesn.?t (?:seem to )?work|isn.?t working|not showing|wrong|error|fail|fix",
         "files": r"$^"},
        {"id": "quality", "name": "Tests & quality", "color": "#ff4d8d", "theme": "build",
         "words": r"test|benchmark|bench|measure|accura|quality|eval|refactor|performance|faster|slow",
         "files": r"tests?/|_test\.|\.test\.|\.spec\.|bench"},
        {"id": "ship", "name": "Releases & Git", "color": "#3ddc84", "theme": "upkeep",
         "words": r"merge|release|deploy|\bci\b|workflow|github|git |push|branch|version|install|publish",
         "files": r"\.github/workflows|package\.json|Cargo\.(?:toml|lock)|pyproject|gh (?:pr|release|workflow|run)"},
        {"id": "docs", "name": "Docs & planning", "color": "#f2f2f2", "theme": "upkeep",
         "words": r"readme|document|roadmap|handoff|report|docs|plan\b|ideas?\b|changelog|spec",
         "files": r"README|CHANGELOG|CLAUDE\.md|docs/|\.md$"},
        {"id": "setup", "name": "Setup & tooling", "color": "#9b6bff", "theme": "upkeep",
         "words": r"setup|set up|config|environment|dependenc|upgrade|node|python|xcode|permission|path",
         "files": r"\.(?:json|ya?ml|toml|lock|plist)$|\.env|Dockerfile"},
        {"id": "learn", "name": "Learning & questions", "color": "#ffe14d", "theme": "upkeep",
         "words": r"layman|analogy|confused|what is|whats|what's|how does|why|what does|explain",
         "files": r"$^"},
    ],
    "themes": [
        {"id": "build", "name": "Building the product", "blurb": "New features and making them better.", "roadmap": True},
        {"id": "upkeep", "name": "Upkeep", "blurb": "Bugs, releases, docs, setup and learning. Not on the roadmap, but it keeps the lights on.", "roadmap": False},
    ],
}

CFG = dict(GENERAL)
PROJECT_COLORS = ["#4dd2ff", "#ff9f43", "#ff4d8d", "#ffe14d", "#3ddc84", "#9b6bff", "#f2f2f2", "#2ec4b6", "#c77dff", "#5b8cff", "#ff6b3d"]
OTHER = {"id": "other", "name": "Everything else", "color": "#7d7699",
         "blurb": "Claude Code work that didn't touch any of your projects: chats, planning, one-offs."}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def make_project(p, i):
    """One tracked project, with its own buckets, themes and roadmap (general ones by default)."""
    name = p.get("product") or p.get("name") or "my product"
    proj = {"id": p.get("id") or slug(name), "name": name, "match": p.get("match") or [slug(name)],
            "color": p.get("color") or PROJECT_COLORS[i % len(PROJECT_COLORS)],
            "roadmap": p.get("roadmap") or "", "roadmap_link": p.get("roadmap_link") or "", "roadmap_rule": p.get("roadmap_rule") or "",
            "buckets": p.get("buckets") or GENERAL["buckets"], "themes": p.get("themes") or GENERAL["themes"],
            "default_bucket": p.get("default_bucket") or GENERAL["default_bucket"]}
    proj["_match"] = name_pattern(proj["match"])
    proj["_buckets"] = [(b["id"], re.compile(b["words"], re.I), re.compile(b["files"], re.I), b.get("weight", 3)) for b in proj["buckets"]]
    proj["_order"] = [b["id"] for b in proj["buckets"]]
    if proj["default_bucket"] not in proj["_order"]:
        proj["default_bucket"] = proj["_order"][0]
    return proj


def load_config():
    """Settings from ~/.swear-jar/config.json, filled in from GENERAL. A "projects" list means
    'track all of these on one dashboard'; without it, the top-level fields are one project."""
    global CFG
    try:
        user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    CFG = {**GENERAL, **user}
    compile_config()
    return bool(user)


def save_config(cfg):
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def name_pattern(names):
    """A path segment or owner/repo that is exactly one of names (vox2, not vox2-site)."""
    alts = "|".join(re.escape(n) for n in names if n)
    return re.compile(rf"(?:^|[/\\]){'(?:' + alts + ')'}(?:\.git)?(?=[/\\\"'\s]|$)", re.I) if alts else None


def compile_config():
    global PROJECTS, ALL, EXCLUDE
    ALL = bool(CFG.get("projects"))
    if ALL:
        PROJECTS = [make_project(p, i) for i, p in enumerate(CFG["projects"])]
        EXCLUDE = name_pattern(CFG.get("exclude") or [])
    else:
        PROJECTS = [make_project(CFG, 0)]
        EXCLUDE = name_pattern([e for e in CFG["exclude"] + ALWAYS_EXCLUDE if e and e not in CFG["match"]])


compile_config()


def classify(prompt, touched, proj):
    """Return one of the project's bucket ids, or None if the turn gives no clue."""
    scores = defaultdict(float)
    for bid, words, _, weight in proj["_buckets"]:
        hits = len(set(m.group(0).lower() for m in words.finditer(prompt)))
        if hits:
            scores[bid] += min(hits, 3) * weight
    if touched:
        counts = defaultdict(int)
        for t in touched:
            for bid, _, files, _ in proj["_buckets"]:
                if files.search(t):
                    counts[bid] += 1
        total = sum(counts.values())
        for bid, n in counts.items():
            scores[bid] += 4 * n / total
    if not scores:
        return None
    return max(scores, key=lambda b: (scores[b], -proj["_order"].index(b)))


# ---------------------------------------------------------------- transcripts

def prompt_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return None


def tool_strings(content):
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input") or {}
            for key in ("file_path", "path", "command", "notebook_path", "url"):
                v = inp.get(key)
                if isinstance(v, str):
                    out.append(v[:400])
    return out


def cost_of(model, u):
    p = PRICES.get(model, PRICES[DEFAULT_MODEL])
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    w5, w1 = w5 or 0, w1 or 0
    inp, out, read = u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0)
    dollars = (inp * p["in"] + out * p["out"] + w5 * p["in"] * 1.25 + w1 * p["in"] * 2 + read * p["read"]) / 1e6
    if u.get("speed") == "fast":
        dollars *= 2
    return dollars, {"input": inp, "output": out, "cache_write": w5 + w1, "cache_read": read}


def parse_file(path):
    """One transcript -> {session_id: [events]} in file order."""
    sessions = defaultdict(list)
    seen = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sid, ts, kind = d.get("sessionId"), d.get("timestamp"), d.get("type")
            if not sid or not ts:
                continue
            msg = d.get("message") or {}
            if kind == "user" and not d.get("isMeta") and not d.get("isSidechain"):
                text = prompt_text(msg)
                if text is None or text.startswith("[Request interrupted"):
                    continue
                # Summaries after a context compaction and "background task finished"
                # notes aren't new requests; they carry on the turn before.
                cont = bool(d.get("isCompactSummary")) or text.startswith(("This session is being continued", "<task-notification", "<system-reminder"))
                sessions[sid].append({"k": "prompt", "ts": ts, "text": text.strip(), "cont": cont})
            elif kind == "assistant":
                tools = tool_strings(msg.get("content"))
                mid = msg.get("id") or d.get("uuid")
                u = msg.get("usage")
                if mid in seen:  # streamed blocks of one reply repeat the same usage
                    ev = seen[mid]
                    ev["tools"] += tools
                    if u:
                        ev["usage"] = u
                    continue
                ev = {"k": "reply", "ts": ts, "model": msg.get("model"), "usage": u, "tools": tools}
                seen[mid] = ev
                sessions[sid].append(ev)
    return sessions


_cache = {}  # path -> (mtime, size, parsed)


def load_all(roots):
    sessions = defaultdict(list)
    for root in roots:
        for p in Path(root).expanduser().glob("**/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            hit = _cache.get(key)
            if not hit or hit[0] != st.st_mtime or hit[1] != st.st_size:
                hit = (st.st_mtime, st.st_size, parse_file(p))
                _cache[key] = hit
            for sid, evs in hit[2].items():
                sessions[sid].extend(evs)
    return sessions


def build_turns(events):
    events = sorted(events, key=lambda e: e["ts"])
    turns, cur = [], None
    for e in events:
        if e["k"] == "prompt":
            cur = {"ts": e["ts"], "prompt": e["text"], "cont": e["cont"], "replies": []}
            turns.append(cur)
        elif cur is not None:
            cur["replies"].append(e)
    return turns


def tag_turn(turn):
    """The project whose folder the turn worked in (most matching paths wins), 'skip' if it
    worked only in an excluded folder, or None if it touched no folders at all."""
    strings = [s for r in turn["replies"] for s in r["tools"]]
    if not ALL and EXCLUDE and any(EXCLUDE.search(s) for s in strings):
        return "skip"  # one project: a turn that also worked in another project doesn't count
    hits = {}
    for p in PROJECTS:
        n = sum(1 for s in strings if p["_match"] and p["_match"].search(s))
        if n:
            hits[p["id"]] = n
    if hits:
        return max(hits, key=hits.get)
    if EXCLUDE and any(EXCLUDE.search(s) for s in strings):
        return "skip"
    return None


def assign_projects(turns):
    """A project (or 'skip') for every turn. Turns that touched no folders ("yes do it",
    questions) belong to the project the session was just working on."""
    tags = [tag_turn(t) for t in turns]
    if not ALL:  # one project: the session must be mostly about it, or name it up front
        p = PROJECTS[0]
        mine, other = tags.count(p["id"]), tags.count("skip")
        said_so = any(n.lower() in t["prompt"].lower() for t in turns[:2] for n in p["match"] + [p["name"]] if n)
        if not (mine >= 1 and (mine > other or (said_so and not other))):
            return ["skip"] * len(turns)
    first = next((t for t in tags if t), None) or (OTHER["id"] if ALL else "skip")
    out, prev = [], first
    for t in tags:
        prev = t or prev
        out.append(prev)
    return out


# ---------------------------------------------------------------- finding your projects (for setup)

FOLDER_MARKERS = {"developer", "projects", "project", "repos", "repositories", "code", "src", "dev", "work",
                  "github", "git", "sites", "apps", "scratchpad", "workspace", "documents", "desktop"}
NOT_PROJECTS = {"null", "tmp", "temp", "bin", "lib", "usr", "var", "etc", "node_modules", "library", "applications",
                # folders that live inside projects rather than being one
                "bench", "cargo", "refs", "rec", "vibe", "assets", "render", "tauri", "macos", "windows", "vis", "target",
                "build", "dist", "docs", "tools", "scripts", "test", "tests", "public", "static", "resources", "examples",
                "chrisqtruong", "users", "home", "downloads", "pictures", "movies", "music", "outputs"}
PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\][^\s\"'|;&<>()]+")


def project_of(path):
    """Guess the project folder in a path: the folder after Developer/, projects/, a repo URL..."""
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/\s]|$)", path)
    if m:
        return m.group(2).lower()
    parts = [p for p in re.split(r"[/\\]+", path) if p]
    for i, p in enumerate(parts[:-1]):
        if p.lower() in FOLDER_MARKERS:
            nxt = parts[i + 1]
            if (i + 2 < len(parts) and len(nxt) >= 3 and not nxt.startswith((".", "-")) and " " not in nxt
                    and nxt.lower() not in FOLDER_MARKERS | NOT_PROJECTS and "." not in nxt[1:]):
                return nxt.lower()
    return None


def find_projects(roots):
    """Projects Claude worked in, with what each cost: [(name, dollars, turns)], biggest first."""
    cost, turns_n = defaultdict(float), defaultdict(int)
    for events in load_all(roots).values():
        for t in build_turns(events):
            names = set()
            for r in t["replies"]:
                for s in r["tools"]:
                    for p in PATH_RE.findall(s) + ([s] if "github.com" in s else []):
                        n = project_of(p)
                        if n and n not in ALWAYS_EXCLUDE:
                            names.add(n)
            if not names:
                continue
            c = sum(cost_of(r["model"], r["usage"])[0] for r in t["replies"] if r.get("usage") and r.get("model") not in (None, "<synthetic>"))
            for n in names:
                cost[n] += c / len(names)
                turns_n[n] += 1
    return sorted(((n, cost[n], turns_n[n]) for n in cost), key=lambda x: -x[1])


# ---------------------------------------------------------------- roadmap

VALUE = {"very high": 4, "high": 3, "medium-high": 2.5, "medium": 2, "low": 1}
EFFORT = {"small": 1, "medium": 2, "large": 3}
_roadmaps = {}  # src -> (fetched at, text)


def roadmap_text(proj):
    """The project's roadmap file (a URL or a path); re-read at most every 10 minutes."""
    src = proj.get("roadmap") or ""
    if not src:
        return ""
    at, text = _roadmaps.get(src, (0, None))
    if time.time() - at > 600:
        try:
            new = fetch(src) if re.match(r"https?://", src) else Path(src).expanduser().read_text(encoding="utf-8")
        except Exception:
            new = None
        text = new if new is not None else (text or "")
        _roadmaps[src] = (time.time(), text)
    return text or ""


def fetch(url):
    """Download text. Falls back to curl, since some Python installs ship without
    the certificates urllib needs (python.org's macOS build, until you run its
    'Install Certificates' step)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8")
    except Exception:
        r = subprocess.run(["curl", "-fsSL", "--max-time", "15", url], capture_output=True, text=True)
        if r.returncode:
            raise OSError(r.stderr.strip() or "download failed")
        return r.stdout


def theme_for(title, proj):
    road = [t for t in proj["themes"] if t.get("roadmap")]
    for t in road:
        if t.get("items") and re.search(t["items"], title, re.I):
            return t["id"]
    for t in road:
        if not t.get("items"):
            return t["id"]
    return road[0]["id"] if road else None


def parse_roadmap(proj):
    """Items from a '## Roadmap' section (or the whole file): numbered or bulleted lines,
    optional **bold** titles, optional *High · small · ...* tags, [x] or a 'Done:' line for done."""
    text = roadmap_text(proj)
    if not text:
        return {"items": [], "done": []}
    m = re.search(r"^#{1,3}\s*Roadmap\b[^\n]*$(.*?)(?=^#{1,3}\s|\Z)", text, re.M | re.S | re.I)
    block = m.group(1) if m else text
    items, done = [], []
    for line in block.splitlines():
        im = re.match(r"^(?:\d+\.|[-*])\s+(?:\[( |x|X)\]\s+)?(?:\*\*(.+?)\*\*(.*)|(.+))$", line)
        if not im:
            continue
        box, title, rest = im.group(1), (im.group(2) or im.group(4) or "").strip(), im.group(3) or ""
        title = re.sub(r"\s*\(\[#\d+\].*$", "", title).rstrip(".:")
        if not title:
            continue
        if box and box.lower() == "x":
            done.append({"title": title, "theme": theme_for(title, proj)})
            continue
        tag = re.search(r"\*(Very high|High|Medium-high|Medium|Low)\s*·\s*(small|medium|large)([^·*]*)", rest, re.I)
        value = tag.group(1).capitalize() if tag else "Medium"
        effort = tag.group(2).lower() if tag else "medium"
        cost = bool(tag and "cost" in tag.group(3))
        issue = re.search(r"#(\d+)", rest)
        status = "started" if re.search(r"\b(Shipped|Started|In progress):", rest, re.I) else "open"
        score = VALUE.get(value.lower(), 2) / (EFFORT.get(effort, 2) + (0.5 if cost else 0))
        items.append({"n": len(items) + 1, "title": title, "value": value, "effort": effort + (" + cost" if cost else ""),
                      "issue": int(issue.group(1)) if issue else None, "status": status, "theme": theme_for(title, proj),
                      "score": round(score, 2)})
    dm = re.search(r"^Done:(.*)$", block, re.M)
    if dm:
        done += [{"title": t.strip().rstrip("."), "theme": theme_for(t, proj)} for t in re.findall(r"\*\*(.+?)\*\*", dm.group(1))]
    return {"items": items, "done": done}


def plan_next(roadmap, theme_cost, proj):
    """Recommend what to aim tokens at next, with plain-language reasons."""
    items = roadmap["items"]
    road_themes = [t["id"] for t in proj["themes"] if t.get("roadmap")]
    spend = {t: theme_cost.get(t, 0) for t in road_themes}
    spend_total = sum(spend.values()) or 1
    value = defaultdict(float)
    for it in items:
        value[it["theme"]] += VALUE.get(it["value"].lower(), 2)
    value_total = sum(value.values()) or 1
    gaps = {t: value[t] / value_total - spend[t] / spend_total for t in road_themes}
    names = {t["id"]: t["name"] for t in proj["themes"]}

    picks = []
    started = [i for i in items if i["status"] == "started"]
    if started:
        s = started[0]
        picks.append({"n": s["n"], "title": s["title"], "theme": s["theme"],
                      "why": f"Already started and #{s['n']} on the roadmap. Finishing it turns tokens already spent into something people can use."})
    pool = [i for i in items if i["status"] == "open"]
    if len(road_themes) > 1:
        hungry = max(gaps, key=gaps.get)
        themed = [i for i in pool if i["theme"] == hungry] or pool
        if themed:
            best = max(themed, key=lambda i: (i["score"], -i["n"]))
            share = round(100 * spend[best["theme"]] / spend_total)
            picks.append({"n": best["n"], "title": best["title"], "theme": best["theme"],
                          "why": f"\"{names[best['theme']]}\" holds {round(100 * value[best['theme']] / value_total)}% of the roadmap's value but got {share}% of roadmap spend. "
                                 f"This is its best value-for-effort item ({best['value'].lower()} value, {best['effort']} effort)."})
    while pool and len(picks) < 3:
        quick = max((i for i in pool if all(i["n"] != p["n"] for p in picks)), key=lambda i: (i["score"], -i["n"]), default=None)
        if not quick:
            break
        picks.append({"n": quick["n"], "title": quick["title"], "theme": quick["theme"],
                      "why": f"Best value for effort left on the roadmap ({quick['value'].lower()} value, {quick['effort']} effort)." if quick["score"] != 1
                      else f"Next on the roadmap that hasn't been started."})
    return picks, {t: round(g, 3) for t, g in gaps.items()}, {t: round(value[t] / value_total, 3) for t in road_themes}


# ---------------------------------------------------------------- report

def model_family(m):
    """opus, sonnet, gpt, gemini... used for the model's color on the page."""
    m = m.lower()
    for fam in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if fam in m:
            return fam
    for fam, pat in (("gpt", r"^(?:gpt|o\d|codex|chatgpt)"), ("gemini", r"^gemini"), ("llama", r"llama"),
                     ("mistral", r"mistral|codestral"), ("deepseek", r"deepseek"), ("grok", r"grok"), ("qwen", r"qwen")):
        if re.search(pat, m):
            return fam
    return "other"


def model_maker(m):
    return {"opus": "Anthropic", "sonnet": "Anthropic", "haiku": "Anthropic", "fable": "Anthropic", "mythos": "Anthropic",
            "gpt": "OpenAI", "gemini": "Google", "llama": "Meta", "mistral": "Mistral", "deepseek": "DeepSeek",
            "grok": "xAI", "qwen": "Alibaba"}.get(model_family(m), "Other")


def model_name(m):
    """claude-opus-5-5 -> Claude Opus 5.5, claude-3-5-sonnet-20241022 -> Claude Sonnet 3.5, gpt-5-codex -> GPT-5 Codex."""
    base = re.sub(r"-\d{8}$|@\d{8}$", "", m)
    c = re.match(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?$", base)
    if c:
        return f"Claude {c.group(1).title()} {c.group(2)}" + (f".{c.group(3)}" if c.group(3) else "")
    c = re.match(r"^claude-(\d+)(?:-(\d+))?-(opus|sonnet|haiku)$", base)
    if c:
        return f"Claude {c.group(3).title()} {c.group(1)}" + (f".{c.group(2)}" if c.group(2) else "")
    if base.lower().startswith("gpt-"):
        head, *rest = base[4:].split("-")
        return "GPT-" + head + "".join(" " + w.title() for w in rest)
    return " ".join(w if any(ch.isdigit() for ch in w) else w.title() for w in base.replace("_", "-").split("-"))


def local_day(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


def collect(roots, live):
    """This computer's sessions -> ({session_id: {project_id: totals}}, recent turns)."""
    out, recent = {}, []
    by_id = {p["id"]: p for p in PROJECTS}
    for sid, events in load_all(roots).items():
        turns = build_turns(events)
        if not turns:
            continue
        per = {}
        prev_bucket = {}
        for t, pid in zip(turns, assign_projects(turns)):
            if pid == "skip":
                continue
            proj = by_id.get(pid)
            if pid not in per:
                per[pid] = {"days": defaultdict(lambda: defaultdict(lambda: {"cost": 0.0, "tokens": 0, "output": 0})),
                            "buckets": defaultdict(lambda: {"cost": 0.0, "tokens": 0, "turns": 0}),
                            "models": defaultdict(lambda: {"cost": 0.0, "tokens": 0, "output": 0, "replies": 0}),
                            "totals": {"cost": 0.0, "tokens": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "turns": 0}}
            agg = per[pid]
            if proj:
                touched = [s for r in t["replies"] for s in r["tools"]]
                prev = prev_bucket.get(pid, proj["default_bucket"])
                bucket = prev if t["cont"] else (classify(t["prompt"], touched, proj) or prev)
                prev_bucket[pid] = bucket
            else:  # everything else: no buckets of its own
                bucket = OTHER["id"]
            tcost, ttok = 0.0, 0
            for r in t["replies"]:
                if not r.get("usage") or r.get("model") in (None, "<synthetic>"):
                    continue
                dollars, parts = cost_of(r["model"], r["usage"])
                tok = sum(parts.values())
                cell = agg["days"][local_day(r["ts"])][bucket]
                cell["cost"] += dollars
                cell["tokens"] += tok
                cell["output"] += parts["output"]
                mod = agg["models"][r["model"]]
                mod["cost"] += dollars
                mod["tokens"] += tok
                mod["output"] += parts["output"]
                mod["replies"] += 1
                for k, v in parts.items():
                    agg["totals"][k] += v
                agg["totals"]["cost"] += dollars
                agg["totals"]["tokens"] += tok
                tcost += dollars
                ttok += tok
            if not t["replies"]:
                continue
            agg["totals"]["turns"] += 1
            b = agg["buckets"][bucket]
            b["cost"] += tcost
            b["tokens"] += ttok
            b["turns"] += 1
            if live and not t["cont"]:
                recent.append({"ts": t["ts"], "project": pid, "bucket": bucket, "prompt": t["prompt"][:120], "cost": round(tcost, 2), "tokens": ttok})
        if per:
            out[sid] = per
    return out, recent


def rounded(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: rounded(x) for k, x in v.items()}
    return v


def add_up(aggs):
    """Sum per-session totals: (days, by_bucket, models, totals)."""
    days = defaultdict(lambda: defaultdict(lambda: {"cost": 0.0, "tokens": 0, "output": 0}))
    by_bucket = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "output": 0, "turns": 0})
    totals = defaultdict(float, {k: 0.0 for k in ("cost", "tokens", "input", "output", "cache_write", "cache_read", "turns")})
    models = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "output": 0, "replies": 0})
    for s in aggs:
        for m, v in s.get("models", {}).items():   # computers on older versions don't send this
            for k in models[m]:
                models[m][k] += v.get(k, 0)
        for d, cells in s["days"].items():
            for bid, c in cells.items():
                for k in ("cost", "tokens", "output"):
                    days[d][bid][k] += c[k]
                by_bucket[bid]["output"] += c["output"]
        for bid, b in s["buckets"].items():
            for k in ("cost", "tokens", "turns"):
                by_bucket[bid][k] += b[k]
        for k, v in s["totals"].items():
            totals[k] += v
    for k in ("tokens", "input", "output", "cache_write", "cache_read", "turns"):
        totals[k] = int(totals[k])
    for d in days.values():
        for c in d.values():
            c["tokens"], c["output"] = int(c["tokens"]), int(c["output"])
    return days, by_bucket, models, totals


def models_out(models):
    return [{"id": m, "name": model_name(m), "maker": model_maker(m), "family": model_family(m), "priced": m in PRICES}
            | rounded(dict(v)) | {"tokens": int(v["tokens"]), "output": int(v["output"]), "replies": int(v["replies"])}
            for m, v in sorted(models.items(), key=lambda kv: -kv[1]["cost"])]


def days_out(days):
    return [{"date": d, "by": rounded({b: dict(c) for b, c in days[d].items()})} for d in sorted(days)]


def report(proj, aggs, recent, n_sessions):
    """What the page shows for one project."""
    days, by_bucket, models, totals = add_up(aggs)
    theme_of = {b["id"]: b["theme"] for b in proj["buckets"]}
    fallback = next((t["id"] for t in proj["themes"] if not t.get("roadmap")), proj["themes"][-1]["id"])
    theme_cost = defaultdict(float)
    for bid, v in by_bucket.items():
        theme_cost[theme_of.get(bid, fallback)] += v["cost"]
    roadmap = parse_roadmap(proj)
    picks, gaps, value_share = plan_next(roadmap, theme_cost, proj)
    on_road = sum(theme_cost[t["id"]] for t in proj["themes"] if t.get("roadmap"))
    link = proj.get("roadmap_link") or ""
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/", proj.get("roadmap") or "")
    if not link and m:
        link = f"https://github.com/{m.group(1)}/{m.group(2)}#roadmap"
    return {
        "id": proj["id"], "color": proj["color"], "product": proj["name"],
        "sessions": n_sessions,
        "buckets": [{k: b[k] for k in ("id", "name", "color", "theme")} | rounded(dict(by_bucket[b["id"]])) for b in proj["buckets"]],
        "themes": [{k: t.get(k) for k in ("id", "name", "blurb", "roadmap")} | {"cost": round(theme_cost[t["id"]], 4), "value_share": value_share.get(t["id"]), "gap": gaps.get(t["id"])} for t in proj["themes"]],
        "days": days_out(days),
        "totals": rounded(dict(totals)),
        "models": models_out(models),
        "alignment": round(on_road / (sum(theme_cost.values()) or 1), 3),
        "roadmap": roadmap, "roadmap_link": link, "roadmap_rule": proj.get("roadmap_rule") or "",
        "next": picks,
        "recent": sorted(recent, key=lambda r: r["ts"])[-12:],
    }


def combine(sessions, live, recent=(), machines=1):
    """Add up sessions (from any number of computers) into what the page shows. One project:
    that project's report. Several: an overview where each project is a bucket, plus a report each."""
    head = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "live": live, "machines": machines,
            "pricing": {"model": DEFAULT_MODEL, **PRICES[DEFAULT_MODEL]}}
    per = defaultdict(list)
    count = defaultdict(int)
    for s in sessions.values():
        for pid, agg in s.items():
            per[pid].append(agg)
            count[pid] += 1
    if not ALL:
        p = PROJECTS[0]
        return head | report(p, per.get(p["id"], []), [r for r in recent if r["project"] == p["id"]], count[p["id"]])

    projects = []
    for p in PROJECTS:
        if per.get(p["id"]):
            projects.append(report(p, per[p["id"]], [r for r in recent if r["project"] == p["id"]], count[p["id"]]))
    # the overview: projects are the buckets; everything else is folded in as one more
    over_aggs = []
    for pid, aggs in per.items():
        for a in aggs:
            over_aggs.append({"days": {d: {pid: {k: sum(c[k] for c in cells.values()) for k in ("cost", "tokens", "output")}}
                                       for d, cells in a["days"].items()},
                              "buckets": {pid: {k: sum(b[k] for b in a["buckets"].values()) for k in ("cost", "tokens", "turns")}},
                              "models": a.get("models", {}), "totals": a["totals"]})
    days, by_bucket, models, totals = add_up(over_aggs)
    entries = [{"id": p["id"], "name": p["name"], "color": p["color"], "theme": ""} for p in PROJECTS] + \
              [{"id": OTHER["id"], "name": OTHER["name"], "color": OTHER["color"], "theme": ""}]
    overview = {
        "id": "all", "product": "All projects", "overview": True, "sessions": len(sessions),
        "buckets": [e | rounded(dict(by_bucket[e["id"]])) for e in entries],
        "themes": [], "days": days_out(days), "totals": rounded(dict(totals)), "models": models_out(models),
        "alignment": 0, "roadmap": {"items": [], "done": []}, "roadmap_link": "", "roadmap_rule": "", "next": [],
        "recent": [r | {"bucket": r["project"]} for r in sorted(recent, key=lambda r: r["ts"])[-12:]],
        "other_blurb": OTHER["blurb"],
    }
    return head | {"mode": "all", "overview": overview, "projects": projects}


# ---------------------------------------------------------------- publishing
# The swear-jar branch of your repo holds the page (index.html), one file per
# computer (machines/<id>.json, totals per session) and data.json, all of them
# added up. Each computer rewrites only its own file, so they never overwrite
# each other. GitHub Pages serves the branch as your dashboard.

def machine_id():
    host = hashlib.sha1(socket.gethostname().encode()).hexdigest()[:6]
    return f"{platform.system().lower()}-{host}"


PUBLISHED = HOME / "published"


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(PUBLISHED), *args], capture_output=True, text=True, check=check)


def repo_url():
    return f"https://github.com/{CFG['publish_repo']}.git"


def ensure_data_clone():
    if (PUBLISHED / ".git").exists():
        if git("remote", "get-url", "origin", check=False).stdout.strip() != repo_url():
            git("remote", "set-url", "origin", repo_url())
        return
    HOME.mkdir(parents=True, exist_ok=True)
    has_branch = subprocess.run(["git", "ls-remote", "--heads", repo_url(), DATA_BRANCH], capture_output=True, text=True).stdout.strip()
    if has_branch:
        subprocess.run(["git", "clone", "-q", "--single-branch", "--branch", DATA_BRANCH, repo_url(), str(PUBLISHED)], check=True)
    else:  # first computer: start a branch that shares nothing with the repo's other branches
        PUBLISHED.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(PUBLISHED)], check=True)
        git("remote", "add", "origin", repo_url())
        git("checkout", "-q", "--orphan", DATA_BRANCH)


def other_machines():
    """Sessions the other computers published, from the local copy of the branch."""
    out, n = {}, 0
    for f in sorted((PUBLISHED / "machines").glob("*.json")):
        if f.stem == machine_id():
            continue
        try:
            got = json.loads(f.read_text(encoding="utf-8"))
            if got.get("version", 1) < 2:  # older files: one project per session
                got["sessions"] = {sid: {PROJECTS[0]["id"]: agg} for sid, agg in got["sessions"].items()}
            out.update(got["sessions"])
            n += 1
        except (OSError, ValueError, KeyError):
            pass
    return out, n


_pages_checked = False


def ensure_pages():
    """Turn on GitHub Pages for the swear-jar branch (needs the gh command); once per run."""
    global _pages_checked
    if _pages_checked or not shutil.which("gh"):
        return
    repo = CFG["publish_repo"]
    r = subprocess.run(["gh", "api", f"repos/{repo}/pages", "-q", ".source.branch"], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/pages", "-f", f"source[branch]={DATA_BRANCH}", "-f", "source[path]=/"], capture_output=True, text=True)
    elif r.stdout.strip() != DATA_BRANCH:
        r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{repo}/pages", "-f", f"source[branch]={DATA_BRANCH}", "-f", "source[path]=/"], capture_output=True, text=True)
    _pages_checked = r.returncode == 0


def pages_url():
    owner, name = CFG["publish_repo"].split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/" if name.lower() != f"{owner.lower()}.github.io" else f"https://{name}/"


def publish(roots):
    ensure_data_clone()
    if git("ls-remote", "--heads", "origin", DATA_BRANCH, check=False).stdout.strip():
        git("pull", "-q", "--rebase", "origin", DATA_BRANCH, check=False)
    mine, _ = collect(roots, live=False)
    (PUBLISHED / "machines").mkdir(exist_ok=True)
    (PUBLISHED / "machines" / f"{machine_id()}.json").write_text(
        json.dumps({"machine": machine_id(), "version": 2, "sessions": rounded(mine)}, sort_keys=True) + "\n", encoding="utf-8")
    shutil.copyfile(HERE / "index.html", PUBLISHED / "index.html")
    (PUBLISHED / ".nojekyll").write_text("")
    (PUBLISHED / "README.md").write_text("The Swear Jar dashboard and its data. Written automatically by swearjar.py; don't edit.\n")
    others, n = other_machines()
    data = combine({**others, **mine}, live=False, machines=n + 1)
    old = PUBLISHED / "data.json"
    try:  # don't commit a new file just because the timestamp moved
        before = json.loads(old.read_text(encoding="utf-8"))
        before.pop("generated", None)
    except (OSError, ValueError):
        before = None
    if before != {k: v for k, v in json.loads(json.dumps(data)).items() if k != "generated"}:
        old.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        ensure_pages()
        return "no change"
    git("commit", "-q", "-m", f"Swear Jar data from {machine_id()}")
    for _ in range(2):
        if git("push", "-q", "-u", "origin", DATA_BRANCH, check=False).returncode == 0:
            ensure_pages()
            return f"published ${(data.get('totals') or data['overview']['totals']).get('cost', 0):,.2f}"
        git("pull", "-q", "--rebase", "origin", DATA_BRANCH, check=False)
    return "push failed (will retry)"


def publish_loop(roots):
    while True:
        try:
            with Handler.lock:
                load_config()
                result = publish(roots)
        except Exception as e:  # keep serving even if git or the network hiccups
            result = f"publish error: {e}"
        print(f"{datetime.now():%Y-%m-%d %H:%M} {result}", flush=True)
        time.sleep(PUBLISH_EVERY)


# ---------------------------------------------------------------- server

class Handler(SimpleHTTPRequestHandler):
    roots = []
    lock = threading.Lock()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def do_GET(self):
        if self.path.split("?")[0] == "/data.json":
            with self.lock:
                load_config()
                mine, recent = collect(self.roots, live=True)
                others, n = other_machines() if CFG.get("publish_repo") else ({}, 0)
                body = json.dumps(combine({**others, **mine}, live=True, recent=recent, machines=n + 1)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- setup

def ask(question, default=""):
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{shown}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return answer.startswith("y") if answer else default


def setup(roots, example=None):
    if example:
        src = HERE / "examples" / f"{example}.json"
        if not src.exists():
            sys.exit(f"No example called {example}. Try: " + ", ".join(p.stem for p in (HERE / "examples").glob("*.json")))
        save_config(json.loads(src.read_text(encoding="utf-8")))
        print(f"Using the {example} settings ({src}). Saved to {CONFIG_FILE}.")
        return

    print("\nSwear Jar setup. Press Enter to take the suggestion in [brackets].\n")
    print("Looking through your Claude Code history for projects...")
    found = find_projects(roots)
    print("\nSwear Jar can follow one project, or all of them on one dashboard (with a tab for each).")
    if ask("One project or all? (one/all)", "all").lower().startswith("a"):
        return setup_all(roots, found)
    cfg = {k: v for k, v in GENERAL.items() if k not in ("buckets", "themes")}
    if found:
        print()
        for i, (n, c, t) in enumerate(found[:10], 1):
            print(f"  {i:>2}. {n:<28} ${c:>9,.2f}  {t:>5} turns")
        print()
        pick = ask("Which one do you want to track? A number, or type a folder name", "1")
    else:
        print("Didn't find any project folders yet. You can type the name of the folder your project lives in.")
        pick = ask("Project folder name")
    if pick.isdigit() and 1 <= int(pick) <= len(found):
        name = found[int(pick) - 1][0]
    else:
        name = pick.strip().lower()
    if not name:
        sys.exit("Setup needs a project. Run it again when you've worked on one with Claude Code.")
    cfg["match"] = [name]
    cfg["product"] = ask("Name to show on the dashboard", name[:1].upper() + name[1:])
    cfg["exclude"] = [n for n, c, _ in found if n != name and c >= 1][:20]   # other projects with real work

    print("\nOptional: a roadmap. Point to your README (or a ROADMAP.md) with a list under a 'Roadmap' heading,")
    print("as a link (e.g. https://raw.githubusercontent.com/you/repo/main/README.md) or a file path.")
    print("The dashboard then compares where tokens went with what the roadmap says matters.")
    cfg["roadmap"] = ask("Roadmap link or path (Enter to skip)", "")

    print("\nOptional: a dashboard on the web. Swear Jar can publish to a GitHub repo you own (it uses a")
    print(f"branch called '{DATA_BRANCH}' and turns on GitHub Pages; nothing else in the repo is touched).")
    print("Needs git signed in to GitHub. The 'gh' command (cli.github.com) also creates the repo and turns on Pages for you.")
    repo = ask("GitHub repo as owner/name (Enter to keep it on this computer)", "")
    if repo:
        repo = re.sub(r"^https?://github\.com/|\.git$", "", repo).strip("/")
        if shutil.which("gh") and subprocess.run(["gh", "repo", "view", repo], capture_output=True).returncode != 0:
            if yes(f"{repo} doesn't exist yet. Create it as a public repo?"):
                subprocess.run(["gh", "repo", "create", repo, "--public", "--description", f"Swear Jar: where my Claude tokens go for {cfg['product']}"], check=False)
        cfg["publish_repo"] = repo
    cfg["buckets"], cfg["themes"] = GENERAL["buckets"], GENERAL["themes"]
    save_config(cfg)
    print(f"\nSaved to {CONFIG_FILE}. Edit it any time: rename buckets, change their words, add themes.")
    if yes("\nStart Swear Jar whenever you log in, so the dashboard is always up to date?"):
        load_config()
        install()
    else:
        print(f"Run it any time with: python3 {Path(__file__).resolve()}")


CLONE_DIRS = ["Developer", "Projects", "projects", "code", "Code", "src", "dev", "repos", "GitHub", "Documents/GitHub", "work"]


def local_clones():
    """{repo name: [local folder names]} for git clones in the usual places."""
    out = defaultdict(list)
    for d in CLONE_DIRS:
        base = Path.home() / d
        if not base.is_dir():
            continue
        for sub in base.iterdir():
            if (sub / ".git").exists():
                url = subprocess.run(["git", "-C", str(sub), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
                repo = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1]) if url else sub.name
                out[repo.lower()].append(sub.name)
    return out


def github_repos():
    """Your GitHub repo names, if the gh command is installed and signed in."""
    if not shutil.which("gh"):
        return []
    r = subprocess.run(["gh", "repo", "list", "--limit", "200", "--json", "name", "-q", ".[].name"], capture_output=True, text=True)
    return [n for n in r.stdout.split() if n] if r.returncode == 0 else []


def pretty(name):
    """weather-in-dots -> Weather in dots; leaves names with dots (my.site.io) alone."""
    return name if "." in name else (name[:1].upper() + name[1:]).replace("-", " ").replace("_", " ")


def preset(pid):
    """examples/<project>.json, if there is one: custom buckets, themes and roadmap."""
    f = HERE / "examples" / f"{pid}.json"
    if not f.exists():
        return {}
    ex = json.loads(f.read_text(encoding="utf-8"))
    return {k: ex[k] for k in ("product", "roadmap", "roadmap_link", "roadmap_rule", "default_bucket", "buckets", "themes") if k in ex}


def setup_all(roots, found):
    clones = local_clones()
    repos = github_repos()
    projects, seen = [], set()
    for repo in repos:
        names = [repo] + [f for f in clones.get(repo.lower(), []) if f.lower() != repo.lower()]
        projects.append({"product": pretty(repo), "match": names})
        seen |= {n.lower() for n in names}
    for repo, folders in clones.items():   # local clones of repos that aren't yours on GitHub
        if repo not in seen and not seen & {f.lower() for f in folders}:
            projects.append({"product": pretty(repo), "match": sorted({repo, *folders})})
            seen |= {repo, *(f.lower() for f in folders)}
    for n, c, _ in found:                   # folders Claude worked in that aren't clones
        if n not in seen and c >= 2:
            projects.append({"product": pretty(n), "match": [n]})
            seen.add(n)
    if not projects:
        sys.exit("Didn't find any projects yet. Run setup again after you've worked on one with Claude Code.")
    cost = {n: c for n, c, _ in found}
    print("\nProjects found" + (" (your GitHub repos, your local clones, and folders Claude worked in)" if repos else "") + ":\n")
    for i, p in enumerate(projects, 1):
        spent = max((cost.get(m.lower(), 0) for m in p["match"]), default=0)
        print(f"  {i:>2}. {p['product']:<28} {'$' + format(spent, ',.2f') if spent else 'no Claude work found yet':>12}")
    drop = ask("\nLeave any out? Numbers separated by commas (Enter to keep them all)", "")
    gone = {int(x) for x in re.findall(r"\d+", drop)}
    projects = [p for i, p in enumerate(projects, 1) if i not in gone]
    for p in projects:
        p.update(preset(slug(p["match"][0])))
    print("Work that touched none of these goes under 'Everything else'.")
    print("\nOptional: a dashboard on the web. Swear Jar can publish to a GitHub repo you own (a")
    print(f"'{DATA_BRANCH}' branch, served by GitHub Pages; nothing else in the repo is touched).")
    repo = ask("GitHub repo as owner/name (Enter to keep it on this computer)", "")
    cfg = {"projects": projects, "exclude": [], "publish_repo": ""}
    if repo:
        repo = re.sub(r"^https?://github\.com/|\.git$", "", repo).strip("/")
        if shutil.which("gh") and subprocess.run(["gh", "repo", "view", repo], capture_output=True).returncode != 0:
            if yes(f"{repo} doesn't exist yet. Create it as a public repo?"):
                subprocess.run(["gh", "repo", "create", repo, "--public", "--description", "Swear Jar: where my Claude tokens go"], check=False)
        cfg["publish_repo"] = repo
    save_config(cfg)
    print(f"\nSaved to {CONFIG_FILE}. Rename projects, pick colors or add a roadmap per project there.")
    if yes("\nStart Swear Jar whenever you log in, so the dashboard is always up to date?"):
        load_config()
        install()
    else:
        print(f"Run it any time with: python3 {Path(__file__).resolve()}")


# ---------------------------------------------------------------- start at login

PLIST = Path.home() / "Library" / "LaunchAgents" / "com.swearjar.dashboard.plist"
OLD_PLISTS = [Path.home() / "Library" / "LaunchAgents" / "com.chrisqtruong.coinop.plist"]
WIN_STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "swear-jar.cmd"


def run_args():
    return [str(Path(__file__).resolve()), "--no-open"] + (["--publish"] if CFG.get("publish_repo") else [])


def install():
    HOME.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        log = str(HOME / "log.txt")
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in [sys.executable] + run_args())
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.swearjar.dashboard</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
        uid = os.getuid()
        for p in OLD_PLISTS + [PLIST]:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(p)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
        where = f" and publishing to {pages_url()}" if CFG.get("publish_repo") else ""
        print(f"Swear Jar now runs from login: http://localhost:{PORT}{where}\nLog: {log}\nUndo: python3 {Path(__file__).resolve()} --uninstall")
    elif sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else sys.executable)
        WIN_STARTUP.write_text("@start \"\" " + " ".join(f'"{a}"' for a in [exe] + run_args()) + "\r\n")
        subprocess.Popen([exe] + run_args(), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        print(f"Swear Jar now runs from login: http://localhost:{PORT}\nUndo: python {Path(__file__).resolve()} --uninstall")
    else:
        sys.exit("--install supports macOS and Windows. On Linux, run the script from your own startup (e.g. a systemd user service).")


def uninstall():
    if sys.platform == "darwin":
        for p in OLD_PLISTS + [PLIST]:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)
            p.unlink(missing_ok=True)
    elif sys.platform == "win32":
        WIN_STARTUP.unlink(missing_ok=True)
        print("Removed from startup. Close the running copy from Task Manager (pythonw) or restart.")
    print("Swear Jar no longer starts at login. Anything already published stays where it is.")


def main():
    ap = argparse.ArgumentParser(description="Swear Jar: where your Claude tokens go")
    ap.add_argument("command", nargs="?", choices=["setup"], help="setup: choose your product and where to publish")
    ap.add_argument("--example", help="with setup: start from examples/<name>.json instead of asking")
    ap.add_argument("--install", action="store_true", help="run from login (and publish, if a repo is set)")
    ap.add_argument("--uninstall", action="store_true", help="undo --install")
    ap.add_argument("--publish", action="store_true", help="publish to the GitHub repo in your settings every few minutes")
    ap.add_argument("--projects", action="append", help="extra transcript folder (repeatable), e.g. a copy from another computer")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args()
    roots = [TRANSCRIPTS] + [Path(p) for p in args.projects or []]

    if args.uninstall:
        return uninstall()
    if args.command == "setup":
        return setup(roots, args.example)
    if not load_config():
        if sys.stdin.isatty():
            print("No settings yet, so let's set up first.")
            return setup(roots)
        sys.exit(f"No settings yet. Run: python3 {Path(__file__).resolve()} setup")
    if args.install:
        return install()
    if args.publish and not CFG.get("publish_repo"):
        sys.exit("No GitHub repo in your settings. Run setup again, or add \"publish_repo\": \"owner/name\" to " + str(CONFIG_FILE))

    Handler.roots = roots
    collect(roots, live=True)  # warm the cache before the first page load
    url = f"http://localhost:{args.port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"Swear Jar is already running at {url}")
        if not args.no_open:
            webbrowser.open(url)
        return
    print(f"Swear Jar is live at {url}  (Ctrl+C to stop)", flush=True)
    if args.publish:
        threading.Thread(target=publish_loop, args=(roots,), daemon=True).start()
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
