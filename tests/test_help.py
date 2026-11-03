"""帮助命令测试：用户命令齐全、管理员段按身份出现。"""

from src.plugins.help_cmd import build_help


def test_help_lists_user_commands():
    text = build_help(is_admin=False)
    for token in (
        "/help", "/reset", "/profile", "/forget",
        "/jev", "/news", "/quote", "/digest", "/remind", "/rank",
        "/weather", "/translate",
    ):
        assert token in text, f"帮助里漏了 {token}"


def test_help_admin_section_gated():
    plain = build_help(is_admin=False)
    admin = build_help(is_admin=True)
    assert "/bot" not in plain
    assert "/news push" not in plain
    assert "/bot status" in admin
    assert "/digest push" in admin
