"""联网搜索插件的触发判定、搜索源适配与格式化测试（HTTP 用 FakeClient 打桩）。"""

import asyncio

import src.plugins.search as sp
from src.plugins.search import (
    _bocha,
    _enabled_providers,
    _extract_query,
    _firecrawl,
    _format_results,
    _format_sources,
    _hit_wake_word,
    _jina,
    _tavily,
    search_web,
)
from src.shared.config import plugin_config


def _patch_keys(monkeypatch, enabled: list[str]):
    for field in sp.KEY_FIELDS.values():
        monkeypatch.setattr(plugin_config, field, "")
    for name in enabled:
        monkeypatch.setattr(plugin_config, sp.KEY_FIELDS[name], "test-key")


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """url 关键词 → payload 或 Exception；capture 记录最后一次请求。"""

    def __init__(self, routes, capture=None):
        self._routes = routes
        self.capture = capture

    async def post(self, url, headers=None, json=None):
        if self.capture is not None:
            self.capture.update({"url": url, "headers": headers, "json": json})
        for k, v in self._routes.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return FakeResp(v)
        raise AssertionError(f"unexpected url: {url}")


# ── 触发词与查询提取 ──────────────────────────────────────────────

def test_hit_wake_word():
    assert _hit_wake_word("联网搜一下 Rust 的新版本")
    assert _hit_wake_word("帮我搜 三体 讲的什么")
    # 裸「搜索」刻意不在默认词表：群里聊「搜索引擎」不该触发
    assert not _hit_wake_word("搜索引擎哪家好")


def test_extract_query():
    assert _extract_query("帮我搜一下 Rust 版本") == "Rust 版本"
    assert _extract_query("联网 Rust 版本") == "Rust 版本"
    assert _extract_query("搜索一下xx最新消息") == "xx最新消息"
    # 触发词后面没词就用全文
    assert _extract_query("联网") == "联网"
    assert _extract_query("帮我搜") == "帮我搜"


# ── 可用源：按配置顺序，只留填了 key 的 ──────────────────────────

def test_enabled_providers(monkeypatch):
    _patch_keys(monkeypatch, ["tavily"])
    assert _enabled_providers() == ["tavily"]

    _patch_keys(monkeypatch, ["jina", "bocha"])
    assert _enabled_providers() == ["bocha", "jina"]  # 跟 provider_order 顺序

    _patch_keys(monkeypatch, [])
    assert _enabled_providers() == []


# ── 四家搜索源适配 ────────────────────────────────────────────────

def test_tavily_adapter(monkeypatch):
    monkeypatch.setattr(plugin_config, "search_max_results", 5)
    capture = {}
    routes = {
        "api.tavily.com": {
            "results": [
                {"title": "Rust 1.85 发布", "url": "https://rust.example/1",
                 "content": "首先说明要点", "score": 0.9},
                {"title": "", "url": "https://x.example", "content": "无标题丢弃"},
            ]
        }
    }
    results = asyncio.run(_tavily(FakeClient(routes, capture), "test-key", "Rust 1.85"))
    assert capture["headers"]["Authorization"] == "Bearer test-key"
    assert capture["json"]["query"] == "Rust 1.85"
    assert capture["json"]["max_results"] == 5
    assert capture["json"]["search_depth"] == "basic"
    assert results == [
        {"title": "Rust 1.85 发布", "url": "https://rust.example/1", "content": "首先说明要点"}
    ]


def test_bocha_adapter():
    routes = {
        "api.bochaai.com": {
            "data": {
                "webPages": {
                    "value": [
                        {"name": "博查结果", "url": "https://bocha.example/1",
                         "summary": "这是长摘要"},
                        {"name": "没摘要的", "url": "https://bocha.example/2",
                         "snippet": "兜底用snippet"},
                    ]
                }
            }
        }
    }
    capture = {}
    results = asyncio.run(_bocha(FakeClient(routes, capture), "test-key", "天气"))
    assert capture["json"]["summary"] is True
    assert results[0] == {
        "title": "博查结果", "url": "https://bocha.example/1", "content": "这是长摘要"
    }
    assert results[1]["content"] == "兜底用snippet"  # summary 缺失落 snippet


def test_firecrawl_adapter(monkeypatch):
    monkeypatch.setattr(plugin_config, "search_max_results", 3)
    routes = {
        "api.firecrawl.dev": {
            "data": {
                "web": [
                    {"title": "FC 结果", "url": "https://fc.example/1",
                     "description": "描述性内容"},
                ]
            }
        }
    }
    capture = {}
    results = asyncio.run(_firecrawl(FakeClient(routes, capture), "test-key", "查询词"))
    assert capture["json"]["sources"] == ["web"]
    assert capture["json"]["limit"] == 3
    assert "scrapeOptions" not in capture["json"]  # 不带抓取，省 credits
    assert results == [
        {"title": "FC 结果", "url": "https://fc.example/1", "content": "描述性内容"}
    ]


def test_jina_adapter():
    long_content = "x" * 1000
    routes = {
        "s.jina.ai": {
            "data": [
                {"title": "Jina 结果", "url": "https://jina.example/1",
                 "description": "摘要", "content": long_content},
                {"title": "只有摘要", "url": "https://jina.example/2",
                 "description": "兜底描述"},
            ]
        }
    }
    results = asyncio.run(_jina(FakeClient(routes), "test-key", "查询词"))
    assert results[0]["content"] == "x" * sp.CONTENT_MAX_CHARS  # content 优先且截断
    assert results[1]["content"] == "兜底描述"


# ── search_web：多源兜底 ──────────────────────────────────────────

def test_search_web_falls_back_on_error(monkeypatch):
    _patch_keys(monkeypatch, ["bocha", "jina"])
    routes = {
        "api.bochaai.com": RuntimeError("503"),
        "s.jina.ai": {"data": [{"title": "兜底成功", "url": "https://j.example",
                                 "content": "内容"}]},
    }
    results, provider, errored = asyncio.run(search_web(FakeClient(routes), "查询词"))
    assert provider == "jina" and not errored  # 兜底成功不算出错
    assert results[0]["title"] == "兜底成功"


def test_search_web_falls_back_on_empty(monkeypatch):
    _patch_keys(monkeypatch, ["tavily", "bocha"])
    routes = {
        "api.tavily.com": {"results": []},
        "api.bochaai.com": {"data": {"webPages": {"value": [
            {"name": "空了就换源", "url": "https://b.example", "summary": "s"},
        ]}}},
    }
    results, provider, errored = asyncio.run(search_web(FakeClient(routes), "查询词"))
    assert provider == "bocha" and not errored  # 有源正常只是没结果
    assert len(results) == 1


def test_search_web_all_failed(monkeypatch):
    _patch_keys(monkeypatch, ["tavily"])
    routes = {"api.tavily.com": RuntimeError("超时")}
    results, provider, errored = asyncio.run(search_web(FakeClient(routes), "查询词"))
    assert (results, provider) == ([], "")
    assert errored  # 全部异常：提示「服务连不上」而不是「没搜到」


# ── 格式化 ────────────────────────────────────────────────────────

RESULTS = [
    {"title": "第一条", "url": "https://a.example/1", "content": "内容一"},
    {"title": "第二条", "url": "", "content": "内容二"},
]


def test_format_results():
    text = _format_results(RESULTS)
    assert "[1] 第一条" in text and "https://a.example/1" in text and "内容一" in text
    assert "[2] 第二条" in text


def test_format_sources():
    # 来源只贴真实链接，无链接的条目不出现，也没有编造的占位
    assert _format_sources(RESULTS) == "🔗 来源：\nhttps://a.example/1"
    assert _format_sources([RESULTS[1]]) == ""


# ── _wants_search 规则 ────────────────────────────────────────────

class PlainEv:
    def __init__(self, text):
        self._t = text

    def get_plaintext(self):
        return self._t


def test_wants_search(monkeypatch):
    _patch_keys(monkeypatch, [])
    assert not asyncio.run(sp._wants_search(PlainEv("联网搜一下 Rust")))  # 没配 key 不触发

    _patch_keys(monkeypatch, ["tavily"])
    assert asyncio.run(sp._wants_search(PlainEv("联网搜一下 Rust")))
    assert not asyncio.run(sp._wants_search(PlainEv("/search Rust")))  # 斜杠让位命令
    assert not asyncio.run(sp._wants_search(PlainEv("今天天气不错")))
    assert not asyncio.run(sp._wants_search(PlainEv("")))


def test_wants_search_respects_group_switch(monkeypatch):
    _patch_keys(monkeypatch, ["tavily"])

    class GroupEv(PlainEv):
        to_me = True

    # 假群事件：isinstance 检查走 sp 里导入的名字，替换后可控群级开关分支
    monkeypatch.setattr(sp, "GroupMessageEvent", GroupEv)
    monkeypatch.setattr(sp, "group_plugin_enabled", lambda ev, p: False)
    assert not asyncio.run(sp._wants_search(GroupEv("联网搜一下 Rust")))
    monkeypatch.setattr(sp, "group_plugin_enabled", lambda ev, p: True)
    assert asyncio.run(sp._wants_search(GroupEv("联网搜一下 Rust")))


def test_wants_search_requires_tome_in_group(monkeypatch):
    _patch_keys(monkeypatch, ["tavily"])

    class GroupEv(PlainEv):
        def __init__(self, text, to_me=True):
            super().__init__(text)
            self.to_me = to_me

    monkeypatch.setattr(sp, "GroupMessageEvent", GroupEv)
    monkeypatch.setattr(sp, "group_plugin_enabled", lambda ev, p: True)
    # 群聊没 @ bot（to_me=False）不触发，哪怕消息带触发词
    assert not asyncio.run(sp._wants_search(GroupEv("等我搜一下哈", to_me=False)))
    assert asyncio.run(sp._wants_search(GroupEv("帮我搜 Rust 版本", to_me=True)))
    # 私聊（非 GroupMessageEvent）不受 to_me 检查影响
    assert asyncio.run(sp._wants_search(PlainEv("帮我搜 Rust 版本")))


# ── /search 点名搜索源 ────────────────────────────────────────────

def test_split_provider():
    assert sp._split_provider("tavily Rust 版本") == ("tavily", "Rust 版本")
    assert sp._split_provider("博查 天气") == ("bocha", "天气")
    assert sp._split_provider("FC Rust") == ("firecrawl", "Rust")  # 大小写/别名
    assert sp._split_provider("Rust 版本") == (None, "Rust 版本")  # 没点名
    # 只有源名没有查询词：整串当查询词（用户可能就想搜这个词）
    assert sp._split_provider("tavily") == (None, "tavily")
    assert sp._split_provider("博查天气") == (None, "博查天气")  # 无空格不拆


# ── chat 唤醒词触发搜索 ───────────────────────────────────────────

def test_wake_prefix(monkeypatch):
    monkeypatch.setattr(plugin_config, "chat_wake_words", ["小助手", "小"])
    assert sp._wake_prefix_hit("小助手 联网搜 Rust")
    assert sp._wake_prefix_hit("小搜索一下")
    assert not sp._wake_prefix_hit("帮我搜 Rust")  # 唤醒词必须开头
    assert not sp._wake_prefix_hit("")

    # 多个命中取最长；没配唤醒词原样返回
    assert sp._strip_wake_prefix("小助手 帮我搜 Rust") == "帮我搜 Rust"
    assert sp._strip_wake_prefix("帮我搜 Rust") == "帮我搜 Rust"
    monkeypatch.setattr(plugin_config, "chat_wake_words", [])
    assert sp._strip_wake_prefix("小助手 帮我搜 Rust") == "小助手 帮我搜 Rust"


def test_search_query_strips_wake_prefix(monkeypatch):
    monkeypatch.setattr(plugin_config, "chat_wake_words", ["小助手"])
    ev = PlainEv("小助手 帮我搜一下 Rust 版本")
    assert sp._search_query(ev) == "Rust 版本"
    monkeypatch.setattr(plugin_config, "chat_wake_words", [])
    assert sp._search_query(PlainEv("帮我搜一下 Rust 版本")) == "Rust 版本"


def test_wants_search_via_wake_prefix(monkeypatch):
    _patch_keys(monkeypatch, ["tavily"])
    monkeypatch.setattr(plugin_config, "chat_wake_words", ["小助手"])

    class GroupEv(PlainEv):
        def __init__(self, text, to_me=False):
            super().__init__(text)
            self.to_me = to_me

    monkeypatch.setattr(sp, "GroupMessageEvent", GroupEv)
    monkeypatch.setattr(sp, "group_plugin_enabled", lambda ev, p: True)
    # 唤醒词开头（免 @）+ 触发词 → 触发；没唤醒（to_me=False 且无唤醒词）→ 不触发
    assert asyncio.run(sp._wants_search(GroupEv("小助手 联网搜 Rust")))
    assert not asyncio.run(sp._wants_search(GroupEv("帮我搜 Rust")))
    # 唤醒词开头但没有搜索触发词也不触发（留给 chat）
    assert not asyncio.run(sp._wants_search(GroupEv("小助手 今天天气怎么样")))
