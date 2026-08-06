---
name: record-to-skill
description: 把用户在内置浏览器中录制的一段真实操作编译成可重复执行的 Playwright 工作流技能。当用户点“生成技能”、要求保存刚才的录制，或希望以后自动重放同一浏览器流程时使用。
metadata:
  skillCategoryName: 设计与开发
  version: 2.0.0
  zh_name: 录制转技能
  mobileclaw:
    emoji: ⏺️
    requires:
      tools:
      - record_compile
      - record_install
      - record_replay
      bins: []
      env: []
    primaryEnv: ''
  query_examples:
  - 把我刚才那套操作变成技能
  - 用刚才的录制生成一个技能
  - 保存并重放这段浏览器演示
  zh_description: 把真实浏览器录制编译成稳定、通用、可直接执行的 Playwright 自动化工作流。
---

# 录制转技能

本流程有三个阶段：

1. `record_compile` 从录制 trace 的 source step 编译不可变 Workflow IR v2 草稿。
2. `record_install` 发布 owner 私有工作流，并安装只含不透明 `workflow_id` 的入口技能。
3. `record_replay` 读取该计划，直接通过 Playwright 执行；回放不再逐步审批，也不使用
   一次性 permit。

## 回放会挂起等人，不是失败

跑到需要用户本人完成的步骤（验证码、密码）时，`record_replay` 返回
**`REPLAY_SUSPENDED`**，带 `resume_token`、`next_step`、`remaining_steps`。

```
REPLAY_SUSPENDED: {"resume_token":"...","next_step":2,"remaining_steps":5,...}
```

这时要做的是：**告诉用户需要他做什么**（浏览器已经交还给他了），等他说完成之后，
带着同一个 `resume_token` 再调一次 `record_replay`，工作流从 `next_step` 继续。

```json
{"workflow_id": "...", "inputs": {}, "resume_token": "上一次返回的那个"}
```

三条约束：
- **`REPLAY_SUSPENDED` 不是失败。** 不要重试、不要重新从头跑、不要改技能。
  `REPLAY_HALTED` 才是要排查的失败。
- `resume_token` **一次性**，且 15 分钟内有效。过期或用过就只能重新运行整个技能。
- 不要替用户填验证码，也不要在对话里让他把验证码发给你——浏览器已经在他手上了。

## 录制由用户发起

开始、暂停、继续、停止录制由用户在浏览器面板操作。用户要求开始录制时，引导他使用
面板按钮；录制停止后再调用编译工具。

## 编译请求

`record_compile` 只接收录制编号、技能 slug 和递增的 source step 引用：

```json
{
  "recording_id": "面板提供的十六进制 ID",
  "slug": "lowercase-skill-slug",
  "workflow": {
    "schema_version": "crew.browser.workflow.v1",
    "steps": [
      {"source_step": 1},
      {"source_step": 2}
    ]
  }
}
```

也可显式添加 `{"safe_step":"snapshot_full"}` 或
`{"safe_step":"takeover"}`。不要手写 selector、URL、输入值或动作；编译器从对应
trace 事件生成它们。

### 状态断言（强烈建议加）

```json
{"assert_step": {"source_step": 4, "state": "visible"}}
```

`state` ∈ `visible` / `hidden` / `enabled` / `disabled` / `checked` / `unchecked`
/ `editable`。**断言目标取自那个 trace step 记录的元素，你不能自己写 selector。**

为什么要加：一次失败的导航之后，剩下的步骤会在错误的页面上依次执行，产出一串
对不上任何元素的报错，而真正的原因（第 2 步就没进详情页）被埋在最后。断言让
工作流在第一个不对的地方就停下来，并报出"断言不成立"这个明确的失败类别。

加在哪：**跨页动作之后**。点进详情页之后断言详情页上某个元素 visible，
比在最后拿到一堆 stale_ref 有用得多。断言步骤可以重复引用同一个
`source_step`（点之前确认可见、点之后确认消失），动作步骤不行。

断言是只读判定：不改变页面、不产生后置快照、不消耗 source_step 的唯一性。

### 意外遮挡的自动处理（有弹窗就加）

```json
{"overlay_step": {"source_step": 3}}
```

含义：**第 3 步点击的那个元素是遮挡层的关闭控件**，把它注册成自动处理器，
而不是在那个固定位置执行一次。目标同样取自轨迹，你不能自己写 selector。

为什么：内网系统最常见的回放杀手不是站点改版，是随机出现的公告弹窗、满意度
调查、版本更新提示。录制那次没弹、回放这次弹了，于是每个后续点击都被一个
半透明遮罩吃掉，而报出来的是"元素不可点击"。注册处理器之后，Playwright 会在
**每次**动作前检查并清掉它——按固定位置插一步做不到这件事，因为弹窗什么时候
来是不确定的。

判断依据：如果录制轨迹里有一步是点「我知道了」「关闭」「稍后再说」这类控件，
它几乎一定该是 `overlay_step` 而不是 `source_step`。

## 功能编译规则

- 普通 click 保留为真实 Playwright click；dblclick、drag、Enter/快捷键也保留原动作。
- 真实提交由录制到的 submitter click 或 Enter 执行，不改写成 takeover。
- `click(control) → input` 中只保留最终控件状态；字段间用于移焦的 Tab 会被合并。
  checkbox/radio/select 不会同时回放触发 click 和状态动作。
- 同一字段的 Backspace/Delete/方向/Home/End/Page 编辑键不独立重放；连续表单阶段对
  同 selector 采用 last-write-wins，只生成一个参数和一次填写。
- 紧随 click/dblclick/Enter 的 Host navigate 是该动作的结果观察，不会再编译成
  第二次 navigate；首条与纯地址栏导航仍保留。
- 连续表单字段合并为一个 `fill_form`；不设置产品自定义字段数上限，也不因任意阈值拆批。
- 输入值不会固化在工作流中。参数名优先来自 aria-label、name、id、可见字段名，
  自动去重；缺参结果同时给出 `display_name` 和 `recorded_hint`。
- navigate 保留业务 query、fragment、跨 host 与 SSO 跳转。
- scroll 保留精确 `delta_x/delta_y`；内层容器同时保留稳定 selector。
- `handoff`（短信/邮件验证码、图形码、扫码）**一律**编译成 takeover：一次性码
  用过即废，把录到的那一个存下来自动填必然失败，站点会拒绝而工作流不知道，
  会继续在登录页上跑完剩下的步骤。`secret`（密码）可复用，保留自动填，代价由
  安装前的知情披露承担。普通按钮、SPA 动态动作、多站点流程和表单提交继续自动执行。
- **takeover 之后可以继续有步骤。** handoff/secret 是挂起点不是终点，所以
  「登录（含验证码）→ 读工单 → 汇报」是一个完整工作流，不需要拆成两个技能。
  只有 `{"safe_step":"takeover"}`（到此交还，结束）才封住计划。
- 安装需要用户确认。审批摘要会列出站点、动作计数、步数、是否含人工接管，以及
  **内含几个凭据字段的录制原值**。不要替用户跳过这一步，也不要在对话里复述凭据。
- 未终止的工作流固定追加 `snapshot_full`；成功结果中的 snapshot 必须来自这个真实
  最终观察，而不是前一步缓存。

Workflow artifact 使用 `crew.browser.replay.v2`。其中 `capabilities` 由最终 plan 精确
推导并按固定顺序保存；生成技能声明 `browser_policy.readonly=false` 与同一能力集合。
旧 `crew.browser.replay.v1` artifact 只保留原来的导航/观察兼容，不会被解释成 v2 写能力。

## 安装与运行

把 `record_compile` 返回的 `recording_id`、`draft_id`、`draft_digest` 原样传给
`record_install`。安装后的技能只携带 `workflow_id`，执行计划仍在当前 owner 的
`browser-workflows` 存储中。

运行时先用空 `inputs` 调用 `record_replay`。若返回
`REPLAY_INPUTS_REQUIRED`，按其中的人类可读字段向用户收集本次值，再一次性传入全部
inputs。不要复用录制值。

回放期间：

- 不调用 `browser_use`，由 `record_replay` 独占回放租约并按 plan 顺序执行。
- selector 必须由 Host 严格唯一解析；0 匹配和多匹配都停止。
- 元素动作依赖 Playwright 自身 actionability、超时和 Locator 语义。
- 任一 uncertain/partial 结果立即停止且不自动重试；批量填表会报告
  `completed_fields`。
- `REPLAY_OK` 必须包含真实最终 snapshot；`REPLAY_PARTIAL`/`REPLAY_HALTED` 要把
  已执行步数和稳定错误码如实交给用户。

## 汇报

简洁说明草稿、安装或回放结果。缺参时使用 `display_name`，例如“Username”或
“Country”，不要让用户猜 `field_1` 之类的内部键。
