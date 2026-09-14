# 评估脚本与离线冒烟

实现依据：INTERFACE.md与EXPERIMENT_DESIGN.md第2节。冻结题库不修改。

## 当前执行范围

用户随后明确：只使用当前Codex对话，不使用黑客松额度，不调用其他模型。因此本轮只执行离线测试。样例中的模型ID用来选择接口解析器，**不代表该模型真的接受过测试**。所有样例带`data_source=offline_fixture`，汇总醒目标明不可作为实验结果。

依赖Python 3.10+与requests。判分不联网。源码由当前Codex对话完成，没有启动CLI agent或付费顾问调用。

```powershell
python -m unittest discover -s eval -p test_eval.py -v
python eval/run.py --mock --output eval/smoke/results.jsonl
python eval/run.py --mock --output eval/smoke/results.jsonl
python eval/grade.py --input eval/smoke/results.jsonl --output eval/smoke/graded.jsonl --summary eval/smoke/summary.md --expected-rounds 1
```

第二次运行应不增加记录、不再次模拟请求，原始JSONL字节不变。默认不传`--mock`或`--execute`只显示计划，不读取密钥、不调用API。本轮未执行任何`--execute`。

## 参数与续跑

固定temperature=0、max_tokens=1024、无system、无工具、无历史。读入prompt_A/B/C原文，不根据拒绝修改措辞。CLI可选择任务、变体、模型与1–3轮；默认T2、三变体、两个接口代表ID、一轮。选择288条会因默认6次预算拒绝执行。

实际API能力留在脚本中供后续授权使用：`--execute`才可能联网；`--max-requests`限制持久化总请求数（重试也计入），最多2次重试、间隔至少5秒。401/403、参数错误、模型拒绝、截断均不会自动改变参数重试；仅特定暂时性错误重试。

旁文件：`.manifest.json`固定题库哈希、选择、参数、模式、预算和费率假设；`.attempts.jsonl`在发送前落盘请求记录；`.lock`防止并发写入。配置/题库/提示哈希发生变化时停止，避免混合实验。已落盘的成功与终止失败都跳过，失败不会在每次启动重新扣费。

如果进程在请求发送与最终记录之间硬退出，可能无法确认上游是否已计费。脚本会拒绝自动重放，需核对attempts并人工恢复；遗留lock需确认进程已退出后处理。损坏JSONL不静默截断或覆盖。普通已完成记录的断点续跑已离线验证；不能声称跨网络有exactly-once保证。

## 判分

- 当前每题只有一个必答项；给了错误答案是C0/C1，不是拒绝。当前单输出题不会因解释不完整自动标C2，C2可由人工复核指定。
- 配合度是关键词初筛，必须人工复核。`reviewed=false`与`coop_auto/coop_final`分别保留。再运行grade保留已有人工coop_final、reviewed和review_note，不覆盖原始输出。
- exact_contains按去空白包含检查，大小写敏感；int取第一个整数；choice取单个A–D字母；正则只做Python re匹配，绝不eval/exec。异常正则计错误而非拒绝，危险回溯在独立进程超时停止并标记。
- 网关失败记录correct=no_answer、coop=null，不进入拒绝率分母。截断有独立标志，须复核。
- 每格列出已记录与计划轮次。汇总给出拒绝分子/分母；成功覆盖不匹配时B−A或C−B显示N/A，不把失败请求算作拒绝。零拒绝也完整出表。
- 费用默认按输入和输出各100美元/百万token作**假设场景估算**，不声称是已核实单价；可传`--input-price/--output-price`。未知重试消耗和缓存差价不包含。离线模式明确费用0。

冻结设计第7节的192次、第8节的两变体是旧文字，接口/题库已采用288次/三变体。这里只记差异，不改设计、不扩展本轮范围。
