"""LLM 层测试：超时参数透传 / 超时异常转换 / 输出提取（mock openai 客户端，不调真实 API）。"""
from types import SimpleNamespace

import httpx
import openai
import pytest

import config
from agent.llm import build_deepseek_generator


class FakeCompletions:
    """记录 create 收到的 kwargs，并把返回值交给可编程 responder。"""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder()


class FakeClient:
    def __init__(self, responder):
        self.chat = SimpleNamespace(completions=FakeCompletions(responder))


def _ok(content: str = "SELECT 1"):
    """模拟 openai 正常响应：单 choice 携带 content。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


@pytest.fixture
def fake_openai(monkeypatch):
    """把 openai.OpenAI 换成 fake：捕获构造参数，completions 可编程。

    llm.py 里是函数内 `from openai import OpenAI` 延迟 import，
    因此 monkeypatch 模块属性 openai.OpenAI 即可拦截。
    """
    state = {"responder": None, "completions": None, "client_kwargs": None}

    def fake_constructor(**kwargs):
        state["client_kwargs"] = kwargs
        client = FakeClient(lambda: state["responder"]())
        state["completions"] = client.chat.completions
        return client

    monkeypatch.setattr(openai, "OpenAI", fake_constructor)
    return state


class TestLLMTimeout:
    def test_timeout_passed_to_sdk(self, fake_openai):
        # 护栏落点：SDK 的 timeout 同时约束连接与读取，必须是显式参数
        fake_openai["responder"] = _ok
        gen = build_deepseek_generator(api_key="k", base_url="https://x", model="m")
        gen("问题", "schema")

        kwargs = fake_openai["completions"].calls[0]
        assert kwargs["timeout"] == config.LLM_TIMEOUT_SECONDS

    def test_apitimeout_converted_to_runtime_error(self, fake_openai):
        # 超时 → 中文 RuntimeError：graph.py 只认普通异常走统一重试链路
        def raise_timeout():
            req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
            raise openai.APITimeoutError(request=req)

        fake_openai["responder"] = raise_timeout
        gen = build_deepseek_generator()  # fake 构造器不校验 api_key，不会真连网
        with pytest.raises(RuntimeError, match="超时"):
            gen("问题", "schema")

    def test_normal_response_extracts_sql(self, fake_openai):
        # 超时改造不破坏正常链路：markdown 代码块照常被 _extract_sql 剥掉
        fake_openai["responder"] = lambda: _ok("```sql\nSELECT 1\n```")
        gen = build_deepseek_generator()
        assert gen("问题", "schema") == "SELECT 1"


class TestGeneratorParams:
    def test_sdk_constructed_from_explicit_params(self, fake_openai):
        # 显式参数优先于 config（build_deepseek_generator 的设计意图）
        fake_openai["responder"] = _ok
        gen = build_deepseek_generator(
            api_key="k1", base_url="https://b1", model="m1"
        )
        gen("a", "b")

        ck = fake_openai["client_kwargs"]
        assert ck["api_key"] == "k1"
        assert ck["base_url"] == "https://b1"
        assert fake_openai["completions"].calls[0]["model"] == "m1"