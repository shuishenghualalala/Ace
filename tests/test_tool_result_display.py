"""tool_result_detail_for_ui：终端等结构化工具结果的 UI 预览提取。"""

from crew.agent.loop.tool_result_display import tool_result_detail_for_ui


def test_terminal_detail_prefers_output_over_json_metadata():
  payload = (
      '{"success": true, "cwd": "C:\\\\work", "command": "python search.py", '
      '"exit_code": 0, "output": "line1\\nline2\\n世界杯赛程"}'
  )
  assert tool_result_detail_for_ui("terminal", payload) == "line1\nline2\n世界杯赛程"


def test_terminal_detail_keeps_tail_when_output_is_long():
  tail = "x" * 50
  output = ("a" * 2000) + tail
  import json

  payload = json.dumps({"success": True, "output": output}, ensure_ascii=False)
  detail = tool_result_detail_for_ui("terminal", payload, max_len=1200)
  assert detail.endswith(tail)
  assert len(detail) == 1200


def test_terminal_detail_falls_back_to_error():
  payload = '{"success": false, "error": "command blocked"}'
  assert tool_result_detail_for_ui("terminal", payload) == "command blocked"


def test_non_terminal_json_prefers_output_field():
  payload = '{"output": "hello", "meta": 1}'
  assert tool_result_detail_for_ui("web_search", payload) == "hello"


def test_plain_text_is_clipped():
  text = "z" * 3000
  assert len(tool_result_detail_for_ui("file_read", text, max_len=100)) == 100


def test_subagent_result_returns_full_json_without_clipping():
  """delegate_task/run_agent：前端 subagent 卡片需要完整 results JSON，不截断。"""
  import json

  payload = json.dumps(
      {
          "results": [
              {
                  "agent": f"task#{i}",
                  "status": "completed",
                  "summary": "摘要" * 500,  # 单条 1000 字，整体远超 1200
                  "duration_seconds": 5.38,
                  "tool_calls": 1,
              }
              for i in range(3)
          ]
      },
      ensure_ascii=False,
  )
  assert len(payload) > 1200
  for name in ("delegate_task", "run_agent"):
      detail = tool_result_detail_for_ui(name, payload)
      assert detail == payload
      # 可解析：前端 JSON.parse 的前提
      assert json.loads(detail)["results"][0]["tool_calls"] == 1


def test_subagent_result_paired_in_history_replay():
  """builtin executor 的 ToolCall.result 为空，历史回放须从 tool 消息配对完整 JSON。"""
  import json

  from crew.core.types import ToolCall
  from crew.gateway.routers.sessions import _tool_result_for_history

  payload = json.dumps(
      {"results": [{"agent": "task#0", "status": "completed", "summary": " done " * 400,
                    "duration_seconds": 3, "tool_calls": 2}]},
      ensure_ascii=False,
  )
  tc = ToolCall(id="tc-1", name="delegate_task", arguments={"tasks": [{"goal": "g"}]})
  assert _tool_result_for_history(tc, {"tc-1": payload}) == payload
  # 非 subagent 工具不配对（保持历史载荷精简的既有语义）
  other = ToolCall(id="tc-2", name="file_read", arguments={})
  assert _tool_result_for_history(other, {"tc-2": "x" * 5000}) == ""
