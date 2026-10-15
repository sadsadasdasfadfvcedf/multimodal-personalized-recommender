"""
chat.py —— 多模态个性化推荐助手 · 交互式对话终端
==================================================

在终端里和系统真正对话（不是跑脚本）。三种输入方式：

    直接打字            「500以内的婚礼装饰品」        → 文本对话
    /voice [秒数]       录一段真实麦克风 → ASR 识别   → 语音对话（需 real 模式）
    /gesture            抓一帧画面 → 手势识别          → 手势对话（👍/👎 即正负反馈）
    /vision [问题]      抓一帧画面 → VL 描述           → 视觉对话

会话命令：

    /undo        撤销上一轮（回滚槽位与反馈）
    /reset       结束当前会话并新开一个（清空偏好）
    /state       查看当前会话状态（槽位、点赞、倒赞）
    /llm on|off  切换真实 LLM 生成解释（默认关；开启后每轮约 +10~25 秒）
    /say on|off  切换语音播报
    /mode        查看当前适配器模式
    /help        帮助
    /quit        退出

用法：
    python chat.py                      # 默认（离线、无语音播报、无 LLM）
    python chat.py --voice              # 开启语音播报
    python chat.py --real               # 真实多模态（麦克风/摄像头/真实模型）
    python chat.py --real --llm         # 真实多模态 + 真实 LLM 解释
    python chat.py --user 14156         # 指定用户（15547 林先生 / 14156 陈小姐 / 17231 王女士）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.utils import now_iso  # noqa: E402

HELP = """可用输入：
  直接打字             例如「500以内的婚礼装饰品」「和这个相似的商品」「为什么推荐第一个」
  /voice [秒数]        录麦克风并识别（默认 4 秒；需 --real）
  /gesture             抓帧识别手势，👍→点赞、👎→倒赞
  /vision [问题]       抓帧做视觉理解，把描述作为视觉标签送进对话
会话命令：
  /undo /reset /state /llm on|off /say on|off /mode /help /quit"""


class Chat:
    def __init__(self, user_id: str, mode: str | None, with_voice: bool, use_llm: bool,
                 allow_real: bool | None = None) -> None:
        self.user_id = user_id
        # allow_real 必须显式传参：settings.RUNTIME 在 import 时已求值，
        # 之后再改环境变量不会生效。
        self.orch = Orchestrator(mode=mode, allow_real=allow_real,
                                 with_voice=with_voice, use_llm=use_llm)
        self.session_id = self.orch.store.default_session_id(user_id)
        self.with_voice = with_voice
        self.media_dir = settings.PATHS["artifacts"] / "chat"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._turn = 0

    # ---------------- 展示 ----------------
    def _print_response(self, resp: dict) -> None:
        status = resp.get("status")
        intent = (resp.get("intent_result") or {}).get("intent") or "-"
        icons = {"ok": "✅", "degraded": "🟡", "clarification": "❓",
                 "conflict": "⚠️", "empty": "⬜", "no_evidence": "🚫", "error": "❌"}
        print(f"\n{icons.get(status, '·')} [{status}] 意图={intent}",
              f"| 推荐适配器={resp.get('trace', {}).get('modes', {}).get('recommender')}")

        if resp.get("error"):
            print(f"   {resp['error'].get('code')}: {resp['error'].get('message')}")
        if resp.get("clarification"):
            print(f"   → {resp['clarification']}")
        ir = resp.get("intent_result") or {}
        if ir.get("slots"):
            print(f"   槽位: {[v.get('value') for v in ir['slots'].values()]}")

        for index, item in enumerate(resp.get("recommendations", [])[:5], start=1):
            price = f"£{item['price']}" if item.get("price") is not None else "—"
            rank = item.get("rank") or index
            score = item.get("score")
            print(f"   {rank}. {item.get('title')}  {price}  "
                  f"分={score if score is not None else '—'}")
            if item.get("explanation"):
                print(f"      {item['explanation']}")
        if resp.get("feedback"):
            fb = resp["feedback"]
            print(f"   反馈已记录: {fb.get('polarity')} → {fb.get('target_title')}")
        if resp.get("before_after"):
            ba = resp["before_after"]
            print(f"   排序变化: {'是' if ba.get('changed') else '否'}"
                  f"  before={ba.get('before_topk', [])[:3]}  after={ba.get('after_topk', [])[:3]}")
        pres = resp.get("presentation") or {}
        if pres.get("voice_available"):
            print(f"   🔊 语音: {pres.get('audio_path')}")
        lat = (resp.get("trace") or {}).get("latency_ms") or {}
        print(f"   ⏱  {lat.get('total_ms')} ms")

    def _send(self, spec: dict) -> None:
        self._turn += 1
        result = self.orch.handle(spec, user_id=self.user_id, session_id=self.session_id)
        if not result.ok:
            print(f"❌ 处理失败: {result.error}")
            return
        if result.data.get("control"):
            print(f"↩️  会话控制: {result.data['control'].get('action')}")
            return
        self._print_response(result.data)

    # ---------------- 输入方式 ----------------
    def _voice_input(self, seconds: float) -> None:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            print("⚠️  语音输入需要 --real 模式（否则无法访问麦克风）")
            return
        wav = self.media_dir / f"chat_mic_{self._turn}.wav"
        print(f"🎙️  录音 {seconds} 秒… 请说话")
        rec = ad.record_audio(str(wav), seconds=seconds)
        if not rec.ok:
            print(f"❌ 录音失败: {rec.error}")
            return
        print("⏳ 识别中…")
        asr = ad.transcribe(str(wav))
        if not asr.ok:
            print(f"❌ 识别失败: {asr.error}")
            return
        text = (asr.data or {}).get("text") or ""
        print(f"🗣️  识别结果：「{text}」")
        if not text.strip():
            print("   （未识别到有效语音，本轮跳过）")
            return
        self._send({"source": "voice", "text": text})

    def _gesture_input(self) -> None:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            print("⚠️  手势输入需要 --real 模式")
            return
        frame = self.media_dir / f"chat_frame_{self._turn}.jpg"
        cap = ad.capture_frame(str(frame))
        if not cap.ok:
            print(f"❌ 抓帧失败: {cap.error}")
            return
        print("⏳ 手势识别中…")
        g = ad.recognize_gesture(str(frame))
        if not g.ok:
            print(f"❌ 识别失败: {g.error}")
            return
        data = g.data or {}
        print(f"✋ 检测到手 {data.get('hands_detected')} 只，手势 {data.get('gestures') or '无'}")
        if not data.get("gestures"):
            print(f"   {data.get('note') or '未识别到已定义手势，本轮跳过'}")
            return
        g0 = data["gestures"][0]
        self._send({"source": "gesture",
                    "gesture": {"name": g0["name"], "confidence": g0["confidence"]}})

    def _vision_input(self, question: str) -> None:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            print("⚠️  视觉输入需要 --real 模式")
            return
        frame = self.media_dir / f"chat_frame_{self._turn}.jpg"
        cap = ad.capture_frame(str(frame))
        if not cap.ok:
            print(f"❌ 抓帧失败: {cap.error}")
            return
        print("⏳ 视觉理解中…（约 20-40 秒）")
        vl = ad.describe_image(str(frame), question or None)
        if not vl.ok:
            print(f"❌ 视觉理解失败: {vl.error}")
            return
        text = (vl.data or {}).get("response") or ""
        print(f"👁️  画面: 「{text}」")
        self._send({"source": "vision", "text": question or "",
                    "visual_tags": [{"tag": text[:24], "confidence": 0.85}]})

    # ---------------- 主循环 ----------------
    def run(self) -> int:
        m = self.orch.modes()
        print("=" * 74)
        print("多模态个性化推荐助手 · 交互式对话")
        print("=" * 74)
        print(f"用户={self.user_id}  会话={self.session_id}")
        print(f"多模态适配器={m['multimodal']}  推荐适配器={m['recommender']}  "
              f"LLM解释={m.get('llm_explain')}  语音播报={'开' if self.with_voice else '关'}")
        if m["multimodal"] != "real":
            print("提示：加 --real 可启用麦克风/摄像头/真实模型（/voice、/gesture、/vision 需要它）")
        print("输入 /help 看用法，/quit 退出。\n")

        while True:
            try:
                line = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return 0
            if not line:
                continue

            if line.startswith("/"):
                parts = line[1:].split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""
                if cmd in ("quit", "exit", "q"):
                    print("再见。")
                    return 0
                if cmd == "help":
                    print(HELP)
                    continue
                if cmd == "mode":
                    print(self.orch.modes())
                    continue
                if cmd == "state":
                    sess = self.orch.store.get(self.session_id)
                    if sess is None:
                        print("会话不存在")
                        continue
                    snap = sess.snapshot()
                    print(f"轮次={snap['turns']} 点赞={snap['likes']} 倒赞={snap['dislikes']}")
                    print(f"槽位={ {k: v['value'] for k, v in snap['slot_state'].items()} }")
                    print(f"焦点商品={snap['focus_item_id']}")
                    continue
                if cmd == "undo":
                    self._send({"source": "text", "text": "撤销"})
                    continue
                if cmd == "reset":
                    self._send({"source": "text", "text": "重新开始"})
                    self.session_id = f"{self.session_id}-{now_iso()[:19]}"
                    print(f"已开启新会话: {self.session_id}")
                    continue
                if cmd == "llm":
                    on = arg.lower() in ("on", "1", "true", "yes")
                    self.orch.llm_explainer.enabled = on
                    if on and self.orch.multimodal.active_mode != "real":
                        print("⚠️  LLM 需要 --real 模式才生效（当前是离线模式）")
                    print(f"LLM 解释已{'开启' if on else '关闭'}"
                          f"（开启后每条约 +10~25 秒，因为每次都要重新加载模型）")
                    continue
                if cmd == "say":
                    on = arg.lower() in ("on", "1", "true", "yes")
                    self.with_voice = on
                    self.orch.with_voice = on
                    print(f"语音播报已{'开启' if on else '关闭'}")
                    continue
                if cmd == "voice":
                    seconds = float(arg) if arg else 4.0
                    self._voice_input(seconds)
                    continue
                if cmd == "gesture":
                    self._gesture_input()
                    continue
                if cmd == "view":
                    if not arg:
                        target = self.orch.store.get(self.session_id)
                        target = target.focus_item_id if target else None
                        if not target:
                            print("用法：/view <商品编号>（如 /view 20829）")
                            continue
                        arg = target
                    self._send({"source": "browse", "item_id": arg})
                    continue
                if cmd == "vision":
                    self._vision_input(arg)
                    continue
                print(f"未知命令 /{cmd}，输入 /help 查看用法")
                continue

            self._send({"source": "text", "text": line})


def main() -> int:
    parser = argparse.ArgumentParser(description="交互式对话终端")
    parser.add_argument("--user", default="15547", help="用户 ID（15547/14156/17231）")
    parser.add_argument("--mode", default=None, choices=["offline", "auto", "online"])
    parser.add_argument("--voice", action="store_true", help="开启语音播报")
    parser.add_argument("--real", action="store_true", help="真实多模态（麦克风/摄像头/真实模型）")
    parser.add_argument("--llm", action="store_true", help="用真实 LLM 生成解释（较慢）")
    args = parser.parse_args()

    settings.ensure_dirs()
    mode = args.mode or ("auto" if args.real else None)
    allow_real = True if args.real else None
    if args.real:
        os.environ.setdefault("INTEGRATED_DEMO_USER", "15547")
    return Chat(args.user, mode, args.voice, args.llm, allow_real).run()


if __name__ == "__main__":
    raise SystemExit(main())
