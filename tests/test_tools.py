"""偏好工具注册正确性 (P2)。"""
from bot.tools import CONFIRM_REQUIRED, NON_MCP_TOOLS, TOOL_NAMES, TOOL_SCHEMAS


def test_pref_tools_are_local_and_not_money_gated():
    assert {"setUserPrefs", "getUserPrefs"} <= NON_MCP_TOOLS  # 走本地，不发瑞幸 MCP
    assert "setUserPrefs" not in CONFIRM_REQUIRED  # 偏好读写不需人工确认
    assert "getUserPrefs" not in CONFIRM_REQUIRED
    assert "createOrder" in CONFIRM_REQUIRED       # 花钱工具仍受确认门保护


def test_pref_tools_in_schema_and_wellformed():
    assert {"setUserPrefs", "getUserPrefs"} <= TOOL_NAMES
    by_name = {t["function"]["name"]: t for t in TOOL_SCHEMAS}
    for n in ("setUserPrefs", "getUserPrefs"):
        fn = by_name[n]["function"]
        assert fn["name"] == n
        assert "parameters" in fn and fn["parameters"]["type"] == "object"
