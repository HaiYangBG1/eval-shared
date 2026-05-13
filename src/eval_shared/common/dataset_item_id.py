"""Dataset item ID 计算（sync_dataset push / promptfoo_ab cache 共用）。

抽到 common 是为了保证两侧用同一种算法——sync 写入 Langfuse 的 item.id 必须
与 promptfoo_ab 在本地复算后查 cache 时用的 id 严格相等。
"""

from __future__ import annotations

import hashlib
import json


def compute_item_id(dataset_name: str, vars_data: dict) -> str:
    """生成稳定的 dataset item id：`{dataset_name}-{sha1(sorted_json(vars))[:10]}`。

    `sort_keys=True` 保证字典序无关；只对 `vars` hash，不含 `assert`/`metadata`，
    所以同一组输入即便后续断言变化仍是同一个 item id。
    """
    id_hash = hashlib.sha1(
        json.dumps(vars_data, sort_keys=True).encode()
    ).hexdigest()[:10]
    return f"{dataset_name}-{id_hash}"
