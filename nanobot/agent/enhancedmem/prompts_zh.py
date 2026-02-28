"""EnhancedMem 中文 Prompts（边界检测、Episode、EventLog、Foresight、Life Profile）"""

CONV_BOUNDARY_DETECTION_PROMPT = """
### 核心任务
作为一名对话分析专家，你需要判断新传入的消息是否为一个已有对话"情节"的自然结尾。目标是将连续的对话流分割成有意义的、可独立记忆的片段（MemCell）。你的核心原则是 **"默认合并，谨慎切分"**。

### 对话上下文
**已有对话历史:**
```
{conversation_history}
```

**与上一条消息的时间间隔:**
`{time_gap_info}`

**新传入的消息:**
```
{new_messages}
```

### 决策变量详解
你需要输出三个关键决策变量：`should_end`, `should_wait`, 和 `topic_summary`。

1.  **`should_end` (结束当前情节):**
    -   **何时设为 `true`?** 仅当新消息明确开启一个与之前历史无关的新主题时。
    -   **触发场景示例:**
        -   **跨天强制切分:** 只要新消息与上一条消息的日期不同，就必须切分。
        -   **主题切换:** 对话从一主题突然转向另一不相关主题。
        -   **任务完成并开启新篇:** 当下一条消息开启了完全不相关的新话题时切分。
        -   **长时间中断后开启新话题:** 时间间隔超过4小时，且新消息与历史无明显关联。

2.  **`should_wait` (等待更多信息):**
    -   **何时设为 `true`?** 当新消息**信息量不足**，无法判断时。
    -   **必须设为 `true` 的情况:**
        -   **非文本消息:** 新消息仅为 `[图片]`, `[视频]` 等占位符。
        -   **无明确意图的短回复:** 如"好的", "嗯", "收到"。
        -   **不确定的中间状态:** 时间间隔和内容都模糊时。

3.  **`topic_summary` (情节主题总结):**
    -   **何时生成?** 仅在 `should_end` 为 `true` 时。
    -   **内容要求:** 用一句话概括即将结束情节的核心内容。

### 决策原则
- **合并是默认倾向:** 不确定时不切分 (`should_end: false`)。
- **`should_end` 与 `should_wait` 互斥。**

### 输出格式
请严格按照以下JSON格式返回：
```json
{{
    "reasoning": "一句话解释决策。",
    "should_end": true或false,
    "should_wait": true或false,
    "confidence": 0.0-1.0,
    "topic_summary": "仅should_end为true时填写，否则为空字符串。"
}}
```
"""

DEFAULT_CUSTOM_INSTRUCTIONS = """
生成情节记忆时请遵循以下原则：
1. 每个情节应该是一个完整、独立的故事或事件
2. 保留所有重要信息
3. 使用陈述性语言描述情节
4. 突出关键信息
5. 确保便于后续检索
"""

GROUP_EPISODE_GENERATION_PROMPT = """
你是事件记录与提炼专家。将以下对话转化为清晰的情景记忆（第三人称叙事）。

对话开始时间：{conversation_start_time}
对话内容：
{conversation}

自定义指令：
{custom_instructions}

要求：以第三人称流畅叙述，提炼核心议题、决策、关键信息。客观，不评价。

仅返回JSON：
{{"title": "精炼标题(含日期)", "content": "叙事文本", "summary": "简短摘要"}}
"""

EVENT_LOG_PROMPT = """你是信息抽取分析师。从对话中提取原子事实。

对话开始时间: {time}
对话内容:
{input_text}

规则：每个atomic_fact是独立可检索的完整句子，第三人称，明确归因。过滤问候寒暄。

仅返回JSON：
{{"event_log": {{"time": "时间", "atomic_fact": ["事实1", "事实2"]}}}}
"""

FORESIGHT_GENERATION_PROMPT = """
基于以下对话，预测对用户未来行为可能的具体影响。联想而非总结。每条不超过40字，4-8条。

user_id: {user_id}
user_name: {user_name}
conversation:
{conversation_text}

仅返回JSON数组：
[{{"content": "预测", "evidence": "证据", "start_time": "YYYY-MM-DD", "end_time": "YYYY-MM-DD", "duration_days": N}}]
"""

PROFILE_LIFE_UPDATE_PROMPT = """你是用户画像更新员。根据对话记录，判断需要对用户画像做哪些操作。

【当前用户画像】
{current_profile}

【对话记录】
{conversations}

【任务】分析对话，输出操作列表。操作类型：update（修改）、add（新增）、delete（删除）、none（无操作）。

【输出格式】仅返回JSON：
{{"operations": [{{"action": "add", "type": "explicit_info", "data": {{"category": "...", "description": "...", "evidence": "..."}}}}], "update_note": "..."}}

无操作时：{{"operations": [{{"action": "none"}}], "update_note": "无"}}
"""
