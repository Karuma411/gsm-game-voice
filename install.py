"""Установка плагина gsm-game-voice в GameSentenceMiner.

Кладёт plugin.py в %APPDATA%\\GameSentenceMiner\\plugins.py, создаёт конфиги
и переключает cron-запись user_plugins на запуск раз в минуту.

    python install.py            установить / обновить
    python install.py --uninstall   убрать плагин (plugins.py.bak вернётся на место)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GameSentenceMiner"
TARGET = APP_DIR / "plugins.py"
BACKUP = APP_DIR / "plugins.py.bak"

DEFAULT_CONFIGS = {
    "gv_games.json": {"games": []},
    "gv_word_audio.json": {"dirs": [],
                           "note": "Папки со своей озвучкой слов. Имя файла = слово (無茶振り.mp3)."},
    "gv_tts.json": {
        "enabled": True,
        "anki_voice_dir": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "AnkiVoice"),
        "gptsovits_root": "",
        "voice": "",
        "mode": "both",
        "max_per_run": 10,
        "timeout_seconds": 900,
    },
    "gv_asr.json": {
        "enabled": True,
        "python": "",
        "model": "large-v3-turbo",
        "samples": 8,
        "min_match": 0.5,
    },
}


def write_configs() -> None:
    for name, payload in DEFAULT_CONFIGS.items():
        path = APP_DIR / name
        if path.exists():
            print(f"  = {name} уже есть, не трогаю")
            continue
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  + {name}")


def set_minutely_cron() -> None:
    """GSM по умолчанию зовёт плагин раз в 15 минут, нам нужна минута."""
    db = APP_DIR / "gsm.db"
    if not db.is_file():
        print("  ! gsm.db ещё нет — запусти GSM один раз и повтори install.py "
              "(--cron), иначе плагин будет работать раз в 15 минут")
        return
    try:
        con = sqlite3.connect(db, timeout=20)
        cur = con.cursor()
        cur.execute("UPDATE cron_table SET schedule='minutely', enabled=1, next_run=? "
                    "WHERE name='user_plugins'", (time.time() + 30,))
        con.commit()
        row = cur.execute("SELECT schedule, enabled FROM cron_table WHERE name='user_plugins'").fetchone()
        con.close()
        print(f"  cron user_plugins -> {row}")
    except sqlite3.Error as exc:
        print(f"  ! не смог поправить cron: {exc}")


def install() -> None:
    if not APP_DIR.is_dir():
        print(f"Не нашёл папку GSM: {APP_DIR}")
        print("Сначала установи и хотя бы раз запусти GameSentenceMiner.")
        sys.exit(1)
    source = HERE / "plugin.py"
    if not source.is_file():
        print("Рядом нет plugin.py — скачай репозиторий целиком.")
        sys.exit(1)
    if TARGET.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"  ~ старый плагин сохранён: {BACKUP.name}")
    shutil.copy2(source, TARGET)
    print(f"  + {TARGET}")
    print("Конфиги:")
    write_configs()
    print("Планировщик:")
    set_minutely_cron()
    print("\nГотово. Перезапусти GameSentenceMiner один раз.")
    print(f"Лог работы: {APP_DIR / 'gv_log.txt'}")


def uninstall() -> None:
    if BACKUP.is_file():
        shutil.copy2(BACKUP, TARGET)
        print(f"Вернул старый plugins.py из {BACKUP.name}")
    elif TARGET.exists():
        TARGET.unlink()
        print("Удалил plugins.py")
    print("Конфиги gv_*.json оставил — удали вручную, если больше не нужны.")


def main() -> None:
    args = [a.lower() for a in sys.argv[1:]]
    if "--uninstall" in args:
        uninstall()
    elif "--cron" in args:
        set_minutely_cron()
    else:
        install()


if __name__ == "__main__":
    main()
