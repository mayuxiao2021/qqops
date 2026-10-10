"""LLM 供应商抽象(对齐 AstrBot 的 `core/provider/`)。

为什么单独抽一层:
  * 总结、结构化抽取、智能查询都要调模型, 各自写一遍 urlopen 会到处不一致;
  * 统一做**超时 / 重试 / 用量与成本统计 / 失败记账 / 密钥脱敏**;
  * 将来接别的供应商(OpenAI 兼容、本地 Ollama、其它网关)只改这里。

配置(全局 `llm` 段)缺省回落到 `summarizer` 段 —— base_url / api_key / model 只维护一份,
避免"配了总结却没配查询"这种坑。

只依赖标准库;OpenAI 兼容的 `/chat/completions` 协议。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import errors

__all__ = ["LLM", "LLMResult", "from_config", "cost_of"]


@dataclass
class LLMResult:
    ok: bool = False
    text: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    cost: float = 0.0
    ms: int = 0
    error: str = ""
    rounds: int = 1

    @property
    def tokens_in(self) -> int:
        return int((self.usage or {}).get("prompt_tokens") or 0)

    @property
    def tokens_out(self) -> int:
        return int((self.usage or {}).get("completion_tokens") or 0)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "model": self.model,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cost": round(self.cost, 6), "ms": self.ms, "error": self.error,
                "rounds": self.rounds}


def cost_of(usage: dict, price_in: float = 0.0, price_out: float = 0.0) -> float:
    """按每百万 token 的单价估算成本(货币单位由配置决定)。"""
    return (int((usage or {}).get("prompt_tokens") or 0) / 1e6) * float(price_in or 0) + \
           (int((usage or {}).get("completion_tokens") or 0) / 1e6) * float(price_out or 0)


class LLM:
    """一个模型端点。线程安全(每次调用独立连接)。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: int = 120, temperature: float = 0.3, max_tokens: int = 1500,
                 retries: int = 2, price_in: float = 0.0, price_out: float = 0.0,
                 component: str = "llm"):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout = max(5, int(timeout))
        self.temperature = float(temperature)
        self.max_tokens = max(64, int(max_tokens))
        self.retries = max(1, int(retries))
        self.price_in = float(price_in or 0)
        self.price_out = float(price_out or 0)
        self.component = component

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    # ---------------------------------------------------------------- 调用
    def chat(self, messages: list, **kw) -> LLMResult:
        """发一轮对话。**不抛异常** —— 失败返回 ok=False 的结果并记账, 让调用方决定降级。"""
        if not self.configured:
            return LLMResult(ok=False, error="模型未配置(base_url / api_key / model 至少缺一项)")
        payload = {
            "model": kw.pop("model", None) or self.model,
            "temperature": (float(kw.pop("temperature")) if kw.get("temperature") is not None
                            else self.temperature),
            "messages": messages,
        }
        mt = kw.pop("max_tokens", None) or self.max_tokens
        if mt:
            payload["max_tokens"] = int(mt)
        payload.update({k: v for k, v in kw.items() if v is not None})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key})
        t0 = time.time()
        last = ""
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace") or "{}")
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                return LLMResult(ok=True, text=text, model=payload["model"], usage=usage,
                                 cost=cost_of(usage, self.price_in, self.price_out),
                                 ms=int((time.time() - t0) * 1000))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last = "HTTP %s %s" % (e.code, detail)
                if e.code in (400, 401, 403, 404):        # 配置类错误重试无意义
                    break
            except Exception as e:                        # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, e)
            if attempt + 1 < self.retries:
                time.sleep(min(2.0 * (attempt + 1), 8.0))
        errors.record("llm.%s" % self.component, kind="llm_failed",
                      message="调用模型失败(重试 %d 次): %s" % (self.retries, last),
                      extra={"model": self.model, "base_url": self.base_url})
        return LLMResult(ok=False, model=self.model, error=last,
                         ms=int((time.time() - t0) * 1000))

    def text(self, system: str, user: str, **kw) -> LLMResult:
        """便捷: 系统提示 + 用户内容。"""
        return self.chat([{"role": "system", "content": system},
                          {"role": "user", "content": user}], **kw)

    def describe(self) -> dict:
        return {"configured": self.configured, "model": self.model,
                "base_url": self.base_url, "timeout": self.timeout,
                "temperature": self.temperature, "max_tokens": self.max_tokens,
                "price_in": self.price_in, "price_out": self.price_out}


def from_config(cfg, section: str = "llm", component: str = "llm") -> LLM:
    """按全局配置构造。`llm` 段缺省时回落到 `summarizer` 段(密钥只维护一份)。"""
    data = {}
    try:
        data = (cfg.data if hasattr(cfg, "data") else cfg) or {}
    except Exception:
        data = {}
    llm_cfg = dict(data.get(section) or {})
    base = dict(data.get("summarizer") or {})
    pick = lambda k, d=None: llm_cfg.get(k) if llm_cfg.get(k) not in (None, "", 0) else base.get(k, d)
    return LLM(
        base_url=pick("base_url", ""),
        api_key=pick("api_key", ""),
        model=pick("model", ""),
        timeout=pick("timeout", 120),
        temperature=llm_cfg.get("temperature", 0.2),
        max_tokens=pick("max_tokens", 1500),
        retries=pick("retries", 2),
        price_in=pick("input_price_per_mtok", 0),
        price_out=pick("output_price_per_mtok", 0),
        component=component,
    )
