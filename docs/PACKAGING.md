# 插件打包与发布（TMEAAA-378）

本文档定义 `astrbot_plugin_tmemory` 的官方打包路径，确保产出的 zip 可被
AstrBot（>=4.28）Dashboard 直接安装。

## 背景：为什么不能直接用 Finder 压缩

AstrBot 安装插件时的校验逻辑（`astrbot/core/star/updater.py:inspect_plugin_archive`
和 `astrbot/core/zip_updater.py:_resolve_archive_root_dir`）会：

1. 取 zip 内**所有条目**的 normalized path，按“目录或有子项则取自身、否则取父目录”
   生成候选集合；
2. 对这些候选求 `os.path.commonpath` 作为“压缩包根目录”；
3. 在该根目录下查找 `metadata.yaml` / `metadata.yml`。

macOS Finder「压缩」生成的 zip 会额外写入 `__MACOSX/`（AppleDouble 资源叉，形如
`__MACOSX/._main.py`）。此时候选集合同时包含 `astrbot_plugin_tmemory` 和
`__MACOSX/...`，`commonpath` 退化为空字符串，AstrBot 便去**根级**找
`metadata.yaml` → 不存在 → 报错：

```
ValueError: 压缩包不是合法的 AstrBot 插件：未找到 metadata.yaml 或 metadata.yml。
```

## 官方打包命令

```bash
# 仓库根目录执行；产出 dist/astrbot_plugin_tmemory.zip
tools/pack_plugin.sh

# 自定义输出路径
tools/pack_plugin.sh --output /tmp/astrbot_plugin_tmemory.zip
```

脚本（`tools/pack_plugin.sh` → `tools/plugin_archive.py`）行为：

- 顶层结构固定为单目录 `astrbot_plugin_tmemory/`；
- 采用**白名单**打包，仅包含运行时文件（`main.py`、`metadata.yaml`、
  `requirements.txt`、`_conf_schema.json`、`logo.png`、`README.md`、
  `CHANGELOG.md`、`LICENSE`、`embeddingProvider.py`、`hybrid_search.py`、
  `vector_manager.py`、`core/`、`adapters/`、`search/`、`web/`、`pages/`、
  `templates/`、`skills/`、`.astrbot-plugin/`）；
- 排除 `__MACOSX`、`.DS_Store`、`._*`、`__pycache__`、`*.pyc`、`.env`、
  `tests/`、`data/`、`docs/`、`docker/`、`.git`、`.venv` 等开发/垃圾内容；
- 打包后立即按 AstrBot 同规则校验，失败则非零退出（可用于 CI / 发布门禁）。

## 校验已有压缩包

```bash
python3 tools/plugin_archive.py verify dist/astrbot_plugin_tmemory.zip
```

校验内容：

- 无 `__MACOSX` / `.DS_Store` / `._*` / `__pycache__` / `.env` 等垃圾条目；
- 顶层目录唯一为 `astrbot_plugin_tmemory/`；
- `_resolve_archive_root_dir` 结果为 `astrbot_plugin_tmemory`；
- 能命中 `astrbot_plugin_tmemory/metadata.yaml`。

通过时输出：

```
[verify] OK dist/astrbot_plugin_tmemory.zip entries=79 root='astrbot_plugin_tmemory' metadata='astrbot_plugin_tmemory/metadata.yaml'
```

## CI 门禁

`.github/workflows/pytest.yml` 的 `unit` job 会执行
`python3 tools/plugin_archive.py pack --output dist/astrbot_plugin_tmemory.zip`，
打包校验不通过即 CI 失败。`tests/test_plugin_archive.py` 复现了
`__MACOSX` 导致根目录退化的回归场景。

## 发布清单

1. `tools/pack_plugin.sh` 生成 zip；
2. 确认校验输出 root/metadata 正常、`__MACOSX=False`；
3. 上传该 zip 到 AstrBot Dashboard 或随 Release 分发；
4. 切勿重新用 Finder「压缩」或 `ditto`（默认 `--sequesterRsrc` 会再次引入
   `__MACOSX`）。
