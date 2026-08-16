# 融合玩法立项机会研究（slg-fusion-research）

给定「一个核心玩法 + 若干融合玩法候选」，自动完成：

**采集市场数据 → 五维打分（吸量/空间/竞争/留存/付费）+ ROI 排序 → 撰写立项方案卡 → 生成可交互 HTML 报告**

- 想用真实数据：填你自己的 Sensor Tower token（脚本自动读取，不依赖任何外部账号）。
- 想让 AI 写方案卡：**优先用 WorkBuddy 自带 AI**（不花外部 API、不走外部模型）；也可以填一个兼容 OpenAI 的模型 key 走自动生成。
- 没有 AI key 也能跑：用 `--dry` 占位方案卡验证流水线；但**核心市场数据必须有 Sensor Tower token**（无 token 不生成报告，杜绝示例/占位数据冒充真实结论）。

---

## 1. 适用对象

| 你是… | 怎么用 |
| --- | --- |
| **WorkBuddy 用户** | 把本文件夹整体放进 `~/.workbuddy/skills/slg-fusion-research/`，然后对话里说「帮我做 SLG 融合玩法调研」即可，AI 会接管全流程。详见第 4 节。 |
| **只用 Python 的人** | 不需要 WorkBuddy，本文件夹就是个普通 Python 项目，按第 3 节命令行运行即可。 |

> 本文件夹**自包含**：代码、配置模板、报告模板都在里面，发给别人解压即用（密钥用 `config.user.vbs.example` 模板，不含真实数据）。

---

## 2. 前置条件

- **Python 3.11+**（3.13 也验证过）。
- 安装依赖（在能联网的 Python 下执行一次）：
  ```bash
  python -m pip install -r requirements.txt
  ```
  依赖只有三个：`PyYAML`、`Jinja2`、`requests`。
- （可选）真实 Sensor Tower 数据需要你自己的 `SENSORTOWER_AUTH_TOKEN`，去 <https://app.sensortower.com> 账号设置复制。

---

## 3. 最快上手（零配置，1 条命令）

在**本文件夹内**执行：

```bash
# Windows
python src/run.py --config configs/slg.yaml --out report.html

# macOS / Linux
python3 src/run.py --config configs/slg.yaml --out report.html
```

- 没有 token → 工具会直接拒绝生成，并提示如何配置 `SENSORTOWER_AUTH_TOKEN`（本工具仅使用 ST 真实数据，无示例/占位数据降级）。
- 已放好 `config.user.vbs`（见第 5 节）→ 自动用你的真实 Sensor Tower 数据。

Windows 用户直接双击 `run.vbs`（自动读取 `config.user.vbs` 里的密钥）；macOS/Linux 用户用 `python3 src/run.py ...`。

---

## 4. WorkBuddy 用户：把 AI 写方案卡这一步交给 WorkBuddy

1. 把本文件夹复制到：`~/.workbuddy/skills/slg-fusion-research/`
   （`~` 在 Windows 上通常是 `C:\Users\你的用户名\.workbuddy`）
2. 在 WorkBuddy 里对话，例如：
   - 「帮我做 SLG 融合玩法调研」
   - 「生成立项机会报告（SLG × 各融合玩法）」
3. WorkBuddy 会按「AI 手桥」三步自动跑完（脚本负责采集+打分+渲染，AI 负责写方案卡文字）：
   - **① 跑数据 + 导出给 AI 看的数据**
     ```bash
     python <本技能>/src/run.py --config <本技能>/configs/slg.yaml --dry \
            --dump-scored scored.json --out report_draft.html
     ```
   - **② WorkBuddy 自身 AI 读 `scored.json`，为每个方向写 10 个模块的文字**，输出 `narrative.json`
     （结构：`{ "<方向id>": { "<模块key>": "<文字>", ... }, ... }`，规范见 `references/narrative_schema.md`）。
     **不得编造市场数字、下载量、收入**，一切以 `scored.json` 实测为准。
   - **③ 渲染最终报告**
     ```bash
     python <本技能>/src/run.py --config <本技能>/configs/slg.yaml \
            --narrative-json narrative.json --out report.html
     ```
4. WorkBuddy 把 `report.html` 用预览 / 附件交付给你。

> 不想走 AI 手桥、想一条命令出完整报告？填好 `LLM_API_KEY`（第 5 节）后直接：
> ```bash
> python src/run.py --config configs/slg.yaml --out report.html
> ```

---

## 5. 配置你自己的密钥（真实数据 / 外部 AI）

1. 复制模板：
   ```bash
   cp config.user.vbs.example config.user.vbs
   ```
2. 用记事本打开 `config.user.vbs`，把 `SENSORTOWER_AUTH_TOKEN=在此填入...` 换成你的真实 token。
3. 把文件放在以下**任意一处**都能被自动读取：
   - 本文件夹根目录（推荐）；
   - 你的 `--config` 配置文件所在目录（研究工作区）；
   - 你运行命令时的当前目录。
4. （可选）若想让脚本自动调外部模型写方案卡，在 `config.user.vbs` 里填 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`。

> Windows 上 `config.user.vbs` 是一个会被 `run.vbs` 解析的 VBScript 配置文件（用 `SetEnv "KEY", "VALUE"` 或 `KEY = "VALUE"` 写法）。
> macOS/Linux 上同样把它放在上述任一位置即可；你也可以不建这个文件，直接在终端 `export SENSORTOWER_AUTH_TOKEN=你的token` 后运行。

⚠️ **安全**：`config.user.vbs` 含你的私钥，**不要把它随包发给别人**。发别人时只发本文件夹里**除 `config.user.vbs` 之外**的内容即可（模板 `.example` 不含密钥，可放心发）。

---

## 6. 换成你自己的研究对象

报告内容完全由 `configs/slg.yaml` 决定。换研究对象（比如 RPG、三消、放置等）只需改这个文件的 `meta` 和 `fusions` 两段：

- `meta.core_genre`：核心玩法名（如 `RPG`）。
- `fusions`：融合候选清单，每项含 `name`（融合玩法名）、`fusion_role`（副玩法/外壳/核心替换/养成层）、`st_subgenre`（在 ST 里的子品类名）、`seeds`（已上线的代表产品，是种子产品法的入口）、`hypothesis`（立项假设）。

改完用 `--config 你的文件.yaml` 运行即可。仓库里还附了几个参考配置：
- `configs/slg_expanded.yaml`：SLG × 32 个方向的扩展示例。
- `configs/rpg.yaml`：以 RPG 为核心玩法的示例。
- `configs/fusion_library.yaml`：融合玩法素材库，写 `fusions` 时可参考。

> 种子产品清单是所有实测数据的入口，务必人工复核；没有种子时相关维度显示「无相关数据」，不影响其它维度打分。

---

## 7. 文件结构

```
slg-fusion-research/
├── SKILL.md                  # WorkBuddy 技能定义（AI 读它来决定怎么调用）
├── README.md                 # 本文件
├── requirements.txt          # Python 依赖
├── config.user.vbs.example   # 密钥模板（复制成 config.user.vbs 再填）
├── run.vbs                   # Windows 一键启动（读 config.user.vbs，带菜单）
├── src/                      # 全部代码
│   ├── run.py                # 编排入口（采集 → 打分 → 方案卡 → 渲染）
│   ├── collect.py            # 数据采集（Sensor Tower API 或手工 CSV）
│   ├── st_client.py          # ST API 客户端 + 连通性自检
│   ├── scorecard.py          # 五维打分 + ROI
│   ├── narrative.py           # 方案卡生成（外部 LLM / 占位 / 读回 AI 手桥）
│   └── expand.py             # 配置展开工具
├── configs/                  # 配置模板（slg / slg_expanded / rpg / fusion_library）
├── templates/                # 报告 HTML 模板（report.html.j2）
├── data/                     # 数据目录
│   ├── cache/                # API 结果缓存（重跑更快）
│   └── cache/                # ST API 原始响应缓存（落盘便于复现/审计）
└── references/               # 规范文档
    ├── narrative_schema.md   # 方案卡 JSON 结构与 10 个模块规范
    └── pipeline.md           # 流水线细节
```

---

## 8. 常见问题

**Q：没填 token 能跑吗？**
不能。本工具仅使用 Sensor Tower 真实数据；没有 token 时不会生成报告（会提示如何配置），不会用任何示例/占位数据。可用 `--dry` 仅生成占位方案卡先验证环境，但核心市场数据仍需 ST token。

**Q：想要真实数据但没 ST 账号？**
本工具仅支持 Sensor Tower API 真实数据，不再支持手工 CSV 导入。可让同事/对方在 Sensor Tower 网页端导出数据后，由你用其 `SENSORTOWER_AUTH_TOKEN` 接入 API 跑真实结论。

**Q：报「网络超时 / 连接失败 / 读取超时」？**
- 连接失败/超时：多为国内直连 Sensor Tower 受限或需代理。设置 `HTTPS_PROXY` 后再跑（`config.user.vbs` 里有模板行）。
- 读取超时（>几分钟无返回）：模型或接口太慢。可在 `config.user.vbs` 里加 `LLM_READ_TIMEOUT=600` 调大；或改用 `--dry` 占位 / WorkBuddy AI 手桥（不经过慢模型）。
- 脚本启动时会先做连通性自检，几秒就能明确告诉你连没连上，不会傻等。

**Q：触发 429 限流？**
方案卡生成内置自适应降速：遇到 429 会自动把并发从默认 2 降到 1 并重试，无需手动干预。

**Q：中文路径/中文控制台乱码或报错？**
脚本已做 GBK 控制台兜底（无法编码的字符替换为 `?`，中文本身正常）。路径含中文也支持。

**Q：改了配置不生效？**
确认是用 `--config 你的文件.yaml` 指定了；不指定时默认读 `configs/slg.yaml`。

---

## 9. 安全须知（发给别人前必读）

- **只发不含 `config.user.vbs` 的文件夹**。`config.user.vbs.example` 是空模板，可放心发；填好真实密钥的 `config.user.vbs` 绝不能外发。
- 评分层所有数字永远来自实测/配置，AI（无论 WorkBuddy 还是外部模型）只写文字、改不了分数，因此不会出现「AI 编数据」污染结论的情况。
- 标杆竞品扫描需要 ST token；无 token 时报告中该板块显示「需配置 token」，不报错。
