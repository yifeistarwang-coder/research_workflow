# 脚本用法

脚本是可选工具，主流程见 [SKILL.md](../SKILL.md)。仅在准备运行某个脚本或解释其输出时读取对应章节；已有的学术搜索、论文阅读工具也可以承担这些步骤，无需为使用 Skill 而安装依赖或运行整套脚本。

## 路径与依赖

命令中的 `<skill-dir>` 是包含 `SKILL.md` 的实际目录，`<workdir>` 是本次调研的输出目录。运行前替换所有尖括号占位符，并保留路径外的引号。不要假设运行环境提供同名环境变量或保留前一次命令的工作目录。

使用环境中可用的 Python 命令；下文写作 `python`，也可换成 `python3`。脚本需要 Python 3.9+，网络请求使用标准库。仅 PDF 文本抽取需要 PyMuPDF；优先使用已有环境。确需安装且授权覆盖当前环境时，在项目虚拟环境中执行：

```sh
python -m pip install pymupdf
```

Semantic Scholar key 优先通过 `S2_API_KEY` 提供，避免命令参数和日志暴露密钥；脚本也支持 `--s2-key`，但不要将真实值写入命令记录。OpenAlex 联系邮箱仅在用户已提供并授权用于该服务时，通过 `--mailto` 或 `OPENALEX_MAILTO` 提供。请求会缓存和退避重试，实际可用性取决于服务配额。

## 找候选

标题检索，结合采集到的参考文献频次与引用数查看候选：

```sh
python "<skill-dir>/scripts/find_seeds.py" search "<领域短语>" "<另一种说法>" --pool 150 --top 10 --out "<workdir>/seeds.json"
```

输出 A 表按参考文献出现频次排序，B 表列出其余高引候选。B 会排除 A 中已列出的论文，不能要求两张表出现同一篇论文。候选保留在 `seeds.json`，完整检索池目前不会写入。

从近年综述的参考文献找起点：

```sh
python "<skill-dir>/scripts/find_seeds.py" reverse --topic "<方向>" --reviews 4 --min-freq 2 --out "<workdir>/seeds.json"
```

`reverse` 使用 OpenAlex 的参考文献；`search` 优先使用 S2，获取失败或无计数时会尝试 OpenAlex。查看日志和实际数据来源，再核对重要候选。

## 采集引用关系

`--seeds` 的逗号分隔输入目前接受 OpenAlex W ID。只有 DOI、arXiv ID 或 S2 ID 时，先核对其对应的 OpenAlex 记录，或使用其他工具整理关系。不要把另一类 ID 直接传入该参数。

```sh
python "<skill-dir>/scripts/build_lineage.py" build --workdir "<workdir>" --seeds "<W-id1>,<W-id2>" --pool-from "<workdir>/seeds.json" --topic "<方向>" --budget 60
```

已有 anchor 且没有 `seeds.json` 时省略 `--pool-from`。`--budget` 限制参考文献采集的论文数；身份查询、元数据刷新与分页可能产生额外请求。

输出 `lineage.json` 和 `timeline.md`。脚本用 S2 references 从种子和输入候选向前追溯文献，不自动发现后续引用者。另用近期综述、引用检索或按时间限定的主题检索补查后续工作。多次候选检索使用不同输出文件，以保留记录。

按 ID 查看节点：

```sh
python "<skill-dir>/scripts/build_lineage.py" show --workdir "<workdir>" --ids "<node-id1>" "<node-id2>" --abstract-limit 700
```

部分节点没有摘要，此时另查原文或数据库。节点 ID 通常是 `arXiv:...`、`DOI:...` 或 `S2:...`，与输入的 OpenAlex W ID 不同。

记录选出的主干：

```sh
python "<skill-dir>/scripts/build_lineage.py" tier --workdir "<workdir>" --backbone "<node-id1>,<node-id2>"
```

`--backbone` 每次传入完整的当前主干集合，包括需要认真读的 seed。分类仅代表阅读安排，不能说明 researcher 已读或已确认。暂定选择及理由写在报告中。

`tier` 修改 `lineage.json` 后，`timeline.md` 不会自动更新。交付前同步它的分类；不要通过重跑 `build` 来刷新分类，重建会重新自动分级并覆盖人工选择。

## 取得原文

```sh
python "<skill-dir>/scripts/fetch_pdfs.py" --workdir "<workdir>" --tier backbone,seed --limit 10
```

`--limit` 按实际需要调整。下载文件、分页文本和状态报告写入 `papers/`。节点 ID 中除字母、数字、点、下划线和连字符外的字符替换为下划线，例如 `arXiv:2005.11401` 对应 `arXiv_2005.11401.pdf`。

登记已有 PDF：

```sh
python "<skill-dir>/scripts/fetch_pdfs.py" --workdir "<workdir>" --ids "<node-id>" --register-local "<node-id>=<local-pdf-path>"
```

分页文本用 `===== [p.N] =====` 标识 PDF 页序。使用环境已有的文件检索或阅读工具按章节定位，再按需要查看 PDF 图表。下载或抽取失败会写入 `papers/FETCH_REPORT.md`，命令退出成功不代表每篇都已取得文本。

## 检查文件

仅在采用脚本生成的 `lineage.json` 与目录结构、且需要结构校验时运行一次；输入变化或检查失败时再重跑。不对普通问答、简版报告或手工整理的候选表运行：

```sh
python "<skill-dir>/scripts/verify_survey.py" --workdir "<workdir>"
```

需要保存输出时，用运行环境支持的方式写入 `verify_survey.txt`。它检查笔记覆盖、部分结构与页码标记、部分 ID 格式、引用数和占位词。`--no-related-work` 适用于本次确实没有 follow-up 的情况；`--allow-external` 允许已核对但未登记到图中的外部参考。

这个检查器目前仍按固定格式要求引用数、页码和 gap 关键词，不能完全覆盖主流程允许的网页来源、缺失元数据或简版报告。逐项区分实际错误与格式误报：修正实际错误；对无法取得的信息据实说明，并记录误报原因，不为通过检查补写数字、页码或研究空白，也不为此请求用户批准。检查器不验证研究者阅读状态、引文语义或结论真假；这些内容统一纳入 [一次交付核对](../SKILL.md#一次交付核对)，不再额外重复一轮。

## 数据解释

`lineage.json` 保存采集结果与分类建议，使用前仍需核对论文身份。当前实现可能将同一论文的 DOI 和 arXiv 记录拆成两个节点，缺少标识符的记录也可能冲突；异常节点先核对再纳入主线。

引用数可能混合来源，缓存没有过期时间。`counts_asof` 当前写的是生成日期，不能据此认定每个数值当天都查询过。需要引用数快照时核对数据来源和实际获取时间，必要时用其他工具重新查询并记录。

`co_cite` 受采集样本影响，表中比例也不宜直接解读为领域占比。以参考列表、论文原文和具体关系判断候选，不把自动分数或 `tier` 当作研究结论。
