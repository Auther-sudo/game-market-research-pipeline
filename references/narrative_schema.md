# narrative.json 规范（由 WorkBuddy AI 撰写方案卡）

## 输入：scored.json（由 `run.py --dump-scored` 产出）
```json
{
  "meta": { "core_genre": "SLG", ... },
  "modules": [
    {"key":"concept","name":"概念","spec":"一句话说清玩家在做什么、核心循环是什么，60-110字"},
    ... 共 11 个
  ],
  "system_prompt": "你是资深游戏立项分析师...",
  "fusions": [
    {
      "id": "match3",
      "name": "三消",
      "role": "副玩法",
      "priority": "P2",
      "combined_roi": 7.46,
      "strong_side": "CN",
      "coverage": 1.0,
      "seeds": [ {"name":"Puzzles & Survival"} ],
      "hypothesis": "三消作为独立副玩法承接泛用户，主线仍是 SLG",
      "by_market": {
        "CN": {
          "dims": { "traffic": {"score":7,"basis":"...","imputed":false}, ... },
          "risk": 12, "risk_reasons": ["..."]
        }
      },
      "benchmarks": [ {"name":"GameA","revenue":1000000,"downloads":500000,"rpd":2.0} ]
    }
  ],
  "instructions": "..."
}
```

## 输出：narrative.json（AI 写回）
为每一个 fusion 写 11 个模块文字：
```json
{
  "match3": {
    "concept": "...", "fit": "...", "acquisition": "...", "retention": "...",
    "monetization": "...", "art": "...", "concept_art": "...",
    "status": "...", "success_logic": "...", "ref_mechanics": "...",
    "ref_games": "..."
  },
  "roguelike": { "...": "..." },
  "...": { "...": "..." }
}
```
- 顶层键 `fusion_id` 必须与 scored.json 中 `fusions[].id` 一一对应（通常 10 个）。
- 每个对象必须包含全部 11 个模块键，键名与 scored.json 的 `modules[].key` 一致。
- 缺任何模块，渲染时会用「数据陈述式占位」自动补，但应尽量写满。

## 11 个模块规范（务必遵循字数与格式）
| 键 | 名 | 要求 |
|----|----|------|
| concept | 概念 | 一句话说清玩家在做什么、核心循环是什么，60-110字 |
| fit | 适配 | 为什么这个玩法能嫁接到核心玩法上，引用实测数据佐证，60-110字 |
| acquisition | 吸量点 | 3个吸量钩子，末尾附「吸量要素：」标签行，80-130字 |
| retention | 留存 | 4条留存设计，用｜分隔，40-80字 |
| monetization | 付费 | 参考产品（上线≤5年）+ 付费点 + 目标RPD，40-80字 |
| art | 美术 | 2-3个美术参考锚点，用｜分隔，20-50字 |
| concept_art | 概念图 | 3张概念图描述，玩法向不是氛围向，用｜分隔，40-80字 |
| status | 现状 | 该方向标杆游戏的市场现状：头部产品、规模、涨/卷，60-110字 |
| success_logic | 成功逻辑 | 头部产品为什么能成：核心循环/买量/留存设计，60-110字 |
| ref_mechanics | 可结合玩法 | 这些机制里哪些能嫁接到核心玩法、怎么借，60-110字 |
| ref_games | 可融合的非SLG游戏 | 列出 3-5 个该融合方向对应品类的非SLG游戏（具体产品），点明各自玩法特征与为何能与 SLG 融合，80-130字 |

## 写作硬性要求
1. 只基于 scored.json 的实测数据与评分，不得编造市场数字、下载量、收入。
2. 融合方案「SLG 为根、融合玩法为壳」，一眼能认出是哪两个玩法的结合，不要把所有玩法都往同一个套路上靠。
3. 判断融合玩法形态后再决定怎么融：非对抗/单人/休闲/解谜/建造类保持原有体验节奏，不要强行加对抗和社交。
4. 付费参考产品必须是上线不超过 5 年的真实产品。
5. 「可融合的非SLG游戏」模块只列该融合方向品类对应的非SLG游戏（如「三消」方向列三消游戏、「塔防」方向列塔防游戏），说明各自玩法特征与可融合点；不得列 SLG 竞品。
6. 语言精炼，不写空话套话。
