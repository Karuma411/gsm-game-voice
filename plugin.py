"""GSM user plugin: exact game voice for new Anki cards + sentence cleanup.

Runs inside GameSentenceMiner (its user-plugins cron). No extra programs,
no batch files: while GSM is open it keeps card audio in sync with the real
voice files of the game you are playing.

Supported game engines right now:
  * RPG Maker MV/MZ  - data/*.json event commands (Show Text 401 + Play SE 250)
  * Ren'Py           - voice "file.ogg" right before the dialogue line

Games to scan are listed in  %APPDATA%\\GameSentenceMiner\\gv_games.json
(created automatically with the currently known game; add more paths there).

What it does every run:
  1. builds/refreshes the map "dialogue line -> voice file" for each game;
  2. looks at cards added today;
  3. if a card's sentence matches a game line, replaces whatever clipped
     audio it has with the exact voice file (converted to mp3);
  4. removes OCR junk (window labels, speaker name, game title) from `Sentence`.

Nothing is written when the sentence is not found, so no garbage appears.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GameSentenceMiner"
GAMES_FILE = APP_DIR / "gv_games.json"
MAP_FILE = APP_DIR / "gv_map.json"
DUR_FILE = APP_DIR / "gv_durations.json"
WORD_DIRS_FILE = APP_DIR / "gv_word_audio.json"
WORD_INDEX_FILE = APP_DIR / "gv_word_index.json"
TTS_FILE = APP_DIR / "gv_tts.json"
TTS_LOCK = APP_DIR / "gv_tts.lock"
ASR_FILE = APP_DIR / "gv_asr.json"
ASR_CACHE = APP_DIR / "gv_asr_cache.json"
VOICE_ASR = APP_DIR / "gv_voice_asr.json"
LOG_FILE = APP_DIR / "gv_log.txt"
ANKI_URL = "http://127.0.0.1:8765"
MAP_MAX_AGE = 6 * 3600

DEFAULT_GAMES = [
    r"D:\Новая папка (2)\RJ01745923.zKiQp\RJ01745923.KQ\合体攻略！ すけべすごろく",
]

AUDIO_EXTS = (".ogg", ".m4a", ".mp3", ".wav", ".opus", ".flac")

# Tags inside game text that are not spoken (<br> stays: it is a line break).
ESCAPE_RE = re.compile(r"\\(?:[NVCPI]\[\d+\]|[{}.|!><^$\\])")
# Punctuation differs between game data (……) and OCR (・・・); drop it for matching.
MATCH_JUNK_RE = re.compile(r"[、。，．,.！？!?…‥.·・･「」『』（）()\[\]【】〈〉《》〜～ー\s\u3000]+")
# Window/UI labels and stray OCR characters that end up in front of a line.
UI_SEGMENTS = {"会話", "タッチ", "メッセージ", "選択肢", "バックログ", "スキップ", "オート", "メニュー"}
UI_CHARS = set("会話タッチ숲Πメッナ下着ごっこいたずら\n ")
FURI_JUNK = {"会話", "タッチ", "숲", "Π会話", "会話タッチ", "Π会話タッチ", "숲タッチ",
              "でんぶ", "先輩", "メッセージ", "選択肢"}
JUNK_PREFIX = set("会話タッチ숲Π")
TITLE_RE = re.compile(r"合体攻略|すけべすごろく|ｖ１|v1\.0", re.IGNORECASE)


# --------------------------------------------------------------------------- log


def log(message: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


# -------------------------------------------------------------------- anki api


def anki(action: str, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    request = urllib.request.Request(ANKI_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.load(response)
    if body.get("error"):
        raise RuntimeError(f"{action}: {body['error']}")
    return body.get("result")


# ------------------------------------------------------------------- text prep


def normalize(text: str) -> str:
    text = ESCAPE_RE.sub("", text or "")
    text = unicodedata.normalize("NFKC", text).replace("\u3000", " ")
    return re.sub(r"\s+", "", text).strip()


def match_key(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "").split(" — ")[0]
    return MATCH_JUNK_RE.sub("", normalize(text))


def clean_furigana(text: str) -> str:
    """Drop OCR junk (window labels, speaker line, game title) from furigana text."""
    if not text:
        return text
    segments: list[str] = []
    for segment in re.split(r"<br\s*/?>", text):
        plain_all = re.sub(r"\[[^\]]*\]", "", re.sub(r"<[^>]+>", "", segment))
        if not plain_all.strip():
            continue
        if TITLE_RE.search(MATCH_JUNK_RE.sub("", normalize(plain_all))):
            continue
        kept: list[str] = []
        for token in segment.split():
            plain = re.sub(r"\[[^\]]*\]", "", re.sub(r"<[^>]+>", "", token))
            compact = MATCH_JUNK_RE.sub("", normalize(plain))
            if not compact or compact in FURI_JUNK:
                continue
            if plain[:1] and plain[0] in JUNK_PREFIX:
                continue
            if len(compact) <= 4 and set(compact) <= UI_CHARS:
                continue
            kept.append(token)
        if kept:
            segments.append(" ".join(kept))
    return "<br>".join(segments).strip()


def clean_sentence(text: str, speakers: set[str]) -> str:
    """Drop OCR junk segments (UI labels, speaker name, game title)."""
    if not text:
        return text
    segments = re.split(r"<br\s*/?>", text)
    keep: list[str] = []
    for segment in segments:
        plain = re.sub(r"<[^>]+>", "", segment).strip()
        if not plain:
            continue
        compact = MATCH_JUNK_RE.sub("", normalize(plain))
        if compact in speakers:
            continue
        if TITLE_RE.search(plain):
            continue
        if plain in UI_SEGMENTS:
            continue
        if len(plain) <= 12 and set(plain) <= UI_CHARS and not re.search(r"[。！？]", plain):
            continue
        keep.append(plain)
    return "<br>".join(keep)


# ---------------------------------------------------------- rpg maker mv / mz


def walk_lists(obj, out) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "list" and isinstance(value, list):
                out.append(value)
            else:
                walk_lists(value, out)
    elif isinstance(obj, list):
        for item in obj:
            walk_lists(item, out)


def rpgmaker_pairs(game_dir: Path):
    data_dir = game_dir / "data"
    if not data_dir.is_dir():
        return []
    lists: list[list] = []
    for path in sorted(data_dir.glob("*.json")):
        if path.name in {"MapInfos.json", "System.json", "Tilesets.json"}:
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                walk_lists(json.load(fh), lists)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue

    # Some games put `Play SE` BEFORE the message it voices, some AFTER it.
    # We build both readings and let build_map pick the one the durations
    # confirm, so a new game cannot silently get the neighbour's voice.
    blocks: list[tuple[str, str, str]] = []   # (text, speaker, voice)
    for commands in lists:
        speaker = ""
        lines: list[str] = []
        block_voice = ""
        pending_voice = ""

        def flush() -> None:
            if not lines:
                return
            text = "".join(lines).strip()
            if text:
                blocks.append((text, speaker, block_voice))

        for command in commands:
            if not isinstance(command, dict):
                continue
            code = command.get("code")
            params = command.get("parameters") or []
            if code == 250:
                first = params[0] if params else ""
                name = str(first.get("name", "")) if isinstance(first, dict) else str(first)
                if "/" in name:
                    pending_voice = name
            elif code == 101:
                flush()
                lines = []
                block_voice = pending_voice
                pending_voice = ""
                speaker = str(params[4]) if len(params) > 4 else ""
            elif code == 401 and params:
                lines.append(str(params[0]))
        flush()

    after = [(text, voice, speaker) for text, speaker, voice in blocks if voice]
    before = [(blocks[i][0], blocks[i + 1][2], blocks[i][1])
              for i in range(len(blocks) - 1) if blocks[i + 1][2]]
    return {"after": after, "before": before}


def rpgmaker_audio(game_dir: Path, voice: str) -> Path | None:
    base = game_dir / "audio" / "se" / voice
    for ext in AUDIO_EXTS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


# -------------------------------------------------------------------- ren'py


def renpy_pairs(game_dir: Path):
    """Read voice statements from .rpy sources (skip compiled .rpyc)."""
    pairs = []
    voice_re = re.compile(r'^\s*voice\s+"([^"]+)"')
    say_re = re.compile(r'^\s*(?:[A-Za-z_][\w.]*\s+)?"((?:[^"\\]|\\.)*)"\s*$')
    for path in game_dir.rglob("*.rpy"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        pending = None
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            match = voice_re.match(line)
            if match:
                pending = match.group(1)
                continue
            if pending:
                say = say_re.match(line)
                if say:
                    pairs.append((say.group(1), pending, ""))
                    pending = None
    return pairs


def renpy_audio(game_dir: Path, voice: str) -> Path | None:
    direct = game_dir / voice
    if direct.is_file():
        return direct
    name = Path(voice).name
    for candidate in game_dir.rglob(name):
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------ map build


def probe_duration(audio: Path) -> float | None:
    """Length of an audio file in seconds (ffprobe next to ffmpeg, else ffmpeg -i)."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return None
    probe = str(Path(ffmpeg).with_name("ffprobe.exe"))
    if Path(probe).is_file():
        done = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(audio)],
                              capture_output=True, text=True, errors="replace")
        try:
            return float((done.stdout or "").strip())
        except ValueError:
            return None
    done = subprocess.run([ffmpeg, "-hide_banner", "-i", str(audio)],
                          capture_output=True, text=True, errors="replace")
    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", done.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def duration_of(audio: Path, cache: dict) -> float | None:
    key = str(audio)
    stamp = cache.get(key)
    try:
        mtime = int(audio.stat().st_mtime)
    except OSError:
        return None
    if isinstance(stamp, dict) and stamp.get("mtime") == mtime:
        return stamp.get("dur")
    dur = probe_duration(audio)
    if dur:
        cache[key] = {"mtime": mtime, "dur": round(dur, 2)}
    return dur


def speech_ratio_ok(dur: float | None, text_len: int) -> bool:
    """Japanese speech is roughly 0.09-0.45 s per character; outside = sound effect."""
    if not dur or text_len <= 0:
        return False
    ratio = dur / text_len
    return 0.09 <= ratio <= 0.45


def load_durations() -> dict:
    try:
        return json.loads(DUR_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_durations(cache: dict) -> None:
    try:
        DUR_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return 0.0
    return cov / (sx * sy)


def pairing_score(pairs, resolver, game_dir: Path, durations: dict,
                  limit: int = 300) -> float:
    """How well do file lengths follow text lengths for this pairing?"""
    xs: list[float] = []
    ys: list[float] = []
    for text, voice, _speaker in pairs[:limit]:
        audio = resolver(game_dir, voice)
        if audio is None:
            continue
        dur = duration_of(audio, durations)
        if not dur:
            continue
        xs.append(len(match_key(text)))
        ys.append(dur)
    if len(xs) < 15:
        return 0.0
    return pearson(xs, ys)


# ------------------------------------------- speech checks (Whisper, optional)


def asr_config() -> dict:
    if not ASR_FILE.is_file():
        python = Path(default_sovits_root(default_anki_voice_dir())) / "runtime" / "python.exe"
        config = {
            "enabled": True,
            "python": str(python),
            "model": "large-v3-turbo",
            "samples": 8,
            "min_match": 0.5,
        }
        ASR_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
        return config
    try:
        config = json.loads(ASR_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": False}
    return config if isinstance(config, dict) else {"enabled": False}


def load_asr_cache() -> dict:
    try:
        return json.loads(ASR_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_asr_cache(cache: dict) -> None:
    try:
        ASR_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


ASR_SCRIPT = (
    "import sys, json\n"
    "from faster_whisper import WhisperModel\n"
    "mid = sys.argv[1]\n"
    "try:\n"
    "    model = WhisperModel(mid, device='cuda', compute_type='float16')\n"
    "except Exception:\n"
    "    model = WhisperModel(mid, device='cpu', compute_type='int8')\n"
    "out = {}\n"
    "for p in sys.argv[2:]:\n"
    "    segs, _ = model.transcribe(p, language='ja', beam_size=1, vad_filter=False,\n"
    "                              condition_on_previous_text=False)\n"
    "    out[p] = ''.join(s.text for s in segs)\n"
    "print('ASRJSON' + json.dumps(out, ensure_ascii=False))\n"
)


def asr_transcribe(paths: list[Path], config: dict) -> dict:
    """Transcribe audio files with faster-whisper in the engine python."""
    python = Path(str(config.get("python") or ""))
    if not python.is_file() or not paths:
        return {}
    command = [str(python), "-c", ASR_SCRIPT, str(config.get("model") or "large-v3-turbo")]
    command += [str(p) for p in paths]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"asr failed: {exc}")
        return {}
    for line in (done.stdout or "").splitlines():
        if line.startswith("ASRJSON"):
            try:
                return json.loads(line[len("ASRJSON"):])
            except json.JSONDecodeError:
                return {}
    tail = [l for l in (done.stderr or "").splitlines() if l.strip()][-1:]
    log("asr: no output " + " | ".join(tail))
    return {}


def asr_similarity(transcribed: str, expected: str) -> float:
    left, right = match_key(transcribed), match_key(expected)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def asr_verify(pairs, resolver, game_dir: Path, config: dict) -> tuple[float, int]:
    """Listen to a sample of voice files and compare with the text they claim."""
    samples = int(config.get("samples") or 8)
    if samples <= 0 or not pairs:
        return 0.0, 0
    step = max(1, len(pairs) // samples)
    picked = []
    for text, voice, _speaker in pairs[::step][:samples]:
        audio = resolver(game_dir, voice)
        if audio is not None:
            picked.append((text, audio))
    if len(picked) < 3:
        return 0.0, 0
    model = str(config.get("model") or "large-v3-turbo")
    cache = load_asr_cache()
    texts: dict[str, str] = {}
    need: list[Path] = []
    for _text, audio in picked:
        key = f"{model}|{audio}"
        item = cache.get(key)
        try:
            mtime = int(audio.stat().st_mtime)
        except OSError:
            mtime = 0
        if isinstance(item, dict) and item.get("mtime") == mtime and item.get("text"):
            texts[str(audio)] = item["text"]
        else:
            need.append(audio)
    if need:
        for path, text in asr_transcribe(need, config).items():
            audio = Path(path)
            try:
                mtime = int(audio.stat().st_mtime)
            except OSError:
                mtime = 0
            cache[f"{model}|{audio}"] = {"mtime": mtime, "text": text}
            texts[str(audio)] = text
        save_asr_cache(cache)
    scores = [asr_similarity(texts.get(str(audio), ""), text) for text, audio in picked]
    scores = [s for s in scores if s > 0]
    if not scores:
        return 0.0, 0
    return sum(scores) / len(scores), len(scores)


def voice_asr_index() -> dict:
    try:
        data = json.loads(VOICE_ASR.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_voice_asr_index(data: dict) -> None:
    try:
        VOICE_ASR.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def asr_scan(game_dir: Path, known: set[Path], config: dict) -> list[dict]:
    """Listen to voice files that no message event pointed at.

    Some games play a line's voice from a plugin call or a common event, so no
    `Play SE` sits next to the text and the file would stay unused forever.
    We transcribe those leftovers once (a budget per run) and index what they
    actually say, so the line can still be matched later.
    """
    if not config.get("enabled"):
        return []
    audio_root = game_dir / "audio"
    if not audio_root.is_dir():
        return []
    budget = int(config.get("scan_per_run") or 40)
    cache = voice_asr_index()
    model = str(config.get("model") or "large-v3-turbo")
    names = {p.resolve() for p in known}
    todo: list[Path] = []
    for path in sorted(p for p in audio_root.rglob("*") if p.suffix.lower() in AUDIO_EXTS):
        if path.resolve() in names:
            continue
        item = cache.get(str(path))
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        if isinstance(item, dict) and item.get("mtime") == mtime and item.get("text"):
            continue
        todo.append(path)
        if len(todo) >= budget:
            break
    if todo:
        log(f"asr scan {game_dir.name}: listening to {len(todo)} unused voice file(s)")
        for path, text in asr_transcribe(todo, config).items():
            audio = Path(path)
            try:
                mtime = int(audio.stat().st_mtime)
            except OSError:
                mtime = 0
            cache[str(audio)] = {"mtime": mtime, "text": (text or "").strip()}
        save_voice_asr_index(cache)
    out = []
    for path in sorted(p for p in audio_root.rglob("*") if p.suffix.lower() in AUDIO_EXTS):
        item = cache.get(str(path))
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if len(match_key(text)) < 4:
            continue
        out.append({"text": text, "voice": str(path.relative_to(game_dir)), "path": path})
    return out


def build_map(game_dirs: list[str]) -> dict:
    index: dict[str, dict] = {}
    durations = load_durations()
    try:
        asr_cfg = asr_config()
    except OSError:
        asr_cfg = {"enabled": False}
    pairings: dict[str, str] = {}
    stats = {"games": 0, "lines": 0, "missing": 0, "soundfx": 0}
    for raw in game_dirs:
        game_dir = Path(raw)
        if not game_dir.is_dir():
            log(f"skip missing game dir: {game_dir}")
            continue
        resolver = rpgmaker_audio
        engine = "rpgmaker"
        pairing = ""
        variants = rpgmaker_pairs(game_dir)
        if variants["after"] or variants["before"]:
            scores = {name: pairing_score(items, resolver, game_dir, durations)
                      for name, items in variants.items()}
            pairing = max(scores, key=lambda name: scores[name])
            pairs = variants[pairing]
            log(f"pairing check {game_dir.name}: after={scores['after']:.2f} "
                f"before={scores['before']:.2f} -> {pairing}")
        else:
            pairs = renpy_pairs(game_dir)
            resolver = renpy_audio
            engine = "renpy"
            pairing = "renpy"
        if pairs:
            score = pairing_score(pairs, resolver, game_dir, durations)
            if score < 0.5:
                log(f"WARNING {game_dir.name}: voice/text pairing not confirmed "
                    f"(r={score:.2f}); its cards will be left untouched")
                stats["games"] += 1
                continue
            if asr_cfg.get("enabled"):
                min_match = float(asr_cfg.get("min_match") or 0.5)
                asr_score, asr_n = asr_verify(pairs, resolver, game_dir, asr_cfg)
                if asr_n >= 3:
                    log(f"asr check {game_dir.name}: match {asr_score:.2f} "
                        f"on {asr_n} sample(s)")
                if asr_n >= 3 and asr_score < min_match and engine == "rpgmaker":
                    other = "before" if pairing == "after" else "after"
                    alt_score, alt_n = asr_verify(variants[other], resolver, game_dir, asr_cfg)
                    log(f"asr retry '{other}': {alt_score:.2f} on {alt_n} sample(s)")
                    if alt_n >= 3 and alt_score > asr_score:
                        pairing, pairs = other, variants[other]
                        asr_score = alt_score
                        log(f"switched voice pairing to '{other}'")
                if asr_n >= 3 and asr_score < min_match:
                    log(f"WARNING {game_dir.name}: speech does not match its text "
                        f"(best {asr_score:.2f}); its cards will be left untouched")
                    stats["games"] += 1
                    continue
        pairings[game_dir.name] = pairing
        stats["games"] += 1
        if engine == "rpgmaker":
            try:
                heard = asr_scan(game_dir, {p for p in (resolver(game_dir, v) for _t, v, _s in pairs) if p},
                                 asr_cfg)
            except OSError as exc:
                log(f"asr scan failed: {exc}")
                heard = []
            for item in heard:
                key = match_key(item["text"])
                if len(key) < 4 or key in index:
                    continue
                audio = item["path"]
                index[key] = {
                    "text": item["text"],
                    "voice": item["voice"],
                    "audio": str(audio),
                    "speaker": "",
                    "engine": "asr",
                    "game": game_dir.name,
                    "seconds": durations.get(str(audio)) or probe_duration(audio) or 0.0,
                }
                stats["lines"] += 1
        grouped: dict[str, list[tuple]] = {}
        for text, voice, speaker in pairs:
            key = match_key(text)
            if len(key) < 6:
                continue
            grouped.setdefault(key, []).append((text, voice, speaker))
        for key, variants in grouped.items():
            if key in index:
                continue
            best: tuple[float, dict] | None = None
            for text, voice, speaker in variants:
                audio = resolver(game_dir, voice)
                if audio is None:
                    stats["missing"] += 1
                    continue
                dur = duration_of(audio, durations)
                if not speech_ratio_ok(dur, len(key)):
                    stats["soundfx"] += 1
                    continue
                score = abs(dur / len(key) - 0.18)
                if best is None or score < best[0]:
                    best = (score, {"text": text, "voice": voice,
                                    "audio": str(audio), "speaker": speaker,
                                    "engine": engine, "game": game_dir.name,
                                    "seconds": round(dur, 2)})
            if best is not None:
                index[key] = best[1]
                stats["lines"] += 1
        log(f"indexed {engine} game: {game_dir.name} (+{stats['lines']} lines, "
            f"skipped sound effects: {stats['soundfx']})")
    save_durations(durations)
    return {"built": time.time(), "stats": stats, "pairing": pairings, "index": index}


def load_games() -> list[str]:
    if not GAMES_FILE.is_file():
        GAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        GAMES_FILE.write_text(json.dumps({"games": DEFAULT_GAMES}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        return DEFAULT_GAMES
    try:
        data = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_GAMES
    games = data.get("games") if isinstance(data, dict) else data
    return [str(g) for g in (games or [])]


def load_map(force: bool = False) -> dict:
    games = load_games()
    if not force and MAP_FILE.is_file():
        try:
            cached = json.loads(MAP_FILE.read_text(encoding="utf-8"))
            if time.time() - cached.get("built", 0) < MAP_MAX_AGE and cached.get("index"):
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    data = build_map(games)
    if data["index"]:
        MAP_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# -------------------------------------------------------------------- matching


def matcher(index: dict):
    items: dict[str, dict] = {}
    speakers: set[str] = set()
    for entry in index.values():
        key = match_key(entry["text"])
        if len(key) >= 6:
            items.setdefault(key, entry)
        speaker = match_key(entry.get("speaker", ""))
        if speaker:
            speakers.add(speaker)
    return items, speakers


def find_voice(items: dict, speakers: set[str], sentence: str,
               cover: float = 0.85, min_block: int = 8):
    """Card text is often a truncated/cropped piece of the game line (OCR cut),
    so we accept a game line when almost the whole card text sits inside it."""
    key = match_key(sentence)
    if key in items and len(key) >= 4:
        return items[key]
    for speaker in speakers:
        key = key.replace(speaker, "") if len(key) - len(speaker) >= 6 else key
    if len(key) < min_block:
        return None
    if key in items:
        return items[key]
    best, best_score = None, 0.0
    for candidate, entry in items.items():
        if len(candidate) < min_block:
            continue
        block = difflib.SequenceMatcher(None, key, candidate, autojunk=False) \
            .find_longest_match(0, len(key), 0, len(candidate))
        if block.size < min_block or block.size < cover * len(key):
            continue
        # the matched piece must touch the start or the end of the card text,
        # otherwise it is a random fragment inside a different line
        if block.a > 3 and len(key) - (block.a + block.size) > 3:
            continue
        score = block.size / len(key) - 0.001 * abs(len(candidate) - len(key))
        if score > best_score:
            best, best_score = entry, score
    return best if best_score > 0 else None


def find_lines(items: dict, speakers: set[str], sentence: str,
               take: float = 0.62) -> list[dict]:
    """Every game line spoken inside this card, in reading order.

    A card often shows two or three message windows plus a menu label, so a
    single-line match would cut the audio in half. Each displayed line is
    matched on its own and the voices are then glued back together.
    """
    out: list[dict] = []
    for raw in re.split(r"<br\s*/?>|[\r\n]+", sentence or ""):
        line = re.sub(r"<[^>]+>", "", raw).strip()
        key = match_key(line)
        if len(key) < 4:
            continue
        if "合体攻略" in line or "すけべすごろく" in line or key in speakers:
            continue
        best, best_score = None, 0.0
        for candidate, entry in items.items():
            if key in candidate or candidate in key:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, key, candidate, autojunk=False).ratio()
            score -= 0.02 * abs(len(candidate) - len(key)) / max(len(key), 1)
            if score > best_score:
                best, best_score = entry, score
        if best is not None and best_score >= take:
            if not out or out[-1].get("voice") != best.get("voice"):
                out.append(best)
    return out


# ------------------------------------------------------------------ anki write


def pick_field(fields: dict, preferred: str) -> str | None:
    if preferred in fields:
        return preferred
    compact = preferred.replace(" ", "").lower()
    for name in fields:
        if name.replace(" ", "").lower() == compact:
            return name
    for name in fields:
        lowered = name.lower()
        if "audio" in lowered and ("sentence" in lowered or "context" in lowered):
            return name
    return None


def set_tags(note_id: int, add=(), remove=()) -> None:
    if add:
        anki("addTags", notes=[note_id], tags=" ".join(add))
    if remove:
        anki("removeTags", notes=[note_id], tags=" ".join(remove))


def sanitize_tag(name: str) -> str:
    return re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]+", "_", name or "").strip("_")[:48]


def load_gsm_games() -> dict[str, str]:
    """note id -> game name, straight from GSM's own database."""
    db = APP_DIR / "gsm.db"
    games: dict[str, str] = {}
    if not db.is_file():
        return games
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            for note_ids, game_name in con.execute(
                    "select note_ids, game_name from game_lines "
                    "where note_ids is not null and note_ids != '' order by timestamp desc"):
                try:
                    ids = json.loads(note_ids)
                except (TypeError, json.JSONDecodeError):
                    continue
                for note_id in ids:
                    games.setdefault(str(note_id), str(game_name))
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        log(f"gsm.db games unavailable: {exc}")
    return games


# ------------------------------------------------------- own word audio pack

WORD_AUDIO_EXTS = (".mp3", ".ogg", ".m4a", ".wav", ".opus")


def load_word_dirs() -> list[str]:
    if not WORD_DIRS_FILE.is_file():
        WORD_DIRS_FILE.write_text(json.dumps({
            "dirs": [],
            "note": "Папки со своей озвучкой слов. Имя файла = слово (無茶振り.mp3, 無茶振り_1.mp3 ...).",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return []
    try:
        data = json.loads(WORD_DIRS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    dirs = data.get("dirs") if isinstance(data, dict) else data
    return [str(d) for d in (dirs or [])]


def word_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    return MATCH_JUNK_RE.sub("", text).lower()


def load_word_index(force: bool = False) -> dict:
    dirs = load_word_dirs()
    if not dirs:
        return {}
    if not force and WORD_INDEX_FILE.is_file():
        try:
            cached = json.loads(WORD_INDEX_FILE.read_text(encoding="utf-8"))
            if cached.get("dirs") == dirs and time.time() - cached.get("built", 0) < MAP_MAX_AGE:
                return cached.get("index") or {}
        except (json.JSONDecodeError, OSError):
            pass
    index: dict[str, str] = {}
    for raw in dirs:
        base = Path(raw)
        if not base.is_dir():
            log(f"skip missing word pack dir: {base}")
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in WORD_AUDIO_EXTS:
                continue
            stem = path.stem
            index.setdefault(word_key(stem), str(path))
            for sep in ("_", " - ", "-"):
                if sep in stem:
                    index.setdefault(word_key(stem.split(sep)[0]), str(path))
    if index:
        WORD_INDEX_FILE.write_text(json.dumps({"built": time.time(), "dirs": dirs,
                                               "index": index}, ensure_ascii=False),
                                   encoding="utf-8")
        log(f"word pack indexed: {len(index)} words")
    return index


def pick_word_field(fields: dict) -> str | None:
    for name in ("ExpressionAudio", "Word Audio", "WordAudio"):
        if name in fields:
            return name
    for name in fields:
        lowered = name.lower()
        if "audio" in lowered and "sentence" not in lowered:
            return name
    return None


def find_word_audio(index: dict, fields: dict) -> Path | None:
    if not index:
        return None
    for name in ("Expression", "Word", "ExpressionReading", "Word Reading"):
        value = fields.get(name, {}).get("value", "")
        key = word_key(re.sub(r"<[^>]+>", "", value))
        if key and key in index:
            return Path(index[key])
    return None


def attach_word_audio(note: dict, source: Path) -> str:
    fields = note["fields"]
    field = pick_word_field(fields)
    if field is None:
        return f"no word-audio field on {note.get('modelName')}"
    media, ext = to_mp3(source)
    media_name = f"gvw_{source.stem}.{ext}"
    anki("storeMediaFile", filename=media_name,
         data=base64.b64encode(media.read_bytes()).decode("ascii"))
    anki("updateNoteFields", note={"id": note["noteId"], "fields": {field: f"[sound:{media_name}]"}})
    return f"{media_name} -> {field}"


# ------------------------------------------------- neural TTS (AnkiVoice)


def default_anki_voice_dir() -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    return str(Path(local) / "Programs" / "AnkiVoice") if local else ""


def default_sovits_root(anki_voice_dir: str) -> str:
    path_file = Path(anki_voice_dir) / "app" / "gptsovits_path.txt"
    try:
        text = path_file.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    except OSError:
        pass
    return ""


def load_tts_config() -> dict:
    if not TTS_FILE.is_file():
        anki_voice = default_anki_voice_dir()
        config = {
            "enabled": True,
            "anki_voice_dir": anki_voice,
            "gptsovits_root": default_sovits_root(anki_voice),
            "voice": "",
            "mode": "both",
            "max_per_run": 10,
            "timeout_seconds": 900,
        }
        TTS_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
        return config
    try:
        config = json.loads(TTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": False}
    return config if isinstance(config, dict) else {"enabled": False}


def tts_busy() -> bool:
    try:
        return TTS_LOCK.is_file() and time.time() - TTS_LOCK.stat().st_mtime < 1800
    except OSError:
        return False


def run_tts(note_ids: list[int]) -> None:
    """Oзвучить карточки нейронкой через GPT-SoVITS пайплайн AnkiVoice."""
    config = load_tts_config()
    if not config.get("enabled") or not note_ids or tts_busy():
        return
    app_dir = Path(config.get("anki_voice_dir") or "") / "app"
    script = app_dir / "voice_pipeline.py"
    python = Path(config.get("gptsovits_root") or "") / "runtime" / "python.exe"
    if not script.is_file() or not python.is_file():
        log(f"tts skipped: pipeline/python not found ({script}, {python})")
        return
    command = [str(python), "-u", str(script), "voice",
               "--ids", *[str(n) for n in note_ids],
               "--mode", str(config.get("mode") or "both")]
    if config.get("voice"):
        command += ["--voice", str(config["voice"])]
    stale_lock = Path(str(config.get("gptsovits_root"))) / "logs" / "pipeline.lock"
    try:
        if stale_lock.is_file() and time.time() - stale_lock.stat().st_mtime > 1800:
            stale_lock.unlink()
            log("removed stale GPT-SoVITS pipeline.lock")
    except OSError:
        pass
    env = dict(os.environ, GPT_SOVITS_ROOT=str(config.get("gptsovits_root")),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    TTS_LOCK.write_text(str(int(time.time())), encoding="utf-8")
    log(f"tts start: {len(note_ids)} card(s)")
    try:
        done = subprocess.run(command, cwd=str(app_dir), env=env,
                              creationflags=flags, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=int(config.get("timeout_seconds") or 900))
        combined = (done.stdout or "") + (done.stderr or "")
        if done.returncode and "Пайплайн уже запущен" in combined:
            # a killed run left the engine lock behind - clear it and try once more
            try:
                stale_lock.unlink()
                log("tts retry after clearing stale engine lock")
            except OSError:
                pass
            done = subprocess.run(command, cwd=str(app_dir), env=env,
                                  creationflags=flags, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=int(config.get("timeout_seconds") or 900))
        tail = [line for line in (done.stdout or "").splitlines() if line.strip()][-4:]
        log(f"tts exit {done.returncode}: " + " | ".join(tail))
        if done.returncode:
            err = [line for line in (done.stderr or "").splitlines() if line.strip()][-3:]
            log("tts stderr: " + " | ".join(err))
    except subprocess.TimeoutExpired:
        log("tts timeout")
    except OSError as exc:
        log(f"tts failed: {exc}")
    finally:
        try:
            TTS_LOCK.unlink()
        except OSError:
            pass


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in APP_DIR.glob("ffmpeg*/**/ffmpeg.exe"):
        return str(candidate)
    return None


def to_mp3(source: Path) -> tuple[Path, str]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return source, source.suffix.lstrip(".")
    target = Path(tempfile.gettempdir()) / f"gv_{source.stem}.mp3"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                    "-codec:a", "libmp3lame", "-q:a", "4", str(target)], check=True)
    return target, "mp3"


def attach(note: dict, entry: dict, sentence_field: str, audio_field: str) -> str:
    return attach_many(note, [entry], sentence_field, audio_field)


def attach_many(note: dict, entries: list[dict], sentence_field: str, audio_field: str) -> str:
    """Put the voice of one or several game lines on the card."""
    fields = note["fields"]
    field = pick_field(fields, audio_field)
    if field is None:
        return f"no audio field on {note.get('modelName')}"
    sources = [Path(e["audio"]) for e in entries
               if e.get("audio") and Path(str(e["audio"])).is_file()]
    if not sources:
        return "missing voice file"
    if len(sources) == 1:
        media, ext = to_mp3(sources[0])
        media_name = f"gv_{sources[0].stem}.{ext}"
    else:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            media, ext = to_mp3(sources[0])
            media_name = f"gv_{sources[0].stem}.{ext}"
        else:
            target = Path(tempfile.gettempdir()) / f"gv_{sources[0].stem}_{len(sources)}.mp3"
            args = [ffmpeg, "-y", "-loglevel", "error"]
            for source in sources:
                args += ["-i", str(source)]
            filt = "".join(f"[{i}:a]apad=pad_dur=0.12[a{i}];" for i in range(len(sources)))
            filt += "".join(f"[a{i}]" for i in range(len(sources)))
            filt += f"concat=n={len(sources)}:v=0:a=1[out]"
            args += ["-filter_complex", filt, "-map", "[out]",
                     "-codec:a", "libmp3lame", "-q:a", "4", str(target)]
            subprocess.run(args, check=True)
            media, media_name = target, target.name
    anki("storeMediaFile", filename=media_name,
         data=base64.b64encode(media.read_bytes()).decode("ascii"))
    anki("updateNoteFields", note={"id": note["noteId"], "fields": {field: f"[sound:{media_name}]"}})
    return f"{media_name} -> {field}"


# ------------------------------------------------------------------------ main


def main():
    try:
        anki("version")
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        log(f"anki not available: {exc}")
        return

    data = load_map()
    index = data.get("index") or {}
    if not index:
        log("no voice map built - check gv_games.json")
        return
    items, speakers = matcher(index)

    note_ids = anki("findNotes", query="added:1") or []
    if not note_ids:
        return
    notes = anki("notesInfo", notes=note_ids)

    polished = 0
    tagged = 0
    words_done = 0
    word_index = {}
    try:
        word_index = load_word_index()
    except OSError as exc:
        log(f"word pack failed: {exc}")
    gsm_games = load_gsm_games()
    tts_ids: list[int] = []
    tts_config = load_tts_config()
    tts_limit = int(tts_config.get("max_per_run") or 10)
    for note in notes:
        fields = note.get("fields") or {}
        sentence = fields.get("Sentence", {}).get("value", "")
        audio_field = pick_field(fields, "Sentence Audio")
        current = fields.get(audio_field, {}).get("value", "") if audio_field else ""
        exact_done = current.strip().startswith("[sound:gv_")

        if sentence:
            cleaned = clean_sentence(sentence, speakers)
            update: dict[str, str] = {}
            if cleaned and cleaned != sentence:
                update["Sentence"] = cleaned
                sentence = cleaned
            furi_name = next((n for n in ("SentenceFurigana", "Sentence Furigana")
                              if n in fields), None)
            furigana = fields.get(furi_name, {}).get("value", "") if furi_name else ""
            if furigana:
                furigana_clean = clean_furigana(furigana)
                if furigana_clean != furigana:
                    update[furi_name] = furigana_clean
            if update:
                try:
                    anki("updateNoteFields", note={"id": note["noteId"], "fields": update})
                    log(f"cleaned {', '.join(update)} of {note['noteId']}")
                except RuntimeError as exc:
                    log(f"clean failed {note['noteId']}: {exc}")

        picked = find_lines(items, speakers, sentence)
        entry = picked[0] if picked else find_voice(items, speakers, sentence)
        tags_add: list[str] = []
        tags_remove: list[str] = []
        game = gsm_games.get(str(note["noteId"])) or (entry or {}).get("game", "")
        game_part = sanitize_tag(game)
        wanted_game = f"voice::game::{game_part}" if game_part else ""
        if wanted_game:
            tags_add.append(wanted_game)
        tags_remove += [t for t in note.get("tags", [])
                        if t.startswith("voice::game::") and t != wanted_game]
        if entry:
            if not exact_done:
                try:
                    result = attach_many(note, picked or [entry], "Sentence", "Sentence Audio")
                    polished += 1
                    log(f"{note['noteId']}: {result} ({entry['voice']})")
                except (RuntimeError, urllib.error.URLError, OSError,
                        subprocess.CalledProcessError) as exc:
                    log(f"attach failed {note['noteId']}: {exc}")
            tags_add += ["voice::exact", f"voice::{entry['engine']}"]
            tags_remove.append("voice::no-source")
            tagged += 1
        else:
            tags_add.append("voice::no-source")
        try:
            set_tags(note["noteId"], add=tags_add, remove=tags_remove)
        except RuntimeError as exc:
            log(f"tag failed {note['noteId']}: {exc}")

        word_field = pick_word_field(fields)
        word_empty = bool(word_field) and not fields.get(word_field, {}).get("value", "") \
            .startswith("[sound:")
        if word_empty:
            word_source = find_word_audio(word_index, fields)
            if word_source is not None:
                try:
                    result = attach_word_audio(note, word_source)
                    words_done += 1
                    set_tags(note["noteId"], add=["voice::word"])
                    word_empty = False
                    log(f"{note['noteId']}: word audio {result}")
                except (RuntimeError, urllib.error.URLError, OSError,
                        subprocess.CalledProcessError) as exc:
                    log(f"word audio failed {note['noteId']}: {exc}")

        sentence_has_audio = (audio_field and
                              bool(fields.get(audio_field, {}).get("value", "").strip()))
        if (not sentence_has_audio and sentence) or word_empty:
            tts_ids.append(int(note["noteId"]))

    if tts_ids:
        run_tts(tts_ids[:tts_limit])

    if polished or tagged or words_done:
        log(f"polished {polished} card(s), exact-tagged {tagged}, word audio {words_done}")
