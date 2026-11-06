# 三项目预审演示（新编虚构样本）

此目录是根据论证稿中的项目类型重新编写，不是原稿未附的旧材料。
导入 materials 下 13 份文件。框架不含赛事权重，不输出名次。

团队建议：内置 Crew Leader（资料核对/综合），技术与 AI 证据审阅员，商业与反证审阅员。
若机器上配置了真实 Kimi/Hermes，可指定对应成员；否则须明确展示实际模型，不能把角色名冒充执行器。

验收：运行 `uv run python scripts/run_project_review.py --output /absolute/path`。
读取 REVIEW_API_KEY / REVIEW_BASE_URL / REVIEW_MODEL；默认使用已配置的 KIMI_API_KEY / KIMI_BASE_URL，
采用 Anthropic 兼容协议，默认模型 kimi-for-coding。凭据只从环境读取，绝不导出。
脚本通过 Crew 的实际 Wiki 文件存储读取来源，真实模型分别执行两个独立专业角色并由 Leader 综合；
运行记录区分这种“同模型多角色真实调用”与完整桌面外援集成验收。

自动检查只核对结构、来源名称/小节/逐字引文、维度覆盖及虚构声明。
不能证明所有推理正确；必须人工审阅。不存在来源就拒绝输出成功，不生成示范答案填空。
