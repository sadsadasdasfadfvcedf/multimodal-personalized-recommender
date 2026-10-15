"""adapters 包：与「推荐系统」「智能多模态AI助手」两个外部系统的唯一接口层。

约定（阶段1 验收项）：
    两套系统不直接读取对方内部变量，只能通过本包暴露的适配器交互。
"""

from .multimodal_adapter import MultimodalAdapter
from .recommender_adapter import RecommenderAdapter

__all__ = ["MultimodalAdapter", "RecommenderAdapter"]
