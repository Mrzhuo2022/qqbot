"""合并转发节点文本提取测试。"""

from src.plugins.forward_parser import extract_transcript


def _node(nickname, text):
    return {
        "sender": {"nickname": nickname},
        "content": [{"type": "text", "data": {"text": text}}],
    }


def test_extract_basic_transcript():
    nodes = [_node("张三", "晚上吃火锅吗"), _node("李四", "走！老地方")]
    transcript = extract_transcript(nodes)
    assert "张三：晚上吃火锅吗" in transcript
    assert "李四：走！老地方" in transcript


def test_extract_placeholders_for_media():
    node = {
        "sender": {"nickname": "王五"},
        "content": [
            {"type": "image", "data": {"url": "http://x/y.jpg"}},
            {"type": "forward", "data": {"id": "abc"}},
            {"type": "text", "data": {"text": "看图"}},
        ],
    }
    transcript = extract_transcript([node])
    assert "[图片]" in transcript
    assert "[嵌套转发]" in transcript
    assert "王五：[图片] [嵌套转发] 看图" in transcript


def test_extract_skips_empty_and_face_only_nodes():
    nodes = [
        {"sender": {"nickname": "A"}, "content": [{"type": "face", "data": {"id": "1"}}]},
        _node("B", "真实内容"),
    ]
    transcript = extract_transcript(nodes)
    assert "A：" not in transcript
    assert "B：真实内容" in transcript


def test_extract_caps_node_count():
    nodes = [_node(f"user{i}", f"msg{i}") for i in range(60)]
    transcript = extract_transcript(nodes)
    assert "msg59" not in transcript
    assert "共 60 条，已截断" in transcript


def test_extract_tolerates_malformed_nodes():
    transcript = extract_transcript([None, "junk", _node("A", "ok")])
    assert "A：ok" in transcript


def test_extract_napcat_message_key():
    """NapCat/Lagrange 返回 OB11Message，段落挂在 message 上而不是 content。"""
    node = {
        "user_id": 10001,
        "sender": {"nickname": "小明", "card": ""},
        "message": [{"type": "text", "data": {"text": "NapCat 的转发正文"}}],
    }
    transcript = extract_transcript([node])
    assert "小明：NapCat 的转发正文" in transcript


def test_extract_segment_style_node():
    """有的实现把节点包成 {type: node, data: {...}} 段。"""
    node = {
        "type": "node",
        "data": {
            "user_id": 10002,
            "nickname": "小红",
            "message": [{"type": "text", "data": {"text": "段式节点内容"}}],
        },
    }
    transcript = extract_transcript([node])
    assert "小红：段式节点内容" in transcript


def test_extract_segment_style_content_key():
    node = {
        "type": "node",
        "data": {
            "nickname": "阿强",
            "content": [{"type": "text", "data": {"text": "data.content 里的内容"}}],
        },
    }
    transcript = extract_transcript([node])
    assert "阿强：data.content 里的内容" in transcript


def test_extract_wrapped_string_segment():
    node = {"sender": {"nickname": "C"}, "content": ["字符串段落"]}
    transcript = extract_transcript([node])
    assert "C：字符串段落" in transcript
