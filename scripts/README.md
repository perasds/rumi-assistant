# 脚本范围

在仓库根目录运行脚本，先按根 README 安装 Python 及 `scripts/package-lock.json` 中的依赖。

## 独立可用工具

- `check_materials.py`：本地材料预检，不登录游戏。
- `manual_captcha.py` / `manual_captcha_host.mjs`：由后端调用的人工官方验证码窗口及网页代理；需要本机 Chrome / Chromium。
- `local_credential_form.py` / `probe_account_login.py`：本机手动登录诊断入口，只对自己的账号使用。它们产生的私有运行数据不属于发布文件。
- `import_pet_catalog.py --source PATH --output data/pets`：从使用者有权使用的、结构匹配的目录导入图鉴索引及图片。不会从 APK 自动生成完整图鉴。
- `export_public_book.py`：将已构建的纯图鉴前端和当前图鉴数据导出为静态站点。
- `verify_runtime.py`：本地服务验收辅助工具；需要按脚本要求准备资源。

## 保留的维护源码

以下脚本是维护工具源码，依赖**本仓库未分发**的原始研究输入及辅助分析模块，不能直接在全新克隆中运行：

| 脚本 | 缺少的外部输入 / 模块 |
| --- | --- |
| `sync_pet_details.py` | 经授权的资源、`luban_pet_decoder`、`static_bundle_options`、`static_survey`、`lgeb_public_bundle_probe`、UnityPy |
| `generate_order_protocol.py` | `adventure_config_probe`、`home_config_probe`、`managed_descriptor_probe`、`protocol_schema_subset` |
| `generate_redbag_protocol.py` | 家园 / 聊天研究输入、`home_hot71_resources`、`pet_details_fast_source_probe`、协议辅助模块、dncil |
| `build_season_schema.py` / `generate_season_tags.py` | 赛季 schema 与已核对的配置输入 |
| `sync_home_management.py` / `sync_home_production.py` | 家园配置研究输入 |

这些脚本不是服务启动步骤。发布的业务运行代码使用仓库内已有的最小协议与配置；不要为更新游戏版本而直接修改固定哈希、放行未验证的协议。

不分发上述外部工具和研究材料，也不从公开仓库自动下载它们。请先核对源码中的输入路径和资源权利，再考虑自行补充。
