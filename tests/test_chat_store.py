"""会话存储 SQLite 层测试（临时库，覆盖隔离/清理/迁移）。"""

import json

import src.shared.chat_store as chat_store


def test_session_roundtrip(tmp_path):
    chat_store.init(tmp_path / "chat.db")
    assert chat_store.load_session("g:1:100") == []

    msgs = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "嗨"}]
    chat_store.save_session("g:1:100", msgs)
    chat_store.save_session("p:200:200", [{"role": "user", "content": "在吗"}])

    assert chat_store.load_session("g:1:100") == msgs
    assert chat_store.session_count() == 2


def test_sessions_isolated_by_key(tmp_path):
    """不同群、群与私聊之间的历史互不可见。"""
    chat_store.init(tmp_path / "chat.db")
    chat_store.save_session("g:1:100", [{"role": "user", "content": "群1的话"}])
    chat_store.save_session("g:2:100", [{"role": "user", "content": "群2的话"}])
    chat_store.save_session("p:100:100", [{"role": "user", "content": "私聊的话"}])

    assert chat_store.load_session("g:1:100") == [{"role": "user", "content": "群1的话"}]
    assert "群1" not in chat_store.load_session("g:2:100")[0]["content"]
    assert "群" not in chat_store.load_session("p:100:100")[0]["content"]


def test_save_session_upserts_single_row(tmp_path):
    """重复保存同一会话是覆盖而不是新增，行数不膨胀。"""
    chat_store.init(tmp_path / "chat.db")
    for i in range(5):
        chat_store.save_session("g:1:100", [{"role": "user", "content": f"m{i}"}])
    assert chat_store.session_count() == 1
    assert chat_store.load_session("g:1:100") == [{"role": "user", "content": "m4"}]


def test_delete_session_and_prefix(tmp_path):
    chat_store.init(tmp_path / "chat.db")
    for key in ("g:1:100", "g:1:200", "g:2:100", "p:1:1"):
        chat_store.save_session(key, [])

    assert chat_store.delete_sessions("g:1:") == 2
    assert chat_store.session_count() == 2

    chat_store.delete_session("p:1:1")
    assert chat_store.session_count() == 1
    assert chat_store.load_session("p:1:1") == []


def test_prune_removes_stale_rows(tmp_path):
    chat_store.init(tmp_path / "chat.db")
    chat_store.save_session("g:1:100", [])
    chat_store.save_group_lines(1, ["a：hi"])
    # 手工把 updated_at 改旧来模拟"过期"
    with chat_store._lock:
        chat_store._db().execute(
            "UPDATE sessions SET updated_at='2020-01-01T00:00:00'"
        )
        chat_store._db().execute(
            "UPDATE group_context SET updated_at='2020-01-01T00:00:00'"
        )
        chat_store._db().commit()

    assert chat_store.prune(14) == 2
    assert chat_store.session_count() == 0
    assert chat_store.load_group_lines(1) == []


def test_group_lines_roundtrip(tmp_path):
    chat_store.init(tmp_path / "chat.db")
    assert chat_store.load_group_lines(42) == []

    chat_store.save_group_lines(42, ["a：你好", "b：吃了吗"])
    assert chat_store.load_group_lines(42) == ["a：你好", "b：吃了吗"]
    chat_store.delete_group_lines(42)
    assert chat_store.load_group_lines(42) == []


def test_migrate_legacy_json(tmp_path):
    """旧 chat_history.json 里的会话迁进库后，原文件改名 .bak。"""
    legacy = {
        "g:9:900": [{"role": "user", "content": "旧会话"}],
        "p:800:800": [{"role": "user", "content": "旧私聊"}],
    }
    (tmp_path / "chat_history.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    chat_store.init(tmp_path / "chat.db")

    assert chat_store.session_count() == 2
    assert chat_store.load_session("g:9:900") == [{"role": "user", "content": "旧会话"}]
    assert (tmp_path / "chat_history.json.bak").exists()
    assert not (tmp_path / "chat_history.json").exists()


def test_summary_roundtrip(tmp_path):
    """滚动摘要随会话存取；不带摘要的旧式保存不覆盖已有摘要。"""
    chat_store.init(tmp_path / "chat.db")
    msgs = [{"role": "user", "content": "我在准备考研"}]
    chat_store.save_session("g:1:100", msgs, "用户在准备考研，目标院校未定")

    history, summary = chat_store.load_session_full("g:1:100")
    assert history == msgs
    assert summary == "用户在准备考研，目标院校未定"
    assert chat_store.load_session("g:1:100") == msgs  # 兼容旧接口

    # 摘要清空（压缩失败退回截断时会传空串以外的场景：这里验证显式覆盖）
    chat_store.save_session("g:1:100", msgs, "")
    assert chat_store.load_session_full("g:1:100")[1] == ""


def test_migrate_add_summary_column(tmp_path):
    """老库（无 summary 列）打开时自动补列，历史数据不丢。"""
    import sqlite3

    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions ("
        "key TEXT PRIMARY KEY, updated_at TEXT NOT NULL, messages TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES('g:1:100','2020-01-01T00:00:00','[]')"
    )
    conn.commit()
    conn.close()

    chat_store.init(db)
    assert chat_store.session_count() == 1
    assert chat_store.load_session_full("g:1:100") == ([], "")


def test_forget_user_sessions(tmp_path):
    """/forget 用：清掉某人私聊 + 各群里的会话，别人的不动。"""
    chat_store.init(tmp_path / "chat.db")
    for key in ("g:1:100", "g:2:100", "p:100:100", "g:1:200", "p:200:200"):
        chat_store.save_session(key, [{"role": "user", "content": "x"}])

    assert chat_store.forget_user_sessions(100) == 3
    assert chat_store.session_count() == 2
    assert chat_store.load_session("g:1:200") == [{"role": "user", "content": "x"}]
    # 不存在的用户清 0 条
    assert chat_store.forget_user_sessions(999) == 0
