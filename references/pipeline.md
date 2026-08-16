# 流水线细节（slg-fusion-research）

## 目录结构（技能内）
```
<SKILL_DIR>/
  SKILL.md
  requirements.txt
  src/        run.py, collect.py, st_client.py, scorecard.py, narrative.py, expand.py
  configs/    slg.yaml, slg_expanded.yaml, rpg.yaml, fusion_library.yaml
  templates/  report.html.j2
  data/       cache/                     # ST API 原始响应缓存
  references/ narrative_schema.md, pipeline.md
```

## run.py 主要开关
- `--config <path>`：配置文件（必填）
- `--dry --dump-scored <path>`：**推荐流程**。只算分 + 把结构化数据导出给 AI，不调外部模型
- `--narrative-json <path>`：读取 AI 写回的方案卡 JSON 渲染最终报告
- `--probe` / `--discover`：探测 Sensor Tower 端点 / 自定义字段
- `--out <path>`：输出 HTML 路径（默认 `out/<核心玩法>_玩法融合立项机会研究.html`）

## 数据来源
- **有 SENSORTOWER_AUTH_TOKEN**：自动走 Sensor Tower API（市场大盘、种子产品、标杆竞品）。
- **无 token**：工具直接拒绝生成报告（仅使用 Sensor Tower API 真实数据，无手工 CSV 降级路径）。请先配置 `SENSORTOWER_AUTH_TOKEN`。

## 换研究对象
复制 `configs/slg.yaml` 改 `meta.core_genre`（如 RPG）和 `fusions` 清单即可。`slg_expanded.yaml` 是 32 方向含英文关键词的扩展示例。

## 依赖安装（隔离 venv 示例）
```
python -m venv venv
venv/Scripts/python -m pip install -r <SKILL_DIR>/requirements.txt
```
