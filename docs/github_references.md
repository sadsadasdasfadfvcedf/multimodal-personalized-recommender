# GitHub 参考项目与采用情况

调研时间：2026-10-19。只借鉴架构与产品思路，**不复制代码**。

| 项目 | 星标（调研时） | 核心内容 | 本项目采用 |
|---|---:|---|---|
| [enoche/MultimodalRecSys](https://github.com/enoche/MultimodalRecSys) | 473 | 多模态推荐资源汇总；文本/图像/通用模型分类；MMRec 统一框架 | 把「图像相似」作为独立通道，而不是混进个性化排序分 |
| [Jinfeng-Xu/Awesome-Multimodal-Recommender-Systems](https://github.com/Jinfeng-Xu/Awesome-Multimodal-Recommender-Systems) | 366 | 持续更新的多模态推荐论文列表 | 用于确认视觉/文本模态常作为独立召回或辅助特征 |
| [westlake-repl/IDvs.MoRec](https://github.com/westlake-repl/IDvs.MoRec) | 169 | ID-based vs modality-based 推荐；视觉/文本编码器；HR@10/NDCG@10 | 明确「模态相似 ≠ 个性化精排」的边界，视觉结果单独标注 |
| [cap-ntu/Video-to-Retail-Platform](https://github.com/cap-ntu/Video-to-Retail-Platform) | 147 | 视频/商品/广告分析；按图像和文本搜索；内容-商品匹配 | 新增商品详情「找相似外观」，按图片颜色/纹理返回视觉相似商品 |

## 本次落地

1. `src/visual_similarity.py`
   - HSV 3D 颜色直方图（8×8×4）+ dHash（64 bit）；
   - 相似度 = 0.75×直方图余弦 + 0.25×dHash 相似度；
   - 签名缓存到 `artifacts/visual_signatures.json`，artifacts 不进提交包。
2. `GET /api/visual-similar/{item_id}?k=8`
   - 候选池：同品类热门 + 全局热门；
   - **只使用本地已缓存的商品图**，避免为 8 件结果串行下载几十张图；
   - 实测 20 张候选、0.68 秒返回。
3. 商品详情新增「🖼 找相似外观」按钮和「视觉相似（按图片外观）」区块，
   显示相似度百分比，并明确标注「不参与个性化精排」。

## 未采用

- 未直接引入 VBPR / CLIP / ResNet 训练代码：需要权重与 GPU，会破坏本项目
  「默认离线、零新增重型依赖」的约束。`compute_signature` 接口已隔离，后续可替换为
  CLIP embedding，而不改上层 API。
- 未引入 Hysia 的视频/广告插入流水线：与本项目的商品推荐场景不匹配。
