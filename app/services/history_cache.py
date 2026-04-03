from __future__ import annotations

import gzip
import pickle
from pathlib import Path
from typing import Callable, Dict, Tuple

import pandas as pd

from app.config import Settings


FrameMap = Dict[str, pd.DataFrame]


def _cache_root(settings: Settings) -> Path:
    path = Path(settings.data_dir) / 'cache' / 'historical'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(value: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in value)


def cache_path(settings: Settings, category: str, key: str) -> Path:
    category_dir = _cache_root(settings) / _safe_name(category)
    category_dir.mkdir(parents=True, exist_ok=True)
    return category_dir / f"{_safe_name(key)}.pkl.gz"


def _normalize_frame(df: pd.DataFrame | object) -> pd.DataFrame:
    rebuilt = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    if 'timestamp' in rebuilt.columns:
        rebuilt['timestamp'] = pd.to_datetime(rebuilt['timestamp'], utc=True, errors='coerce')
        rebuilt = rebuilt.dropna(subset=['timestamp']).reset_index(drop=True)
    return rebuilt


def load_frame_map(path: Path) -> FrameMap:
    with gzip.open(path, 'rb') as handle:
        payload = pickle.load(handle)
    output: FrameMap = {}
    for symbol, df in payload.items():
        output[symbol] = _normalize_frame(df)
    return output


def save_frame_map(path: Path, frame_map: FrameMap) -> None:
    serializable = {}
    for symbol, df in frame_map.items():
        if df is None:
            continue
        working = _normalize_frame(df)
        if 'timestamp' in working.columns:
            working['timestamp'] = working['timestamp'].astype(str)
        serializable[symbol] = working
    with gzip.open(path, 'wb') as handle:
        pickle.dump(serializable, handle, protocol=pickle.HIGHEST_PROTOCOL)


def fetch_or_cache_frame_map(
    settings: Settings,
    *,
    category: str,
    key: str,
    loader: Callable[[], FrameMap],
) -> Tuple[FrameMap, Dict[str, object]]:
    path = cache_path(settings, category, key)
    if path.exists():
        try:
            data = load_frame_map(path)
            return data, {'cache_hit': True, 'path': str(path), 'symbols': len(data)}
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
    data = loader()
    save_frame_map(path, data)
    return data, {'cache_hit': False, 'path': str(path), 'symbols': len(data)}
