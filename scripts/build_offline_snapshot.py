"""
scripts/build_offline_snapshot.py
=================================

从外部「推荐系统」发布的只读制品中导出一份**紧凑离线快照**，写入 data/offline/。

为什么需要它
------------
任务要求「离线演示模式可运行」。本脚本只读取推荐系统 *对外发布* 的制品文件
（webapp/data/*.json、products.csv、submission csv），不 import 其任何 Python 模块、
不访问其内部变量，因此符合「两套系统不直接读取对方内部变量」的接口约定。

产出（全部 UTF-8 JSON）:
    data/offline/catalog.json    商品目录（id/标题/类目/价格）
    data/offline/similar.json    商品相似索引
    data/offline/scores.json     个性化推荐分数 {user_id: {item_id: score}}
    data/offline/popular.json    全局热门 + 品类热门（兜底召回用）
    data/offline/history.json    用户购买历史（用于过滤已购与解释）
    data/offline/users.json      演示用户档案
    data/offline/meta.json       快照元信息（来源、币种、统计、生成时间）

用法:
    python scripts/build_offline_snapshot.py
    python scripts/build_offline_snapshot.py --recommender-root "D:/path/to/推荐系统"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from src.utils import now_iso, write_json  # noqa: E402


def _read_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def build(recommender_root: Path, out_dir: Path) -> dict:
    interop = settings.RECOMMENDER_INTEROP
    products_csv = recommender_root / interop["products_csv"]
    app_data_json = recommender_root / interop["app_data_json"]
    price_map_json = recommender_root / interop["price_map_json"]
    history_json = recommender_root / interop["user_history_json"]
    submission_csv = recommender_root / interop["submission_csv"]

    for path in (products_csv, app_data_json, price_map_json):
        if not path.exists():
            raise FileNotFoundError(f"缺少推荐系统发布制品: {path}")

    products_rows = list(csv.DictReader(products_csv.open("r", encoding="utf-8-sig")))
    app_data = _read_json(app_data_json)
    price_map = _read_json(price_map_json)
    price_items = price_map.get("items", {})
    default_price = float(price_map.get("default_price", 0.0) or 0.0)

    catalog: dict[str, dict] = {}
    for row in products_rows:
        code = str(row.get("StockCode", "")).strip()
        if not code:
            continue
        price_rec = price_items.get(code, {})
        price = price_rec.get("price", default_price)
        catalog[code] = {
            "item_id": code,
            "stock_code": code,
            "title": str(row.get("Chinese_Description") or row.get("Description") or "").strip(),
            "english_title": str(row.get("Description") or "").strip(),
            "category": str(row.get("Product_Category") or "其他").strip() or "其他",
            "price": round(float(price), 4) if price is not None else None,
            "currency": price_map.get("currency", "GBP"),
            "price_source": price_rec.get("source", "catalog_default"),
            "transactions": int(price_rec.get("transactions", 0) or 0),
        }

    # 相似索引（截断到 top-12，保持体积可控）
    similar_src = app_data.get("similar", {}) or {}
    similar: dict[str, list[str]] = {}
    for item_id, neighbors in similar_src.items():
        keep: list[str] = []
        for candidate in neighbors:
            cand = str(candidate)
            if cand in catalog and cand != str(item_id) and cand not in keep:
                keep.append(cand)
            if len(keep) >= 12:
                break
        if keep:
            similar[str(item_id)] = keep

    # 个性化分数
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    if submission_csv.exists():
        with submission_csv.open("r", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                user = str(row.get("user_id", "")).strip()
                item = str(row.get("item_id", "")).strip()
                if not user or not item or item not in catalog:
                    continue
                try:
                    scores[user][item] = round(float(row.get("score", 0.0) or 0.0), 6)
                except ValueError:
                    continue

    # 演示用户在 app_data 里可能没有全量分数，用 recommendations 兜底
    for user, rows in (app_data.get("recommendations") or {}).items():
        bucket = scores.setdefault(str(user), {})
        for row in rows:
            item = str(row.get("item_id"))
            if item in catalog and item not in bucket:
                bucket[item] = round(float(row.get("score", 0.0) or 0.0), 6)

    # 热门：全局 + 按类目（相似索引出度作为热度信号）
    popularity: Counter[str] = Counter()
    for neighbors in similar.values():
        popularity.update(neighbors)
    global_top = [item for item, _ in popularity.most_common(400) if item in catalog]

    by_category: dict[str, list[str]] = defaultdict(list)
    for item in global_top:
        by_category[catalog[item]["category"]].append(item)
    category_files: dict[str, int] = {c: len(v) for c, v in by_category.items()}

    popular = {
        "global_top": global_top,
        "by_category": {c: ids[:120] for c, ids in by_category.items()},
        "popularity": {item: int(cnt) for item, cnt in popularity.most_common(2000)},
    }

    history = _read_json(history_json) if history_json.exists() else {}
    users = {
        "users": app_data.get("users", {}),
        "model_version": app_data.get("model_version", "unknown"),
    }

    meta = {
        "generated_at": now_iso(),
        "source_root_found": True,
        "cutoff": price_map.get("cutoff"),
        "currency": price_map.get("currency", "GBP"),
        "symbol": price_map.get("symbol", "£"),
        "model_version": app_data.get("model_version", "unknown"),
        "counts": {
            "catalog": len(catalog),
            "similar": len(similar),
            "users_with_scores": len(scores),
            "score_rows": sum(len(v) for v in scores.values()),
            "categories": len(category_files),
        },
        "categories": sorted({row["category"] for row in catalog.values()}),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "catalog.json", catalog, indent=None)
    write_json(out_dir / "similar.json", similar, indent=None)
    write_json(out_dir / "scores.json", dict(scores), indent=None)
    write_json(out_dir / "popular.json", popular, indent=None)
    write_json(out_dir / "history.json", history, indent=None)
    write_json(out_dir / "users.json", users)
    write_json(out_dir / "meta.json", meta)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="导出推荐系统离线快照")
    parser.add_argument("--recommender-root", default=None, help="推荐系统根目录（默认取 config 解析结果）")
    parser.add_argument("--out", default=str(settings.PATHS["offline"]), help="输出目录")
    args = parser.parse_args()

    root = Path(args.recommender_root).expanduser() if args.recommender_root else settings.RECOMMENDER_ROOT
    if root is None or not Path(root).is_dir():
        print("[build_offline_snapshot] 未找到推荐系统根目录。")
        print("  请用 --recommender-root 指定，或设置环境变量 INTEGRATED_RECOMMENDER_ROOT。")
        return 2

    meta = build(Path(root), Path(args.out))
    print(f"[build_offline_snapshot] 快照已生成 → {args.out}")
    for key, value in meta["counts"].items():
        print(f"  - {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
