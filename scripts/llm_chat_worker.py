"""
scripts/llm_chat_worker.py
==========================

常驻对话 worker：**一次加载模型，多轮复用**。

为什么需要
----------
`multimodal_ai` 的 `TextLLM` 每次构造都会重新加载 Qwen3-4B（约 25s）。
如果每条聊天都起一个新进程，用户每句话都要等 25 秒，谈不上「正常对话」。

协议
----
stdin 每行一个 JSON：{"id": 1, "text": "..."} 或 {"id": 2, "op": "reset"}
stdout 每行一个 JSON：{"event": "ready"} / {"id": 1, "ok": true, "text": "..."}
（库自身的日志全部重定向到 stderr，避免污染协议）

由 `src/llm_chat.py` 以 cwd=multimodal_ai 根目录启动。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# multimodal_ai 的 TextLLM 默认系统提示词是给「国脉大学养老」知识库用的：
# 只要传入 context 就会要求模型在结尾注明「（信息来源：国脉大学养老资料库）」。
# 本项目的开放问答必须用自己的约束，且不能把系统说明当知识库塞进去。
INTEGRATED_SYSTEM_PROMPT = """你是「多模态个性化推荐助手」的对话引擎，运行在一个课程项目中。

回答原则:
1. 用简洁、自然的中文直接回答问题，默认控制在 150 字以内。
2. 不要编造来源、文献、链接、统计数据或商品信息；不确定就如实说明。
3. 与商品推荐无关的问题就事论事回答，不要强行推荐商品。
4. 用户要求写文案、解释概念、翻译或计算时，按需求给出结果。
"""


def _chat_wrap(llm, prompt: str) -> str:
    """把解释提示词套上模型 chat template；没有模板时原样返回。"""
    tokenizer = getattr(llm, "tokenizer", None)
    if tokenizer is None or not getattr(tokenizer, "chat_template", None):
        return prompt
    messages = [{"role": "user", "content": prompt}]
    try:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    except Exception:
        return prompt


def main() -> int:
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    # 复制一份真实的 stdout 作为协议通道，其余 print 全部走 stderr
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr

    root = params.get("root")
    if not root:
        proto.write(json.dumps({"event": "error", "error": "missing root"}) + "\n")
        proto.flush()
        return 2
    sys.path.insert(0, root)

    from src.text_llm import TextLLM

    llm = TextLLM(model_path=params.get("model_path"))
    llm.SYSTEM_PROMPT = INTEGRATED_SYSTEM_PROMPT
    try:
        llm.chat("你好")  # 触发加载 + 预热
        llm.clear_history()
    except Exception as exc:
        proto.write(json.dumps({"event": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
        proto.flush()
        return 1

    proto.write(json.dumps({"event": "ready"}) + "\n")
    proto.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if req.get("op") == "reset":
            llm.clear_history()
            out = {"id": req.get("id"), "ok": True, "text": ""}
        elif req.get("op") == "prompts":
            prompts = req.get("prompts") or []
            outputs = []
            for prompt in prompts:
                wrapped = _chat_wrap(llm, str(prompt))
                text = llm.generate(
                    wrapped,
                    max_new_tokens=int(req.get("max_new_tokens", 120)),
                    temperature=float(req.get("temperature", 0.2)),
                )
                outputs.append({"text": (text or "").strip()})
            out = {
                "id": req.get("id"),
                "ok": True,
                "outputs": outputs,
                "model": Path(llm.model_path).name,
            }
        else:
            try:
                # 不把 context 传给 TextLLM：它会把 context 当成外部知识库并
                # 追加固定来源标注。用户/系统信息已经在 SYSTEM_PROMPT 中约束。
                text = llm.chat(str(req.get("text") or ""))
                out = {"id": req.get("id"), "ok": True, "text": text}
            except Exception as exc:
                out = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps(out, ensure_ascii=False) + "\n")
        proto.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
