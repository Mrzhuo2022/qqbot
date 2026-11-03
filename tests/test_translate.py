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
