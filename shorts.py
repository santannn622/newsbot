#!/usr/bin/env python3
"""
shorts.py - turns a script into a finished vertical YouTube Short using free tools only.

  python3 shorts.py render --b64 <base64 json>   (this is what n8n calls)
  python3 shorts.py render --file story.json
  python3 shorts.py test                         (renders a sample so you can check your setup)

Payload JSON: {"id": "abc123", "script": "...", "search_terms": ["gaming setup neon", ...]}
Prints ONE json line on stdout when done: {"video": "/path/to/file.mp4", "duration": 41.2}
All progress logging goes to stderr.

Needs: python 3.9+, ffmpeg (5+), pip install edge-tts faster-whisper
Real clips: pip install yt-dlp (downloads the clip linked in the Reddit post)
Extra footage: Pexels key OR Pixabay key (free) OR your own clips in ./footage (any mix)
"""
import argparse
import asyncio
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
WORK_ROOT = ROOT / "work"
MUSIC_DIR = ROOT / "music"  # drop royalty-free .mp3 files here (optional)
FOOTAGE_DIR = ROOT / "footage"  # drop your own .mp4 background clips here (optional)


def load_config():
    p = ROOT / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


CFG = load_config()
PEXELS_KEY = CFG.get("pexels_api_key") or os.environ.get("PEXELS_API_KEY", "")
PIXABAY_KEY = CFG.get("pixabay_api_key") or os.environ.get("PIXABAY_API_KEY", "")
VOICE = CFG.get("voice", "en-US-AndrewNeural")
RATE = CFG.get("voice_rate", "+8%")
WHISPER_MODEL = CFG.get("whisper_model", "base.en")
FONT = CFG.get("caption_font", "Arial")
FONT_SIZE = int(CFG.get("caption_font_size", 96))
MUSIC_VOLUME = float(CFG.get("music_volume", 0.07))
KEEP_WORK = bool(CFG.get("keep_work_files", False))
AI_IMAGES = bool(CFG.get("ai_images", True))
IMAGE_STYLE = CFG.get("image_style", "clean editorial illustration, flat vector style, blue and teal palette, abstract and symbolic, no text, no logos, no people")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
PIXABAY_SIZE = CFG.get("pixabay_size", "medium")
BADGE_LABEL = CFG.get("badge_label", "WORLD NEWS")
REAL_CLIPS = bool(CFG.get("real_clips", True))
MAX_CLIP_SECONDS = float(CFG.get("max_clip_seconds", 20))   # keep short: safer for fair use
CLIP_SKIP = float(CFG.get("clip_skip_seconds", 0))          # start this far into the clip
CLIP_VOLUME = float(CFG.get("clip_audio_volume", 0.6))
W, H, FPS = 1080, 1920, 30


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("Command failed: %s\n%s" % (" ".join(map(str, cmd)), p.stderr[-1500:]))
    return p.stdout


# ---------------------------------------------------------------- text / voice
def clean_script(t):
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)  # emoji
    t = re.sub(r"[*#_`~\[\]{}<>|\\]", "", t)
    return re.sub(r"\s+", " ", t).strip()


async def _tts(text, path):
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))


def tts(text, path):
    try:
        asyncio.run(_tts(text, path))
    except Exception as e:  # Edge TTS can be blocked on some servers: fall back to gTTS
        log("Edge TTS failed (%s) - trying the gTTS fallback" % str(e)[:120])
        from gtts import gTTS

        gTTS(text, lang="en").save(str(path))


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


# ---------------------------------------------------------------- captions
def transcribe_words(audio):
    """Returns [(start, end, word), ...] using faster-whisper (runs locally, free)."""
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), word_timestamps=True, language="en")
    words = []
    for seg in segments:
        for w in (seg.words or []):
            txt = w.word.strip()
            if txt:
                words.append((w.start, w.end, txt))
    return words


def _ts(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: %(w)d
PlayResY: %(h)d
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,%(font)s,%(size)d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,8,2,2,70,70,430,1
Style: Hook,%(font)s,76,&H00FFFFFF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,12,0,8,90,90,330,1
Style: Chip,%(font)s,44,&H0000D7FF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,14,0,8,60,60,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HILITE = r"{\1c&H00FFFF&\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"


def _esc(t):
    return t.replace("{", "").replace("}", "").replace("\\", "")


def make_ass(words, path, total, hook="", source=""):
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 3 or w[2][-1] in ".?!,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    lines = [ASS_HEADER % {"w": W, "h": H, "font": FONT, "size": FONT_SIZE}]

    # word-by-word highlighted captions (current word pops in yellow)
    for gi, g in enumerate(groups):
        group_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else min(g[-1][1] + 0.3, total)
        for k, w in enumerate(g):
            start = w[0]
            end = g[k + 1][0] if k + 1 < len(g) else group_end
            end = max(end, start + 0.12)
            parts = []
            for j, x in enumerate(g):
                t = _esc(x[2]).upper()
                parts.append(HILITE + t + r"{\r}" if j == k else t)
            lines.append("Dialogue: 1,%s,%s,Default,,0,0,0,,%s" % (_ts(start), _ts(end), " ".join(parts)))

    # headline card for the first seconds
    if hook:
        hook_end = min(3.2, total * 0.45)
        lines.append("Dialogue: 2,%s,%s,Hook,,0,0,0,,{\\fad(120,200)}%s" % (_ts(0), _ts(hook_end), _esc(hook).upper()))

    # source chip (credit) for the whole video
    if source:
        lines.append("Dialogue: 2,%s,%s,Chip,,0,0,0,,%s" % (_ts(0), _ts(total), _esc(source)))

    # progress bar along the bottom edge
    ms = int(total * 1000)
    lines.append("Dialogue: 3,%s,%s,Default,,0,0,0,,{\\an7\\pos(0,%d)\\p1\\bord0\\shad0\\1c&H00D7FF&\\fscx1\\t(0,%d,\\fscx100)}m 0 0 l %d 0 %d 14 0 14{\\p0}"
                 % (_ts(0), _ts(total), H - 14, ms, W, W))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- footage
def pexels_search(query, n=8, orientation="portrait"):
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "orientation": orientation, "size": "medium", "per_page": n})
    req = urllib.request.Request(url, headers={"Authorization": PEXELS_KEY, "User-Agent": "shorts-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("videos", [])


def pick_file(video, portrait=True):
    files = [f for f in video.get("video_files", [])
             if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")]
    if portrait:
        pool = [f for f in files if f["height"] > f["width"]] or files
    else:
        pool = [f for f in files if f["width"] >= f["height"]] or files
    pool.sort(key=lambda f: abs(f["width"] - (1080 if portrait else 1920)))
    return pool[0]["link"] if pool else None


def download(url, dest, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "shorts-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def pixabay_search(query, n=10):
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
        {"key": PIXABAY_KEY, "q": query, "per_page": n, "safesearch": "true"})
    req = urllib.request.Request(url, headers={"User-Agent": "shorts-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        hits = json.load(r).get("hits", [])
    out = []
    for h in hits:
        vids = h.get("videos", {})
        for size in (PIXABAY_SIZE, "medium", "large", "small"):
            if vids.get(size, {}).get("url"):
                out.append({"id": "px%s" % h["id"], "link": vids[size]["url"]})
                break
    return out


def search_stock(term, orientation="portrait"):
    """Returns [{'id':..., 'link':...}] from Pexels, falling back to Pixabay."""
    results = []
    if PEXELS_KEY:
        try:
            for v in pexels_search(term, orientation=orientation):
                link = pick_file(v, portrait=(orientation == "portrait"))
                if link:
                    results.append({"id": "pe%s" % v["id"], "link": link})
        except Exception as e:
            log("Pexels error:", e)
    if not results and PIXABAY_KEY:
        try:
            results = pixabay_search(term)
        except Exception as e:
            log("Pixabay error:", e)
    return results


def local_footage():
    if not FOOTAGE_DIR.exists():
        return []
    return [p for p in FOOTAGE_DIR.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]


def fetch_sources(terms, n, workdir, orientation="portrait", use_local=True, prefix="src"):
    paths = []
    if PEXELS_KEY or PIXABAY_KEY:
        terms = [t for t in terms if t] or ["gaming setup", "neon lights", "city night"]
        used, tries, i = set(), 0, 0
        while len(paths) < n and tries < n * 3:
            term = terms[i % len(terms)]
            i += 1
            tries += 1
            vids = search_stock(term, orientation)
            random.shuffle(vids)
            for v in vids:
                if v["id"] in used:
                    continue
                dest = workdir / ("%s_%02d.mp4" % (prefix, len(paths)))
                try:
                    download(v["link"], dest)
                except Exception as e:
                    log("Download failed:", e)
                    continue
                used.add(v["id"])
                paths.append(dest)
                break
    if use_local:
        local = local_footage()
        random.shuffle(local)
        paths += local[:n]  # your own clips get mixed in
    random.shuffle(paths)
    if not paths:
        log("No footage found (no API key, empty footage folder) - using plain background")
    return paths


def fetch_ai_images(prompts, workdir, limit=6, w=None, h=None, prefix="ai"):
    """Free AI scene images from Pollinations.ai (no key). Failures are skipped silently."""
    if not AI_IMAGES or not prompts:
        return []
    out = []
    for i, p in enumerate(prompts[:limit]):
        full = "%s, %s" % (re.sub(r"[^\w\s,'-]", "", str(p))[:220], IMAGE_STYLE)
        q = urllib.parse.urlencode({"width": w or W, "height": h or H, "nologo": "true",
                                    "seed": random.randint(1, 99999), "model": "flux"})
        url = "https://image.pollinations.ai/prompt/%s?%s" % (urllib.parse.quote(full), q)
        dest = workdir / ("%s_%02d.jpg" % (prefix, i))
        for _ in range(2):
            try:
                download(url, dest, timeout=120)
                if dest.stat().st_size > 20000:
                    out.append(dest)
                    break
            except Exception as e:
                log("AI image failed:", e)
                time.sleep(4)
    return out


def interleave(a, b):
    out = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


# ---------------------------------------------------------------- video
def render_clip(src, seg, out, flip=False, start=0.0, w=None, h=None):
    w, h = w or W, h or H
    if src is None:  # fallback: dark gradient-ish solid so the pipeline never dies
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x14142b:s=%dx%d:r=%d" % (w, h, FPS),
             "-t", "%.2f" % seg, "-c:v", "libx264", "-pix_fmt", "yuv420p", out])
        return
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=%d,setsar=1,"
          "eq=brightness=-0.06" % (w, h, w, h, FPS))
    if flip:
        vf += ",hflip"
    vf += ",format=yuv420p"
    pre = ["-ss", "%.2f" % start] if start > 0 else []
    run(["ffmpeg", "-y", "-stream_loop", "-1"] + pre + ["-i", src, "-t", "%.2f" % seg, "-vf", vf,
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", out])


def render_image_clip(src, seg, out, zoom_in=True, w=None, h=None):
    w, h = w or W, h or H
    frames = int(seg * FPS) + 2
    z = "min(1+0.0016*on,1.35)" if zoom_in else "max(1.35-0.0016*on,1)"
    bw, bh = int(w * 1.5), int(h * 1.5)
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
          "zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d,format=yuv420p"
          % (bw, bh, bw, bh, z, frames, w, h, FPS))
    run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-t", "%.2f" % seg, "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", out])


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
               "-of", "csv=p=0", path])
    return bool(out.strip())


def probe_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
               "-of", "csv=p=0", path])
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def download_clip(url, workdir):
    """Downloads the real clip with yt-dlp (pip install yt-dlp). Returns a Path or None."""
    if not REAL_CLIPS or not url:
        return None
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--max-filesize", "150M",
                        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                        "-o", str(workdir / "orig.%(ext)s"), url],
                       capture_output=True, text=True, timeout=240, check=True)
        files = sorted(workdir.glob("orig.*"))
        return files[0] if files else None
    except subprocess.CalledProcessError as e:
        log("Clip download failed:", (e.stderr or "")[-300:])
    except Exception as e:
        log("Clip download failed:", str(e)[:200])
    return None


def render_real_clip(src, seconds, out_video, out_audio):
    """Vertical layout: the real clip centered over a blurred, darkened copy of itself."""
    w, h = probe_size(src)
    if h > w:  # already vertical: just fill the frame
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=%d,setsar=1,format=yuv420p"
              % (W, H, W, H, FPS))
    else:
        vf = ("split[a][b];[a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
              "boxblur=40:6,eq=brightness=-0.15[bg];[b]scale=%d:-2[fg];"
              "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=%d,setsar=1,format=yuv420p" % (W, H, W, H, W, FPS))
    ss = ["-ss", "%.2f" % CLIP_SKIP] if CLIP_SKIP > 0 else []
    run(["ffmpeg", "-y"] + ss + ["-i", src, "-t", "%.2f" % seconds, "-filter_complex", vf,
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", out_video])
    if has_audio(src):
        run(["ffmpeg", "-y"] + ss + ["-i", src, "-t", "%.2f" % seconds, "-vn",
             "-c:a", "aac", "-b:a", "128k", out_audio])
        return True
    return False


def pick_music():
    if not MUSIC_DIR.exists():
        return None
    tracks = [p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")]
    return random.choice(tracks) if tracks else None


def render(payload):
    sid = re.sub(r"\W", "", str(payload.get("id", "short"))) or "short"
    script = clean_script(payload["script"])
    terms = payload.get("search_terms") or []
    hook = clean_script(payload.get("hook", "")) or re.split(r"(?<=[.?!])\s", script)[0]
    source = clean_script(payload.get("source", ""))
    stamp = int(time.time())
    work = WORK_ROOT / ("%s_%d" % (sid, stamp))
    work.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)

    log("1/6 voiceover")
    voice = work / "voice.mp3"
    tts(script, voice)
    dur = probe_duration(voice)
    if dur > 58:
        log("WARNING: voiceover is %.0fs - Shorts work best under 60s" % dur)
    total = dur + 0.4

    log("2/6 captions")
    words = transcribe_words(voice)
    make_ass(words, work / "captions.ass", total, hook, source)

    log("3/6 real clip")
    rc, real_audio = 0.0, False
    clip_src = download_clip(payload.get("clip_url"), work)
    if clip_src:
        try:
            rc = min(MAX_CLIP_SECONDS, probe_duration(clip_src) - CLIP_SKIP, total)
            if rc >= 2:
                if total - rc < 0.8:
                    rc = total
                real_audio = render_real_clip(clip_src, rc, work / "real.mp4", work / "real_audio.m4a")
                log("   using %.1fs of the real clip" % rc)
            else:
                rc = 0.0
        except Exception as e:
            log("Real clip failed (%s) - continuing without it" % str(e)[:150])
            rc, real_audio = 0.0, False
    rest = total - rc

    clip_names = ["real.mp4"] if rc > 0 else []
    n = max(1, min(14, round(rest / 3.5))) if rest > 0.8 else 0
    if n:
        log("4/6 extra footage")
        videos = fetch_sources(terms, n, work)
        images = fetch_ai_images(payload.get("image_prompts") or [], work)
        log("   %d stock/local clips, %d AI images" % (len(videos), len(images)))
        sources = interleave(images, videos)
        seg = rest / n
        for i in range(n):
            src = sources[i % len(sources)] if sources else None
            name = "clip_%02d.mp4" % i
            reused = bool(sources) and i >= len(sources)
            try:
                if src is not None and src.suffix.lower() in IMG_EXT:
                    render_image_clip(src, seg, work / name, zoom_in=(i % 2 == 0))
                else:
                    start = 0.0
                    if src is not None:
                        try:
                            start = random.uniform(0, max(0.0, probe_duration(src) - seg - 0.5))
                        except Exception:
                            start = 0.0
                    render_clip(src, seg, work / name, start=start,
                                flip=reused and (i // len(sources)) % 2 == 1)
            except Exception as e:
                log("clip %d failed (%s) - using plain background" % (i, str(e)[:120]))
                render_clip(None, seg, work / name)
            clip_names.append(name)
    (work / "list.txt").write_text("".join("file '%s'\n" % c for c in clip_names), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "video.mp4"], cwd=work)

    log("5/6 final mix")
    music = pick_music()
    out = OUT_DIR / ("%s_%d.mp4" % (sid, stamp))
    cmd = ["ffmpeg", "-y", "-i", "video.mp4", "-i", "voice.mp3"]
    fc = ["[0:v]ass=captions.ass[v]"]
    if real_audio:
        # the clip's own audio plays, but ducks automatically whenever the narrator speaks
        cmd += ["-i", "real_audio.m4a"]
        fc += ["[1:a]asplit=2[vo1][vo2]",
               "[2:a]volume=%.2f,apad[ca]" % CLIP_VOLUME,
               "[ca][vo2]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=400[cad]"]
        mix, music_idx = ["[vo1]", "[cad]"], 3
    else:
        mix, music_idx = ["[1:a]"], 2
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fc.append("[%d:a]volume=%.3f[m]" % (music_idx, MUSIC_VOLUME))
        mix.append("[m]")
    if len(mix) > 1:
        fc.append("%samix=inputs=%d:duration=first:normalize=0[a]" % ("".join(mix), len(mix)))
        amap = "[a]"
    else:
        amap = "1:a"
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", amap, "-t", "%.2f" % total,
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
    log("6/6 encoding")
    run(cmd, cwd=work)

    if not KEEP_WORK:
        shutil.rmtree(work, ignore_errors=True)
    return {"video": str(out), "duration": round(dur, 1), "real_clip_seconds": round(rc, 1)}


# ---------------------------------------------------------------- roundup (full-length video)
RW, RH = [int(x) for x in str(CFG.get("roundup_resolution", "1920x1080")).lower().split("x")]
SHOT_SECONDS = float(CFG.get("roundup_shot_seconds", 5.5))
STOCK_PER_STORY = int(CFG.get("roundup_stock_per_story", 2))
AI_PER_STORY = int(CFG.get("roundup_ai_images_per_story", 2))
ROUNDUP_PRESET = CFG.get("roundup_preset", "veryfast")
ROUNDUP_MUSIC = float(CFG.get("roundup_music_volume", 0.05))
GAP = 0.6
GENERIC_TERMS = ["world map", "city skyline night", "newsroom", "globe network"]
GENERIC_PROMPTS = [
    "a glowing world map with connected lines of light",
    "an abstract globe surrounded by flowing data streams",
    "a calm city skyline at dusk, flat illustration",
    "an empty modern newsroom desk with soft blue light",
]

RASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: %(w)d
PlayResY: %(h)d
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,%(font)s,%(cap)d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,%(side)d,%(side)d,%(mv)d,1
Style: LT,%(font)s,%(lt)d,&H00FFFFFF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,10,0,1,%(ml)d,60,%(ltv)d,1
Style: Chip,%(font)s,%(chip)d,&H0000D7FF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,8,0,7,%(ml)d,60,%(cv)d,1
Style: Title,%(font)s,%(tt)d,&H00FFFFFF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,16,0,5,%(tm)d,%(tm)d,0,1
Style: Sub,%(font)s,%(st)d,&H0000D7FF,&H000000FF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,10,0,5,%(tm)d,%(tm)d,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def mmss(t):
    t = int(t)
    return "%d:%02d" % (t // 60, t % 60)


def caption_events(words, total, per=5):
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= per or w[2][-1] in ".?!,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    ev = []
    for gi, g in enumerate(groups):
        group_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else min(g[-1][1] + 0.3, total)
        for k, w in enumerate(g):
            start = w[0]
            end = g[k + 1][0] if k + 1 < len(g) else group_end
            end = max(end, start + 0.12)
            parts = []
            for j, x in enumerate(g):
                t = _esc(x[2]).upper()
                parts.append(HILITE + t + r"{\r}" if j == k else t)
            ev.append("Dialogue: 1,%s,%s,Default,,0,0,0,,%s" % (_ts(start), _ts(end), " ".join(parts)))
    return ev


def make_roundup_ass(words, path, total, segs, starts, lens, title, date_label):
    s = RH / 1080.0
    d = {"w": RW, "h": RH, "font": FONT, "cap": int(64 * s), "side": int(160 * s), "mv": int(70 * s),
         "lt": int(46 * s), "ml": int(70 * s), "ltv": int(250 * s), "chip": int(36 * s), "cv": int(50 * s),
         "tt": int(92 * s), "st": int(48 * s), "tm": int(220 * s)}
    lines = [RASS_HEADER % d]
    lines += caption_events(words, total)

    n = 0
    for i, sg in enumerate(segs):
        t0, t1 = starts[i], starts[i] + lens[i]
        if sg.get("kind") == "story":
            n += 1
            head = _esc(sg.get("headline", "")).upper()
            lines.append("Dialogue: 2,%s,%s,LT,,0,0,0,,{\\fad(200,250)}%d. %s"
                         % (_ts(t0 + 0.3), _ts(min(t0 + 9.0, t1)), n, head))
            src = _esc(" • ".join((sg.get("sources") or [])[:3]))
            if src:
                lines.append("Dialogue: 2,%s,%s,Chip,,0,0,0,,Sources: %s" % (_ts(t0), _ts(t1), src))
        elif sg.get("kind") == "intro":
            end = min(5.0, t1 * 0.9)
            lines.append("Dialogue: 3,%s,%s,Title,,0,0,0,,{\\an5\\pos(%d,%d)\\fad(250,400)}%s"
                         % (_ts(0.2), _ts(end), RW // 2, int(RH * 0.42), _esc(title).upper()))
            if date_label:
                lines.append("Dialogue: 3,%s,%s,Sub,,0,0,0,,{\\an5\\pos(%d,%d)\\fad(250,400)}%s"
                             % (_ts(0.6), _ts(end), RW // 2, int(RH * 0.68), _esc(date_label).upper()))

    ms = int(total * 1000)
    bar = max(6, int(10 * s))
    lines.append("Dialogue: 4,%s,%s,Default,,0,0,0,,{\\an7\\pos(0,%d)\\p1\\bord0\\shad0\\1c&H00D7FF&\\fscx1\\t(0,%d,\\fscx100)}m 0 0 l %d 0 %d %d 0 %d{\\p0}"
                 % (_ts(0), _ts(total), RH - bar, ms, RW, RW, bar, bar))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_thumbnail(path, title, date_label, bg, work):
    tw, th = 1280, 720
    ass = work / "thumb.ass"
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: T,%s,104,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,3,5,90,90,0,1\n"
            "Style: B,%s,46,&H00000000,&H000000FF,&H0000D7FF,&H0000D7FF,-1,0,0,0,100,100,0,0,3,12,0,7,60,60,50,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n") % (tw, th, FONT, FONT)
    badge = BADGE_LABEL + (" • " + date_label.upper() if date_label else "")
    body = ("Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,%s\nDialogue: 0,0:00:00.00,0:00:05.00,B,,0,0,0,,%s\n"
            % (_esc(title).upper(), _esc(badge)))
    ass.write_text(head + body, encoding="utf-8")
    if bg is not None:
        inp = ["-i", str(bg)]
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,eq=brightness=-0.28,ass=thumb.ass"
              % (tw, th, tw, th))
    else:
        inp = ["-f", "lavfi", "-i", "color=c=0x0b2a4a:s=%dx%d:d=1" % (tw, th)]
        vf = "ass=thumb.ass"
    run(["ffmpeg", "-y"] + inp + ["-vf", vf, "-frames:v", "1", "-q:v", "3", str(path)], cwd=work)


def render_roundup(payload):
    sid = re.sub(r"\W", "", str(payload.get("id", "roundup"))) or "roundup"
    stamp = int(time.time())
    work = WORK_ROOT / ("%s_%d" % (sid, stamp))
    work.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    segs = payload["segments"]
    title = clean_script(payload.get("title", "World News Update"))
    date_label = clean_script(payload.get("date_label", ""))

    # 1) voiceover, one file per part so we know exactly when each story starts
    log("1/6 voiceover (%d parts)" % len(segs))
    files, durs, lens = [], [], []
    for i, sg in enumerate(segs):
        f = work / ("seg_%02d.mp3" % i)
        tts(clean_script(sg["text"]), f)
        d = probe_duration(f)
        files.append(f.name)
        durs.append(d)
        lens.append(d + (0.4 if i == len(segs) - 1 else GAP))
    cmd = ["ffmpeg", "-y"]
    for n in files:
        cmd += ["-i", n]
    fc = ["[%d:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=pad_dur=%.2f[a%d]"
          % (i, lens[i] - durs[i], i) for i in range(len(files))]
    fc.append("%sconcat=n=%d:v=0:a=1[aout]" % ("".join("[a%d]" % i for i in range(len(files))), len(files)))
    cmd += ["-filter_complex", ";".join(fc), "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "voice.m4a"]
    run(cmd, cwd=work)
    total = probe_duration(work / "voice.m4a")
    starts = [sum(lens[:i]) for i in range(len(lens))]
    log("   total length %s" % mmss(total))

    # 2) captions
    log("2/6 captions (this is the slowest step on older laptops)")
    words = transcribe_words(work / "voice.m4a")
    make_roundup_ass(words, work / "captions.ass", total, segs, starts, lens, title, date_label)

    # 3) visuals per part
    log("3/6 footage and images")
    shot_names, all_pool, first_image = [], [], None
    for i, sg in enumerate(segs):
        is_story = sg.get("kind") == "story"
        terms = sg.get("search_terms") or GENERIC_TERMS
        vids = fetch_sources(terms, STOCK_PER_STORY, work, orientation="landscape", use_local=False,
                             prefix="s%02d" % i)
        prompts = (sg.get("image_prompts") or [])[:AI_PER_STORY]
        if not prompts and not is_story:
            prompts = [GENERIC_PROMPTS[i % len(GENERIC_PROMPTS)]]
        imgs = fetch_ai_images(prompts, work, limit=AI_PER_STORY, w=1280, h=720, prefix="a%02d" % i)
        if imgs and first_image is None:
            first_image = imgs[0]
        pool = interleave(imgs, vids) or list(all_pool)
        all_pool += [p for p in pool if p not in all_pool]
        log("   part %d/%d: %d stock, %d images" % (i + 1, len(segs), len(vids), len(imgs)))

        n = max(1, int(round(lens[i] / SHOT_SECONDS)))
        shot = lens[i] / n
        for k in range(n):
            src = pool[k % len(pool)] if pool else None
            name = "shot_%02d_%02d.mp4" % (i, k)
            try:
                if src is not None and src.suffix.lower() in IMG_EXT:
                    render_image_clip(src, shot, work / name, zoom_in=(k % 2 == 0), w=RW, h=RH)
                else:
                    start = 0.0
                    if src is not None:
                        try:
                            start = random.uniform(0, max(0.0, probe_duration(src) - shot - 0.5))
                        except Exception:
                            start = 0.0
                    flip = bool(pool) and k >= len(pool) and (k // len(pool)) % 2 == 1
                    render_clip(src, shot, work / name, flip=flip, start=start, w=RW, h=RH)
            except Exception as e:
                log("shot failed (%s) - plain background" % str(e)[:100])
                render_clip(None, shot, work / name, w=RW, h=RH)
            shot_names.append(name)

    log("4/6 joining shots")
    (work / "list.txt").write_text("".join("file '%s'\n" % n for n in shot_names), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "video.mp4"], cwd=work)

    log("5/6 final mix and encode (can take 10+ minutes)")
    music = pick_music()
    out = OUT_DIR / ("%s_%d.mp4" % (sid, stamp))
    cmd = ["ffmpeg", "-y", "-i", "video.mp4", "-i", "voice.m4a"]
    fcs = ["[0:v]ass=captions.ass[v]"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fcs += ["[2:a]volume=%.3f[m]" % ROUNDUP_MUSIC,
                "[1:a][m]amix=inputs=2:duration=first:normalize=0[a]"]
        amap = "[a]"
    else:
        amap = "1:a"
    cmd += ["-filter_complex", ";".join(fcs), "-map", "[v]", "-map", amap, "-t", "%.2f" % total,
            "-c:v", "libx264", "-preset", ROUNDUP_PRESET, "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    run(cmd, cwd=work)

    log("6/6 thumbnail and chapters")
    thumb = OUT_DIR / ("%s_%d_thumb.jpg" % (sid, stamp))
    try:
        make_thumbnail(thumb, payload.get("thumbnail_text") or title, date_label, first_image, work)
    except Exception as e:
        log("thumbnail failed:", str(e)[:150])
        thumb = None
    chapters = []
    for i, sg in enumerate(segs):
        label = {"intro": "Intro", "outro": "Wrap-up"}.get(sg.get("kind")) or str(sg.get("headline", "Story"))[:55]
        chapters.append("%s %s" % (mmss(starts[i]), label))

    if not KEEP_WORK:
        shutil.rmtree(work, ignore_errors=True)
    return {"video": str(out), "thumbnail": str(thumb) if thumb else "", "duration": round(total, 1),
            "chapters": "\n".join(chapters)}


SAMPLE_ROUNDUP = {
    "id": "roundtest",
    "title": "World News Test Edition",
    "date_label": "Test Day",
    "segments": [
        {"kind": "intro", "text": "Welcome to this test of the daily news roundup. Today we check that every part of the pipeline works."},
        {"kind": "story", "headline": "First test story", "sources": ["BBC", "Al Jazeera"],
         "text": "This is the first test story. It is only here to check that the lower third, the source label and the captions line up with the voice. Nothing in this video is real news.",
         "search_terms": ["world map"], "image_prompts": ["a glowing world map with connected lines of light"]},
        {"kind": "story", "headline": "Second test story", "sources": ["NPR", "The Guardian", "DW"],
         "text": "This is the second test story. A new headline card should appear when it starts, and the chapter list should show where each story begins.",
         "search_terms": ["city skyline night"], "image_prompts": ["a calm city skyline at dusk, flat illustration"]},
        {"kind": "outro", "text": "That is the end of the test. If you can see this, the roundup renderer works."},
    ],
}


SAMPLE = {
    "id": "test",
    "script": ("Okay, this is why streamers never sleep. Imagine going live at three in the morning "
               "with eight monitors, four energy drinks, and a chat that will not stop spamming. "
               "That is just a normal Tuesday for a full time streamer. And the wildest part? "
               "People still say it is an easy job. Do you agree?"),
    "search_terms": ["gaming setup neon", "streamer microphone", "computer monitors night"],
}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--b64")
    r.add_argument("--file")
    ru = sub.add_parser("roundup")
    ru.add_argument("--b64")
    ru.add_argument("--file")
    sub.add_parser("test")
    sub.add_parser("roundup-test")
    a = ap.parse_args()

    job_file = None
    if a.cmd == "test":
        payload = SAMPLE
    elif a.cmd == "roundup-test":
        payload = SAMPLE_ROUNDUP
    elif a.b64:
        payload = json.loads(base64.b64decode(a.b64).decode("utf-8"))
    elif a.file:
        job_file = Path(a.file)
        payload = json.loads(job_file.read_text(encoding="utf-8-sig"))
    else:
        ap.error("%s needs --b64 or --file" % a.cmd)

    try:
        is_round = a.cmd in ("roundup", "roundup-test")
        result = render_roundup(payload) if is_round else render(payload)
        print(json.dumps(result))
        if job_file and job_file.name.startswith("job_"):
            try:
                job_file.unlink()
            except Exception:
                pass
    except Exception as e:
        log("ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
