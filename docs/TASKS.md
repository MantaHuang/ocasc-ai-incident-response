# 待办与交接

## 当前：冻结评估脚本

- [x] 动手前读取EXPERIMENT_DESIGN第2节与INTERFACE。
- [x] 实现eval/run.py与eval/grade.py，不修改tasks.json。
- [x] 离线验证6条T2记录的JSONL、续跑、判分；未使用黑客松额度或其他模型。
- [x] 记录模拟/真实数据区分及已知局限。
- [ ] 用户授权后再做真实网关冒烟；当前明确不调用。
- [ ] 正式数据采集、全量288次：本轮禁止执行。

以下保留早期选题阶段待办，不能覆盖当前冻结实验与用户最新授权。

- [x] 读取OCASC总指南PDF、Apart官网与领取指南。
- [x] 导出用户可见完整对话的公开脱敏版。
- [x] 整理独立参谋任务和证据分层。
- [ ] Claude独立方案参谋，输出docs/CLAUDE_REVIEW.md。
- [ ] 用户确认最终选题与最小范围。
- [ ] 补齐48关链接或测试记录（若仍与选题相关）。
- [ ] 复核本地Fable调用记录与平台计费/权限；GPT6仍缺已审阅证据。
- [ ] 完成中文HTML阅读版（既有样式问题未答复）。
- [ ] 若选定工具课题，再建模拟环境、指标、实验记录与Demo。
- [ ] 按官方模板准备报告与必要附录，截止前提交。

仓库内容发布不等于已把任务自动送达Claude，也不等于其已有写权限。

## 2026-09-12 19:40 交接（Claude Opus 5 → Codex）

选题已定，详见 `docs/EXPERIMENT_DESIGN.md`（预注册，跑数据前冻结）。
放弃「第49关」合成招聘场景，理由见 `docs/CLAUDE_REVIEW.md` 第 2 节。
经 GPT-6 与 Fable 5.1 两轮独立审查，两边均判定新方向优于「值班室」。

- [x] 出题与评分标准（`eval/tasks.json`，8 题 × 2 变体，全部合成数据、答案可机械校验）
- [ ] **给 Codex**：写 `eval/run.py`（读 tasks.json，对 4 个模型 × 2 变体 × 3 轮 = 192 次调用，输出 JSONL）
- [ ] **给 Codex**：写 `eval/grade.py`（按 EXPERIMENT_DESIGN 第 5 节的 C0–C4 档位关键词初筛 + 正确性机械校验；T6 需实际运行正则打标签）
- [ ] 跑实验（黑客松额度，约 $10–15）
- [ ] 人工复核判分，初筛与复核不一致的条目单独列表
- [ ] 报告 ≤8 页 + Limitations and Dual-Use Considerations 附录（必需）

### 给 Codex 的注意事项

- 网关 `https://api.openai-next.com`，**不带 `/v1`**；密钥在本地 `tools/opendev.env`，勿入库。
- 余额截至 2026-09-12 18:40 为 $44.02，**失效时刻 2026-09-15 00:00 北京时间**（`access_until=1789401600`）。这解决了 STATUS.md 里标为缺证据的那一项。
- **不要用黑客松额度跑 agentic 编码**，实测约 $100/百万输出 token，两次长 agentic 任务已耗约 $56。脚本编写请走 ChatGPT 订阅。
- 调用参数（temperature、max_tokens、system prompt）全程固定并记录进报告附录。
- 模型一律记为「模型 ID X，经 OpenDev 网关，日期 Y」，不断言上游身份。
- 本机时区是 EST 不是北京时间，按本机时钟排期会高估工时。

### STATUS.md 需修正的两处

1. 「本轮没有重跑调用」已过时：主会话已重跑，并修正了 tools/ 里的 base URL 错误（文档写的 `credits.openai-next.com` 是门户站，真实网关是 `api.openai-next.com`）。
2. 「精确失效时刻」不再是缺证据，见上。
