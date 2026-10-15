"""src/catalog.py —— 商品目录。

回归锁的重点是 category_top 的 fallback 分支。

历史缺陷：_cat_top（离线快照预计算的「类目热门榜」）只覆盖部分类目；
其余类目走 fallback 分支，而该分支曾误写为迭代 self.catalog.values()
（商品 dict）而不是 items()（商品 ID），于是返回一堆 dict。
调用方 src/adapters/recommender_adapter.py 把它当 ID 集合使用，
执行 `i not in exclude` 时直接抛 TypeError: unhashable type: 'dict'。

mypy 在 return-value 上发现了它（声明 list[str] 却返回 list[dict]），
这里用运行期断言把同一个不变量钉死 —— 以后无论谁改，都会在测试里先炸。
"""

from __future__ import annotations

import pytest

from src.catalog import Catalog


@pytest.fixture(scope="module")
def catalog():
    return Catalog.from_snapshot()


def test_categories_non_empty(catalog):
    assert len(catalog.categories()) > 1


def test_category_top_returns_ids_for_every_category(catalog):
    """无论类目是否在 _cat_top 里，都必须返回商品 ID 字符串。"""
    for category in catalog.categories():
        ids = catalog.category_top(category, 3)
        assert all(isinstance(i, str) for i in ids), f"{category} 返回了非字符串元素"
        assert all(catalog.exists(i) for i in ids), f"{category} 返回了未知商品 ID"


def test_category_top_fallback_branch_is_actually_exercised(catalog):
    """确保样本数据仍然存在「不在 _cat_top 里」的类目 —— 否则上面的测试形同虚设。"""
    fallback_categories = [c for c in catalog.categories() if not catalog._cat_top.get(c)]
    assert fallback_categories, "样本数据已变：没有类目会走 fallback，本回归锁失效"
    for category in fallback_categories:
        ids = catalog.category_top(category, 3)
        assert ids, f"{category} 的 fallback 分支返回空"
        assert all(isinstance(i, str) for i in ids), f"{category} 的 fallback 返回了非字符串"


def test_category_top_result_is_hashable_membership_safe(catalog):
    """复现调用方写法：结果必须能安全地参与 set 成员判断。"""
    for category in catalog.categories():
        ids = catalog.category_top(category, 3)
        excluded: set[str] = set()
        result = [i for i in ids if i not in excluded][:3]  # 这里曾抛 TypeError
        assert isinstance(result, list)


def test_category_top_respects_k(catalog):
    assert len(catalog.category_top("家居装饰", 2)) <= 2


def test_global_top_returns_ids(catalog):
    ids = catalog.global_top(5)
    assert all(isinstance(i, str) for i in ids)
    assert all(catalog.exists(i) for i in ids)


def test_unknown_category_returns_empty(catalog):
    assert catalog.category_top("这个类目不存在", 5) == []
