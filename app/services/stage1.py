from __future__ import annotations

from typing import Dict, List

import pandas as pd

from app.services.shared_logic import build_stage1_target_group


def select_top_positive_movers(stage1_records: List[Dict[str, object]], top_mover_count: int) -> pd.DataFrame:
    return build_stage1_target_group(stage1_records, top_mover_count)
