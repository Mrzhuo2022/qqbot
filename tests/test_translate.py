"""翻译插件测试：目标语言参数解析。"""

from src.plugins import translate as tr


def test_parse_target_auto():
    target, text = tr.parse_target("你好世界")
    assert target == tr.AUTO_TARGET
    assert text == "你好世界"


def test_parse_target_lang_code():
    target, text = tr.parse_target("en Hello world")
    assert target == "英语"
    assert text == "Hello world"


def test_parse_target_lang_code_case_insensitive_and_strip():
    target, text = tr.parse_target("  JA  おはよう ")
    assert target == "日语"
    assert text == "おはよう"


def test_parse_target_english_word_not_lang_code():
    # english 不是语言代码，整句按自动目标处理
    target, text = tr.parse_target("english text here")
    assert target == tr.AUTO_TARGET
    assert text == "english text here"


def test_parse_target_empty_is_usage():
    assert tr.parse_target("") is None
    assert tr.parse_target("   ") is None


def test_parse_target_lang_code_alone_is_usage():
    assert tr.parse_target("en") is None


# ── 自然语言触发解析（parse_nl_translate）───────────────────


def test_nl_translate_give_form():
    assert tr.parse_nl_translate("把早上好翻译成日语") == ("日语", "早上好")
    assert tr.parse_nl_translate("帮我把这个翻译成英语") == ("英语", "这个")
    assert tr.parse_nl_translate("给我把晚安翻译成法文") == ("法语", "晚安")
    assert tr.parse_nl_translate("把hello world翻译成中文") == ("简体中文", "hello world")
    assert tr.parse_nl_translate("把早上好翻成日语") == ("日语", "早上好")  # 单字动词


def test_nl_translate_colon_form():
    assert tr.parse_nl_translate("翻译成英语：hello") == ("英语", "hello")
    assert tr.parse_nl_translate("翻译日语：早上好") == ("日语", "早上好")
    assert tr.parse_nl_translate("翻译为韩语:good night") == ("韩语", "good night")


def test_nl_translate_rejects_non_imperative():
    assert tr.parse_nl_translate("这个翻译得真好") is None
    assert tr.parse_nl_translate("翻译成日语的方法") is None  # 无冒号不触发
    assert tr.parse_nl_translate("机器翻译成日语很难") is None
    assert tr.parse_nl_translate("/translate 你好") is None
    assert tr.parse_nl_translate("把这句翻译成日语吗？") is None
    assert tr.parse_nl_translate("") is None
