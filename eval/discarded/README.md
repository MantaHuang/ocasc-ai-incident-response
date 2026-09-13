# 作废数据

`results.temperature-contaminated.jsonl` —— 2026-09-13 首批 9 条，**不得进入分析**。

采集时 payload 含 `temperature: 0.0`。该参数被网关拒绝（"`temperature` is deprecated for this model"），
且会使请求落入另一条带 agent 脚手架的路径：input 用量从约 300 膨胀到约 5000（含 cache_read 5118），
模型改以工具调用作答（出现 Claude Code 的 `todo_write` 工具名），或返回编造内容。

移除 temperature 后，同一提示词 input=311，与提示词长度相符，无脚手架痕迹。

保留本文件是为了让「为什么重采」可核查，不是为了使用这批数据。
