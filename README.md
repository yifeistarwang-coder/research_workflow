# Research Workflow

记录和实践 AI 辅助科研的工作流程。目前包含文献调研 skill `lit-survey`，以及科研与读博心得 `reflection`。

项目关注研究者如何建立对一个方向的理解：从奠基论文出发，追踪关键问题和方法的变化，把精读时间放在重要论文上。AI 负责检索、整理证据、准备导读和比较后续工作；研究者通过阅读原文、实践和反馈形成自己的判断。

## 从一次文献调研开始

让具备文献检索、文件读写和论文阅读能力的 AI agent 读取 [lit-survey/SKILL.md](lit-survey/SKILL.md)，再描述你的研究方向、调研用途、已知起点论文和阅读预算。例如：

> 请读取并按照 `lit-survey/SKILL.md`，帮我调研 RAG 中自适应检索的发展。关注近五年的进展，允许追溯更早的奠基论文。我的目标是确定研究选题，预计能精读 6 篇论文。请自行选择起点，整理研究主线、主干论文导读和 follow-up 综述。关键关系需要原文依据，尚未核对的地方明确标注。结果保存到 `surveys/adaptive-rag/`。

如果使用的 agent 支持加载 skill，也可以在按该工具要求加载后，通过 `$lit-survey` 引用它。具体加载方式取决于所用工具；直接指定 `SKILL.md` 路径即可让 agent 阅读流程说明。

执行调研可以使用 agent 已有的学术搜索和论文阅读工具。仓库附带的 Python 脚本是可选辅助，使用流程本身无需先安装这些脚本的依赖。

## lit-survey 如何工作

| 步骤 | 要解决的问题 | 主要产出 |
| --- | --- | --- |
| 确定范围 | 研究什么、为什么调研、能投入多少阅读时间 | 范围说明与暂定假设 |
| 找奠基工作 | 哪些论文提出了后续持续采用的问题或方法 | 少量起点候选及选择依据 |
| 串起主线 | 后续工作保留了什么、改变了什么，是否出现分叉 | 关键转折、关系依据与主干候选 |
| 准备主干导读 | 方法怎样工作，证据在哪里，应该带着什么问题读 | 每篇主干的导读与原文定位 |
| 总结 follow-up | 中间工作相对主干做了哪些修改，何时值得补读 | 按共同问题组织的 related work |
| 交付与核对 | 阅读顺序是否清楚，哪些结论仍需核验 | 调研报告、阅读建议与待核对问题 |

**主干论文（backbone）**包括奠基工作，以及改变问题设定、方法思路或关键假设的论文，由研究者认真读原文。**后续工作（follow-up）**由 agent 比较并总结，研究者按需补读。分类会随证据和研究问题调整。

引用数用于初筛，选择时还需考虑领域规模、发表时间和具体贡献。论文之间存在引用，并不自动证明方法继承；关键关系需要原文或可核对的比较支持。导读分别记录 agent 实际查看的材料和研究者的阅读反馈，只有收到明确反馈才更新研究者的已读状态。

## 调研交付

内容先以 Markdown 保存。一次调研的输出可以组织为：

```text
surveys/adaptive-rag/
├── scope.md                  # 持续调研时记录范围、用途与假设
├── survey.md                 # 研究主线、关键转折、阅读顺序与疑点
├── backbone/                 # 每篇主干的导读与研究者阅读反馈
└── digests/
    └── related_work.md       # 按问题组织的后续工作比较与综述
```

若检测到可用的 **pdfLaTeX、XeLaTeX 或 LuaLaTeX** 环境，agent 将最终报告编译为 `survey.pdf`，作为主要交付文件，并保留 Markdown 与 LaTeX 源文件。PDF 包含主线报告、主干导读和 related work，便于独立阅读；中文优先使用 XeLaTeX 或 LuaLaTeX。

没有可用 LaTeX 环境，或现有字体、宏包不足以完成编译时，继续交付 Markdown 并说明原因，不要求为此安装 LaTeX。PDF 编排与编译是 agent 的交付步骤，目前仓库没有专门的报告 PDF 生成脚本。

使用附带脚本时，还会按执行步骤生成 `seeds.json`、`lineage.json`、`timeline.md` 和 `papers/` 等辅助材料。格式和示例见 [报告规范](lit-survey/references/writing-survey.md) 与 [报告模板](lit-survey/assets/survey-template.md)。

## 可选脚本

脚本需要 Python 3.9+。网络请求使用标准库，PDF 文本抽取另需 PyMuPDF：

```sh
python -m pip install pymupdf
```

| 脚本 | 用途 |
| --- | --- |
| `find_seeds.py` | 按主题检索，或从近期综述的参考文献寻找奠基论文候选 |
| `build_lineage.py` | 采集参考文献关系，生成图数据和时间线，记录主干分类 |
| `fetch_pdfs.py` | 下载 PDF 或登记已有本地文件，抽取带 PDF 页码的文本 |
| `verify_survey.py` | 检查笔记覆盖、部分结构、标识符和占位内容 |
| `oalib.py` / `s2lib.py` | OpenAlex 与 Semantic Scholar 的接口及数据处理 |

在仓库根目录执行下面的示例，可以检索起点候选：

```sh
python lit-survey/scripts/find_seeds.py search "adaptive retrieval" "retrieval augmented generation" --pool 150 --top 10 --out "surveys/adaptive-rag/seeds.json"
```

Semantic Scholar API key 可通过 `S2_API_KEY` 或 `--s2-key` 提供；OpenAlex 联系邮箱可通过 `OPENALEX_MAILTO` 或 `--mailto` 提供。实际服务可用性取决于网络、服务配额与访问条件。

候选检索之后仍需核对论文身份、选择起点、补查后续工作和阅读原文。完整命令、ID 格式与参数说明见 [脚本用法](lit-survey/references/script-usage.md)。

### 论文来源与全文获取

调研可使用 DOI、arXiv ID、数据库记录或原文链接核对论文身份。原文可以来自期刊或会议官网、开放仓储，以及通过机构订阅取得的本地 PDF；网页全文也可以按章节定位证据。

当前下载脚本利用节点中已有的 PDF 或开放获取 URL，并在有 arXiv ID 时提供 arXiv 下载入口。它尚未集成数据库登录或多来源全文检索。需要机构访问的论文，可以通过图书馆或数据库取得 PDF，再使用 `fetch_pdfs.py --register-local` 登记，具体命令见脚本用法。

原文暂时拿不到时，保留论文条目，标明现有材料、版本和待核对内容。只看过摘要的结论按摘要级证据表述，关键实验数字和页码应对应实际阅读的版本。

### 当前限制

- `build_lineage.py` 主要沿参考文献追溯前作，不自动发现后续引用者；需要另用引用检索、近期综述和主题检索补查后续发展。
- 同一论文的 DOI 与 arXiv 记录可能被拆成不同节点；引用数也可能混合来源或来自旧缓存，关键记录需要核对。
- 调整主干分类后，`timeline.md` 不会自动同步，需要在交付前更新。
- `verify_survey.py` 的 `OVERALL PASS` 仅表示程序检查项通过。检查器无法验证研究结论、关系语义或研究者阅读状态，固定格式检查也不能覆盖所有合法的交付形式。

## 仓库结构

```text
.
├── README.md
├── lit-survey/
│   ├── SKILL.md              # 文献调研流程入口
│   ├── agents/openai.yaml    # 界面名称、描述与默认提示词
│   ├── references/           # 领域梳理、阅读、写作和脚本说明
│   ├── assets/               # 导读、报告与 related work 的模板和示例
│   └── scripts/              # 检索、引文采集、PDF 处理与结构检查
└── reflection/
    └── README.md             # 科研与读博心得
```

`assets/` 中的示例使用虚构论文演示写法，不能作为真实文献引用。[reflection](reflection/README.md) 记录通过实践理解论文、博士阶段试错和亲自阅读原文等体会。
