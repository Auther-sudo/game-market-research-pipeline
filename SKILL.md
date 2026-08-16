---
name: slg-fusion-research
description: "融合玩法立项机会研究流水线。给定核心玩法（如 SLG）与若干融合玩法候选，自动采集 Sensor Tower 市场数据、做五维打分与 ROI 排序，并由 WorkBuddy 自身 AI 为每个方向撰写立项方案卡（含「可融合的非SLG游戏」清单），最终生成可交互 HTML 报告。适用于游戏立项调研、玩法融合机会扫描、竞品标杆分析。所有 AI 撰写步骤均由 WorkBuddy 自带 AI 完成，无需外部 LLM API key。"
agent_created: true
---

# 融合玩法立项机会研究（SLG 等）

## 这个 skill 做什么
输入一个核心玩法 + 若干「融合玩法」候选（在 YAML 里配置），输出一份立项机会研究报告：
1. 采集市场数据（仅 Sensor Tower API 真实数据）
2. 五维打分（吸量/空间/竞争/留存/付费）+ ROI 排序
3. **由 WorkBuddy 自带 AI 为每个方向撰写 11 模块方案卡**（含「可融合的非SLG游戏」清单，不依赖任何外部 LLM）
4. 渲染成 HTML 报告

## 何时用
- 用户要做「XX 玩法 × 其他玩法」的融合立项调研
- 用户说「帮我调研 SLG 融合玩法」「生成立项机会报告」「扫描玩法融合方向」「XX 整体情况调研」等

## 前置
- Python 3.11+，依赖：pyyaml、jinja2、requests。运行前确保已装，可：
  `pip install -r <SKILL_DIR>/requirements.txt`（建议用隔离 venv）
- **Sensor Tower token 是必需的**——没有 token 时工具直接拒绝生成报告（不会降级到任何示例/占位数据）
- 把 token 放在研究工作区的 `config.user.vbs`（技能目录不存你的密钥）

## ⚠️ 强制第一步：必须先向用户要 Sensor Tower token

**在运行任何命令之前，你必须先问用户：是否已有 Sensor Tower token？**

- **用户有 token**：让用户把 token 写入 `<WORKDIR>/config.user.vbs`（格式见 `<SKILL_DIR>/config.user.vbs.example`），然后继续正常流程。技能会自动从该文件加载 token。
- **用户没有 token**：明确告知——
  > 「没有 Sensor Tower token 的话，本工具无法生成报告（仅使用 ST 真实数据，无示例数据降级）。请先配置 `SENSORTOWER_AUTH_TOKEN` 再运行。」
  > 不要在没有 token 的情况下声称已生成真实数据报告。
- **严禁在没有 Sensor Tower token 的情况下声称已生成真实数据报告或混入示例数据。**

## 工作流程（AI 驱动，无需外部 API）
设本技能目录为 `<SKILL_DIR>`（即本文件所在目录），用户研究工作区为 `<WORKDIR>`（任意目录，如 `D:\projects\slg-research` 或 `~/slg-research`）。

1. **准备配置**：把 `<SKILL_DIR>/configs/slg.yaml` 复制到 `<WORKDIR>/slg.yaml`，按用户需求改 `meta`（核心玩法名/描述）和 `fusions`（融合候选清单）。换研究对象（如 RPG）就改这份文件即可，其余不用动。
2. **跑数据流水线 + 导出给 AI 的数据**：
   - **有 token（正常模式）**：
     ```
     python <SKILL_DIR>/src/run.py --config <WORKDIR>/slg.yaml --dry --dump-scored <WORKDIR>/scored.json --out <WORKDIR>/report_draft.html
     ```
     技能会自动从 `<WORKDIR>/config.user.vbs`（fallback：包根/cwd）加载 `SENSORTOWER_AUTH_TOKEN`，无需手动设环境变量。
   - **无 token（demo 模式，用户已确认）**：
     ```
     python <SKILL_DIR>/src/run.py --config <WORKDIR>/slg.yaml --dry --dump-scored <WORKDIR>/scored.json --out <WORKDIR>/report_draft.html
     ```
     报告使用 ST 真实数据；无 token 时本步骤不会执行（工具会在更早阶段拒绝生成）。
3. **AI 撰写方案卡（本技能核心「AI 部分」，由 WorkBuddy 自身完成，不调外部模型）**：
   读取 `<WORKDIR>/scored.json`。其中 `fusions` 是每个方向的实测评分与标杆数据，`modules` 是 11 个文字模块及其字数/格式要求，`system_prompt` 是分析师人设与硬性要求。
   为**每个 fusion** 写满 11 个 module 的文字（严格遵循字数与格式），输出：
   `<WORKDIR>/narrative.json`，结构：`{ "<fusion_id>": { "<module_key>": "<文字>", ... }, ... }`
   具体规范见 `<SKILL_DIR>/references/narrative_schema.md`。**不得编造市场数字、下载量、收入**，所有数值以 scored.json 中的实测为准。
4. **渲染最终报告**：
   ```
   python <SKILL_DIR>/src/run.py --config <WORKDIR>/slg.yaml --narrative-json <WORKDIR>/narrative.json --out <WORKDIR>/report.html
   ```
5. 把 `<WORKDIR>/report.html` 用预览 / 附件交付给用户。

## 可选：用外部 LLM 加速（非必需）
若用户配置了 LLM_API_KEY 且想让脚本自动调用外部模型（更快但需 key），可跳过第 3 步，直接：
```
python <SKILL_DIR>/src/run.py --config <WORKDIR>/slg.yaml --out <WORKDIR>/report.html
```
本技能默认推荐上面的「AI 手桥」流程（第 2 -> 3 -> 4 步），因为它不依赖任何外部服务、纯靠 WorkBuddy 自身 AI。

## 注意事项
- 评分层数字永远来自实测/配置，AI 只写文字、改不了分数。
- 标杆竞品扫描需要 ST token；无 token 时报告中该板块显示「需配置 token」。
- 详细数据 schema 与模块规范：`references/narrative_schema.md`；流水线细节：`references/pipeline.md`。
