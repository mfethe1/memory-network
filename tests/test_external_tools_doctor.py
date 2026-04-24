from code_index.commands import doctor_cmd


def test_external_tools_report_has_recommended_systems() -> None:
    report = doctor_cmd._external_tools_report()

    assert {
        "scip",
        "scip_python",
        "ast_grep",
        "zoekt",
        "codeql",
    } <= set(report)

    for tool in report.values():
        assert set(tool) == {"available", "command", "path", "role", "hint"}
        assert isinstance(tool["available"], bool)
        assert tool["command"]
        assert tool["role"]
        assert tool["hint"]


def test_external_tool_uses_first_available_command(monkeypatch) -> None:
    def fake_which(command: str) -> str | None:
        if command == "sg":
            return "/tools/sg"
        return None

    monkeypatch.setattr(doctor_cmd.shutil, "which", fake_which)

    tool = doctor_cmd._external_tool(
        "ast-grep",
        commands=("ast-grep", "sg"),
        role="structural search",
        hint="install ast-grep",
    )

    assert tool["available"] is True
    assert tool["command"] == "sg"
    assert tool["path"] == "/tools/sg"
