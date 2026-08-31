# 章节识别（Phase 2 · 第二层）

> 状态：草稿骨架。

在已纠错的 transcript 上识别授课结构，输出：

```json
[
  {"title": "波函数的统计解释", "start": "00:14:23", "end": "00:32:50",
   "type": "concept|derivation|example|review|admin"}
]
```

要求：时间区间必须连续覆盖，不重叠；标题用老师实际讲的说法，不要自己造词。
