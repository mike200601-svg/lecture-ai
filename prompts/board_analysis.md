# 板书照片理解（Phase 3）

> 状态：草稿骨架，Phase 3 实现。

不要依赖传统 OCR —— 板书上有文字、公式、坐标系、箭头、电路图、光路图、几何图。
用视觉模型整体理解。

## 输出

```json
{
  "timestamp": "00:24:12",
  "topics": [],
  "equations": ["LaTeX 形式"],
  "description": "板书整体内容描述",
  "diagram_type": "none|coordinate|circuit|optics|geometry|schematic",
  "needs_original_image": true,
  "possible_context": "推测这块板书在讲什么"
}
```

`needs_original_image`：示意图/复杂推导 → true（笔记里嵌原图）；
简单公式 → false（转成 LaTeX 即可，不要无脑塞图）。
