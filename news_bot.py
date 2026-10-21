#!/usr/bin/env python3
"""
news_bot.py - the whole news pipeline in one script, so it can run on GitHub Actions
(free, and it keeps running when your own computer is off).

  python news_bot.py shorts [--topic sports]   -> makes one Short and sends it to Telegram
  python news_bot.py daily  [--topic sports]   -> makes the full-length daily video and sends it to Telegram
(topic defaults to "world"; topics and their news feeds are defined in topics.json)

Secrets come from environment variables (GitHub Secrets):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PIXABAY_API_KEY (optional)
"""
import datetime
import email.utils
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import shorts  # the video renderer  # noqa: E402

CFG = shorts.CFG
PROMPTS = json.loads((ROOT / "prompts.json").read_text(encoding="utf-8"))

TOPICS = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
MATCH = 0.2
MIN_SOURCES = 2
SHORTS_MAX_AGE_H = 20
DAILY_MAX_AGE_H = 24
DAILY_STORIES = 6
MODELS = CFG.get("gemini_models") or ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-lite-latest"]
STOP = set("the and for with from after before over under into about amid says say said new more than this "
           "that their its his her they will would could may might not but who what when where why how out "
           "off has have had was were are been being also just can our your you one two".split())


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ topics
def state_name(topic, kind):
    return "state_%s.json" % kind if topic == "world" else "state_%s_%s.json" % (topic, kind)


def apply_topic(T):
    """Point the video renderer at this topic's look and fallback footage."""
    shorts.IMAGE_STYLE = T["image_style"]
    shorts.GENERIC_TERMS = T["generic_terms"]
    shorts.GENERIC_PROMPTS = T["generic_prompts"]
    shorts.BADGE_LABEL = T["badge"]


# ------------------------------------------------------------------ memory
def load_state(name):
    p = ROOT / name
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    st.setdefault("seen", {})
    st.setdefault("topics", [])
    st.setdefault("daily", {"date": "", "count": 0})
    now = time.time()
    st["seen"] = {k: v for k, v in st["seen"].items() if now - v < 4 * 86400}
    st["topics"] = [t for t in st["topics"] if now - t["t"] < 3 * 86400]
    today = datetime.date.today().isoformat()
    if st["daily"].get("date") != today:
        st["daily"] = {"date": today, "count": 0}
    return st


def save_state(name, st):
    (ROOT / name).write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------------ news feeds
def clean(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'"),
                 ("&#039;", "'"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = s.replace("&amp;", "&")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tag(block, name):
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (name, name), block, re.S | re.I)
    return clean(m.group(1)) if m else ""


def parse_date(s):
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def toks(s):
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", s.lower()).split() if len(w) > 2 and w not in STOP}


def jacc(a, b):
    i = len(a & b)
    return i / ((len(a) + len(b) - i) or 1)


def fetch_items(max_age_h, feeds):
    now = time.time()
    items, errors, ok = [], [], 0
    for name, url in feeds:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            blocks = re.findall(r"<item[\s>].*?</item>", r.text, re.S | re.I)
            if not blocks:
                raise ValueError("no items")
            ok += 1
            for b in blocks[:40]:
                title = tag(b, "title")
                link = tag(b, "link") or tag(b, "guid")
                if not title or not link:
                    continue
                summary = tag(b, "description")[:400]
                ts = parse_date(tag(b, "pubDate") or tag(b, "dc:date") or tag(b, "published"))
                age = 1.0 if ts is None else (now - ts) / 3600
                if age > max_age_h or age < -1:
                    continue
                items.append({"source": name, "title": title, "link": link, "summary": summary, "age": age,
                              "tk": toks(title + " " + " ".join(summary.split()[:25]))})
        except Exception as e:
            errors.append("%s: %s" % (name, str(e)[:80]))
    log("feeds loaded: %d of %d" % (ok, len(feeds)), ("| problems: " + "; ".join(errors)) if errors else "")
    if ok < 2:
        raise RuntimeError("Fewer than 2 news feeds loaded. " + "; ".join(errors))
    return items


def cluster(items):
    items.sort(key=lambda i: i["age"])
    clusters = []
    for it in items:
        home = None
        for c in clusters:
            if it["source"] in c["sources"]:
                continue
            if jacc(it["tk"], c["seed"]) >= MATCH and len(it["tk"] & c["seed"]) >= 3:
                home = c
                break
        if home:
            home["items"].append(it)
            home["sources"].add(it["source"])
        else:
            clusters.append({"seed": it["tk"], "items": [it], "sources": {it["source"]}})
    return clusters


def rank(clusters, st, use_topics, skip):
    out = []
    for c in clusters:
        if len(c["sources"]) < MIN_SOURCES:
            continue
        if skip.search(" ".join(i["title"] + " " + i["summary"] for i in c["items"])):
            continue
        if any(i["link"] in st["seen"] for i in c["items"]):
            continue
        if use_topics and any(jacc(c["seed"], set(t["tokens"])) >= 0.35 for t in st["topics"]):
            continue
        score = len(c["sources"]) * 10 - min(i["age"] for i in c["items"]) * 0.5
        out.append((score, c))
    out.sort(key=lambda x: -x[0])
    return [c for _, c in out]


def story_text(c, limit=5):
    its = c["items"][:limit]
    return its, "\n".join("[%s] %s%s" % (i["source"], i["title"],
                                          (" - " + i["summary"][:350]) if i["summary"] else "") for i in its)


def mark_seen(st, c):
    for i in c["items"]:
        st["seen"][i["link"]] = time.time()
    st["topics"].append({"t": time.time(), "tokens": sorted(c["seed"])})


# ------------------------------------------------------------------ Gemini
def parse_json(txt):
    txt = re.sub(r"```json|```", "", txt).strip()
    try:
        return json.loads(txt)
    except ValueError:
        pass
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    body = re.sub(r",\s*([}\]])", r"\1", txt[start:end + 1])  # drop trailing commas
    return json.loads(body, strict=False)  # strict=False allows raw line breaks inside strings


def call_gemini(system, user_text, temperature=None, max_tokens=None):
    key = os.environ["GEMINI_API_KEY"]
    gen = {"responseMimeType": "application/json"}
    if temperature is not None:
        gen["temperature"] = temperature
    if max_tokens:
        gen["maxOutputTokens"] = max_tokens
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}], "generationConfig": gen}
    problems = []
    for m in MODELS:
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json=body, timeout=180)
            except requests.RequestException as e:
                problems.append("%s: network error (%s)" % (m, type(e).__name__))
                time.sleep(8)
                continue
            if r.status_code == 200:
                cand = (r.json().get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                txt = "".join(p.get("text", "") for p in parts)
                finish = cand.get("finishReason", "")
                if txt.strip():
                    try:
                        data = parse_json(txt)
                        log("Gemini model used:", m)
                        return data
                    except ValueError:
                        problems.append("%s: invalid JSON (%d characters, finish=%s)" % (m, len(txt), finish or "?"))
                        if attempt == 0:  # one more try with the same model
                            time.sleep(3)
                            continue
                        break
                problems.append("%s: empty reply (finish=%s)" % (m, finish or "?"))
                break
            problems.append("%s: HTTP %s %s" % (m, r.status_code, r.text[:110].replace(key, "***").replace("\n", " ")))
            if r.status_code in (500, 503):  # busy: wait and retry the same model
                time.sleep(10 * (attempt + 1))
                continue
            break  # 404 (retired), 429 (quota), 400: try the next model
        log("Gemini problem with %s, trying the next model" % m)
    raise RuntimeError("Gemini failed on every model. " + " | ".join(problems[-6:]))


# ------------------------------------------------------------------ Telegram
def _tg(method):
    return "https://api.telegram.org/bot%s/%s" % (os.environ["TELEGRAM_BOT_TOKEN"], method)


def _check(r, what):
    if not r.ok:
        raise RuntimeError("Telegram %s failed (HTTP %s): %s" % (what, r.status_code, r.text[:200]))


def tg_message(text):
    r = requests.post(_tg("sendMessage"), data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000],
                                                "disable_web_page_preview": "true"}, timeout=60)
    _check(r, "message")


def tg_photo(path, caption=""):
    with open(path, "rb") as f:
        r = requests.post(_tg("sendPhoto"), data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": caption[:1000]},
                          files={"photo": f}, timeout=120)
    _check(r, "photo")


def tg_video(path, caption="", width=None, height=None):
    data = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": caption[:1000], "supports_streaming": "true"}
    if width and height:
        data.update({"width": width, "height": height})
    with open(path, "rb") as f:
        r = requests.post(_tg("sendVideo"), data=data, files={"video": f}, timeout=900)
    _check(r, "video")


def telegram_copy(src, dst):
    """Telegram bots can't send files over 50 MB, so make a smaller copy if needed."""
    if Path(src).stat().st_size <= 48 * 1024 * 1024:
        return Path(src)
    for width, crf in ((1280, 27), (854, 30), (640, 33)):
        shorts.run(["ffmpeg", "-y", "-i", src, "-vf", "scale=%d:-2" % width, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", crf, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst])
        if Path(dst).stat().st_size <= 48 * 1024 * 1024:
            return Path(dst)
    return None


# ------------------------------------------------------------------ SHORTS
def run_shorts(topic):
    T = TOPICS[topic]
    apply_topic(T)
    skip = re.compile(T["skip"], re.I)
    name = state_name(topic, "shorts")
    st = load_state(name)
    max_day = int(CFG.get("max_shorts_per_day", 3))
    left = max_day - st["daily"]["count"]
    if left <= 0:
        log("Daily %s Shorts limit (%d) reached - nothing to do." % (topic, max_day))
        return
    items = fetch_items(SHORTS_MAX_AGE_H, T["feeds"])
    picks = rank(cluster(items), st, True, skip)[:min(int(CFG.get("shorts_per_run", 1)), left)]
    if not picks:
        log("No new well-sourced stories right now.")
        save_state(name, st)
        return

    for c in picks:
        its, text = story_text(c)
        log("Story from %d outlets: %s" % (len(c["sources"]), its[0]["title"]))
        user = "SOURCE MATERIAL:\nSOURCE TYPE: the same %s story reported by %d outlets in the last %d hours\n%s" % (
            T["label"].lower(), len(its), SHORTS_MAX_AGE_H, text)
        d = call_gemini(PROMPTS[T["prompt_shorts"]], user, temperature=0.5)
        script = str(d.get("script") or "").strip()
        words = len(script.split())
        if d.get("skip") or words < 30 or words > 190:
            log("Gemini skipped this story (skip flag or length %d words)." % words)
            mark_seen(st, c)
            save_state(name, st)
            continue

        line = T.get("subscribe_shorts", "")
        if line and "subscribe" not in script.lower():
            script = script + " " + line
        names = [i["source"] for i in its]
        links = [i["link"] for i in its[:3]]
        sid = "n" + hashlib.md5(its[0]["link"].encode()).hexdigest()[:8]
        payload = {
            "id": topic + sid, "script": script,
            "search_terms": (d.get("search_terms") or [])[:5],
            "image_prompts": (d.get("image_prompts") or [])[:6],
            "hook": str(d.get("hook") or "")[:90],
            "source": "Sources: " + " • ".join(names[:3]),
            "clip_url": "",
        }
        result = shorts.render(payload)
        title = str(d.get("title") or its[0]["title"])[:95]
        tags = ",".join((d.get("tags") or [])[:10])
        description = "%s\n\nReported by: %s\n%s\n\n%s" % (
            d.get("description") or "", ", ".join(names), "\n".join(links), T["hashtags_shorts"])

        tg_video(result["video"], "🎬 [%s] %s" % (T["label"], title), 1080, 1920)
        tg_message("📌 %s\n\nTITLE:\n%s\n\nDESCRIPTION:\n%s\n\nTAGS:\n%s\n\nCHECK THE FACTS BEFORE POSTING:\n%s\n\nSCRIPT:\n%s"
                   % (T["label"].upper(), title, description, tags, "\n".join(links), script))
        log("Sent to Telegram.")
        mark_seen(st, c)
        st["daily"]["count"] += 1
        save_state(name, st)


# ------------------------------------------------------------------ DAILY VIDEO
def run_daily(topic):
    T = TOPICS[topic]
    apply_topic(T)
    skip = re.compile(T["skip"], re.I)
    name = state_name(topic, "daily")
    st = load_state(name)
    today = datetime.date.today()
    date_label = "%s %d" % (today.strftime("%A, %B"), today.day)
    ranked = rank(cluster(fetch_items(DAILY_MAX_AGE_H, T["feeds"])), st, False, skip)[:DAILY_STORIES]
    if len(ranked) < 3:
        raise RuntimeError("Only %d well-sourced stories found - not enough for a video." % len(ranked))

    stories = []
    for n, c in enumerate(ranked, 1):
        its, text = story_text(c)
        stories.append({"index": n, "its": its, "text": text, "cluster": c})
    user = "DATE: %s\n\n" % date_label + "\n\n".join(
        "STORY %d (%d outlets):\n%s" % (s["index"], len(s["its"]), s["text"]) for s in stories)
    d = call_gemini(PROMPTS[T["prompt_daily"]], user, max_tokens=16000)

    segments, used = [], []
    for s in d.get("stories") or []:
        src = next((x for x in stories if x["index"] == s.get("index")), None)
        narration = str(s.get("narration") or "").strip()
        if not src or s.get("skip") or len(narration.split()) < 40:
            continue
        names = [i["source"] for i in src["its"]]
        segments.append({"kind": "story", "headline": str(s.get("headline") or "")[:70], "text": narration,
                         "sources": names, "search_terms": (s.get("search_terms") or [])[:4],
                         "image_prompts": (s.get("image_prompts") or [])[:3]})
        used.append((str(s.get("headline") or ""), [i["link"] for i in src["its"][:3]], src["cluster"]))
    if len(used) < 3:
        raise RuntimeError("Only %d usable stories after Gemini's checks." % len(used))
    if not d.get("intro") or not d.get("outro"):
        raise RuntimeError("Gemini did not return an intro and outro.")
    segments.insert(0, {"kind": "intro", "text": str(d["intro"])})
    outro = str(d["outro"])
    line = T.get("subscribe_daily", "")
    if line and "subscribe" not in outro.lower():
        outro = outro + " " + line
    segments.append({"kind": "outro", "text": outro})

    title = str(d.get("title") or T["default_title"])[:95]
    payload = {"id": T["id_prefix"] + today.strftime("%Y%m%d"), "title": title, "date_label": date_label,
               "thumbnail_text": str(d.get("thumbnail_text") or title)[:60], "segments": segments}
    result = shorts.render_roundup(payload)

    sources_block = "STORIES AND SOURCES:\n" + "\n\n".join(
        "%d. %s\n%s" % (i, h, "\n".join(links)) for i, (h, links, _) in enumerate(used, 1))
    description = "%s\n\nCHAPTERS\n%s\n\n%s\n\n%s" % (
        d.get("description") or "", result["chapters"], sources_block, T["hashtags_daily"])
    tags = ",".join((d.get("tags") or [])[:12])
    mins = round(result["duration"] / 6) / 10.0

    if result.get("thumbnail"):
        tg_photo(result["thumbnail"], "[%s] New daily video ready (%s min)\n%s" % (T["label"], mins, title))
    tg_message("📌 %s\n\nTITLE:\n%s\n\nDESCRIPTION (paste into YouTube):\n%s\n\nTAGS:\n%s"
               % (T["label"].upper(), title, description, tags))
    copy = telegram_copy(result["video"], str(ROOT / "output" / ("telegram_copy_%s.mp4" % topic)))
    if copy:
        tg_video(copy, "🎬 [%s] %s" % (T["label"], title), 1280, 720)
        log("Video copy sent to Telegram.")
    else:
        tg_message("The %s video was too large for Telegram. Download the full video from the run page on GitHub "
                   "(Actions tab, open today's run, Artifacts)." % T["label"])
    for _, _, c in used:
        mark_seen(st, c)
    save_state(name, st)


def main():
    args = sys.argv[1:]
    mode = args[0] if args else ""
    topic = "world"
    if "--topic" in args:
        i = args.index("--topic")
        topic = args[i + 1] if i + 1 < len(args) else ""
    if mode not in ("shorts", "daily") or topic not in TOPICS:
        print(__doc__)
        print("Topics available:", ", ".join(TOPICS))
        sys.exit(2)
    try:
        run_shorts(topic) if mode == "shorts" else run_daily(topic)
    except Exception as e:
        log("ERROR:", e)
        try:
            tg_message("⚠️ The %s %s bot hit a problem:\n%s" % (TOPICS[topic]["label"], mode, str(e)[:900]))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
