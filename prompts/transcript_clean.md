# 课堂转录忠实清洗（Phase 2A）

你在处理一份课堂 ASR 转录。当前模式：`{{mode}}`。

## 绝对边界

1. 这是忠实编辑，不是摘要。不得概括、压缩、扩写或补充外部知识。
2. 不得删除有信息量的内容。必须保留例子、推导过程、老师强调、考试提示、常见错误、
   直觉解释、数字、公式口述和视觉/板书引用。
3. 只允许修正明显 ASR 错字（尤其参考术语表）、补标点和断句、整理明确无意义的口癖。
4. 不确定时保留原文，并把原因写入 `uncertain`；禁止猜测。
5. 每个输入 segment id 必须且只能输出一次。不得新增、删除、合并或重排 id。
6. 只返回符合给定 JSON Schema 的 JSON，不要 Markdown，不要解释。
7. 对“字幕、订阅、转发、打赏”等与课堂上下文明确无关的 ASR 模板幻觉，可以删除；
   如果整段只有这类模板，保留该 id、令 `text` 为空，并在 `uncertain` 写明删除原因，
   同时在 `corrections` 记录 original/corrected/decision/reason。
   只要可能是老师原话，就必须保留，不能自作聪明。
8. 每次实质纠错或删除都写入 `corrections`；只补标点、断句时可留空数组。

课程：{{course_name}}

参考术语（仅用于拼写候选，不是事实来源）：
{{glossary}}

## 模式说明

- `clean_chunk`：清洗每个 segment 的 `text`。原时间戳由程序保管，无需改动。
- `reconcile_boundary`：输入包含相邻块对重叠 segment 的不同版本。只协调这些 id 的
  断句、重复与术语一致性；不得触碰非重叠区，也不得引入两侧都没有的信息。

边界上下文：
{{boundary_context}}

<input_json>
{{segments_json}}
</input_json>

输出对象格式：

```json
{
  "segments": [
    {
      "id": 0,
      "text": "忠实清洗后的文字",
      "uncertain": [],
      "visual_references": [],
      "corrections": [
        {
          "original": "波涵数",
          "corrected": "波函数",
          "decision": "correct",
          "reason": "课程术语与上下文一致"
        }
      ]
    }
  ]
}
```
