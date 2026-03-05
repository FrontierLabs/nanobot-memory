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

PROFILE_LIFE_UPDATE_PROMPT = '''你是用户画像更新员。根据对话记录，判断需要对用户画像做哪些操作。

【当前用户画像】（每条都有 index 编号）
{current_profile}

【对话记录】（来自同一主题的多轮对话）
{conversations}

【任务】
分析对话，输出需要执行的操作列表（可以有多条操作）。可选操作类型：
- **update**: 修改现有条目（通过 index 指定）
- **add**: 新增画像条目
- **delete**: 删除现有条目
- **none**: 无需任何操作（当对话不包含任何用户信息时使用）

【操作选择指南】
- **update**: 现有条目有信息更新、补充、修改
- **add**: 发现全新的用户信息（与现有条目无关）
- **delete**: 以下情况应该删除：
  - 用户明确否定（如"我不再吃素了"）
  - 信息已过时（如"下周要出差"但已经过了）
  - 与新信息直接矛盾

【重要规则】
1. **挖掘标签**：隐式特征必须包含【性格标签】，例如：[风险厌恶型]、[社交驱动型]、[数据考据党]。
2. 只提取用户信息，不要把 AI 助手的建议当成用户特征
3. sources 格式：使用对话 ID（方括号里的，如 ep1, ep2）
4. evidence 要包含时间信息 - 如"2024年10月用户提到..."
5. explicit_info 和 implicit_traits 的 index 是独立编号的

【画像定义与分析框架】
- **explicit_info（显式信息）**：可以直接从对话中提取的用户事实。
  - *包含内容*：基本资料、健康状况、能力技能、明确偏好等。

- **implicit_traits（隐式特征）**：基于行为推断的心理画像、性格标签和决策风格。
  - *提取要求*：请结合对话上下文，从决策模式、社交偏好、生活哲学等维度进行自由分析和概括。
  - *命名规范*：
    1. 标签必须简练、可读、可复用（便于检索/对比），尽量控制在 2-6 个字。
    2. 避免把多个维度硬拼成一个长标签；如果信息包含多个维度，请拆成多条隐式特征分别表达。
    3. 标签应描述“稳定的行为/心理倾向”，不要写成一次性的事件或短期状态。
  - 请做合理推理，提取出用户的深层特征
【输出格式】
无操作时：
```json
{{"operations": [{{"action": "none"}}], "update_note": "对话不包含用户信息"}}
```

有操作时（可以组合多条 add/update/delete）：
```json
{{
  "operations": [
    {{"action": "add", "type": "explicit_info", "data": {{"category": "...", "description": "...", "evidence": "...", "sources": ["ep1"]}}}},
    {{"action": "add", "type": "implicit_traits", "data": {{"trait": "...", "description": "...", "basis": "...", "evidence": "...", "sources": ["ep1", "ep2"]}}}},
    {{"action": "update", "type": "explicit_info", "index": 0, "data": {{"description": "...", "sources": ["ep3"]}}}},
    {{"action": "delete", "type": "implicit_traits", "index": 1, "reason": "..."}}
  ],
  "update_note": "新增2条显式信息和1条隐式特征，更新1条，删除1条"
}}
```'''

PROFILE_LIFE_COMPACT_PROMPT = '''当前用户画像有 {total_items} 条记录（explicit_info + implicit_traits 合计），超过了上限 {max_items} 条。

请精简画像至 **合计 {max_items} 条**（explicit_info + implicit_traits 两类加起来，不是每类 {max_items} 条）。

精简原则：
1. **合并同类项**：将同一维度的多条记录（如多次体重记录）合并为一条"当前状态+趋势"的描述。
2. **提炼标签**：隐式特征应归纳为性格标签（如[风险厌恶型]），删除重复或浅层的描述。
3. 删除不重要、已过时或短期状态。
4. 保留每条条目的字段完整（尤其是 evidence / sources）。

当前画像：
{profile_text}

**重要**：输出的 explicit_info + implicit_traits 合计必须 ≤ {max_items} 条。
```json
{{
  "explicit_info": [
    {{"category": "...", "description": "...", "evidence": "...", "sources": ["episode_id"]}}
  ],
  "implicit_traits": [
    {{"trait": "...", "description": "...", "basis": "...", "evidence": "...", "sources": ["id1", "id2"]}}
  ],
  "compact_note": "说明删除/合并了哪些内容"
}}
```'''

PROFILE_LIFE_INITIAL_EXTRACTION_PROMPT = '''你是一个"用户画像分析师"。请阅读下面的对话，构建用户画像。

【第一部分：显式信息 (explicit_info)】
用户的客观事实和当前状态，如身高体重、喜好、疾病等。

【第二部分：隐式特征 (implicit_traits)】
基于行为推断的心理画像、性格标签和决策风格。
*提取要求*：从决策、社交、生活观念等维度进行深度挖掘。
*命名规范*：Trait 字段必须简练精准，推荐“[形容词] [名词]”格式，严禁过度堆砌形容词。

【提取原则】
1. 只提取用户本人的信息，不要把助手的建议当成用户特征
2. 隐式特征必须有多个证据支撑：同一条隐式特征的 sources 至少包含 2 个来源；证据可以来自【当前对话】与/或【已有画像 current_profile 的 evidence/sources】（更新时可用），不能仅凭单条新对话臆断
3. 每条信息用一句自然语言描述，通俗易懂
4. 标注信息来源（消息编号）

【输出格式】
请直接输出 JSON，格式如下：
```json
{{
  "explicit_info": [
    {{
      "category": "分类名",
      "description": "一句话描述",
      "evidence": "一句话证据（来自对话内容）",
      "sources": ["YYYY-MM-DD HH:MM|episode_id"]
    }}
  ],
  "implicit_traits": [
    {{
      "trait": "特征名称",
      "description": "一句话描述这个特征",
      "basis": "从哪些行为/对话推断出来的",
      "evidence": "一句话证据（来自对话内容）",
      "sources": ["YYYY-MM-DD HH:MM|episode_id1", "YYYY-MM-DD HH:MM|episode_id2"]
    }}
  ]
}}
```

【对话原文】
{conversation_text}'''

MEMORY_COMPRESS_PROMPT = """你是长期记忆压缩员。下面是一份 MEMORY.md 的当前内容，已超过字符上限，需要压缩。

【要求】
1. 保留对用户/项目真正重要的信息：偏好、关键决策、待办、关系、重要日期等。
2. 合并重复或可合并的条目，删除过时、低价值或纯寒暄类内容。
3. 输出仍为 Markdown，条目用 `- ` 列表；可保留少量标题行。
4. 总字符数（含换行）必须不超过 {max_chars}。
5. 直接输出压缩后的完整内容，不要解释、不要包在代码块里。

【当前 MEMORY.md 内容】
```
{content}
```

请输出压缩后的 MEMORY.md 内容（纯 Markdown）："""
