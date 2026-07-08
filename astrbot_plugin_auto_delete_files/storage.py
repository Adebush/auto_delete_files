"""持久化存储：清理历史记录 & 系统配置"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = DATA_DIR / "auto_delete_history.json"
CONFIG_FILE = DATA_DIR / "auto_delete_config.json"

BEIJING_TZ = timezone(timedelta(hours=8))


# ── 清理历史 ──────────────────────────────────

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_history(records: list):
    HISTORY_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_history_entry(
    group_id: int,
    operator: str,
    deleted_count: int,
    failed_count: int,
    file_names: list,
):
    records = load_history()
    records.insert(0, {
        "time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "group_id": group_id,
        "operator": operator,
        "deleted_count": deleted_count,
        "failed_count": failed_count,
        "total": deleted_count + failed_count,
        "files": file_names[:50],  # 最多存50个文件名
    })
    # 保留最近500条记录
    save_history(records[:500])


# ── 系统配置 ──────────────────────────────────

DEFAULT_CONFIG = {
    "auto_clean_time": "00:00",
    "auto_clean_day": 1,
}

_config_cache = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if CONFIG_FILE.exists():
        try:
            _config_cache = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return _config_cache
        except (json.JSONDecodeError, OSError):
            pass
    _config_cache = dict(DEFAULT_CONFIG)
    return _config_cache


def save_config(config: dict):
    global _config_cache
    _config_cache = config
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
