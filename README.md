# 噜咪小助手 · Rumi Assistant

基于 Vue 3、TypeScript 与 FastAPI 的非官方游戏辅助工具。此仓库公开本地完整版源码，并提供可独立部署的纯图鉴构建。

## 功能

- 图鉴：名称 / ID 搜索、单选元素筛选、基本属性、特性、固定技能及可替换技能的展示。
- 登录：密码或短信验证码登录；官方安全验证由使用者手动完成；浏览器到后端的登录请求体加密。
- 交易：使用游戏一口价排序、宠物及赛季筛选、按价格 / 数量 / 金币余额限制自动监测购买、取消任务。
- 订单：订单计划、库存保留、生产调度、定时收取产出及工作巡检。
- 我的：个人信息、金币状态及自动领取启动后收到的新红包。

自动任务依赖后端进程和有效游戏会话；关闭网页不等同于停止任务，退出登录或后端重启会结束相应会话 / 任务。

## 分发边界

这是完整业务源码，不是包含游戏客户端和素材的即用安装包。

- 仓库保留最小协议 schema、校验元数据及非账号配置事实。
- 图鉴附带名称与基础属性；游戏图片、技能 / 特性文案未分发，界面会显示缺失状态。完整展示需要自行提供有权使用的图鉴资源。
- 真实登录还需要使用者自备与适配器匹配的官方 APK，配置 `RUMI_APK_PATH`。本项目保留哈希和协议校验，不通过修改摘要绕过版本检查。
- 不包含账号密码、验证码、密钥、登录记录、数据库、运行日志、部署历史、原始 APK / 程序集、抓包或研究材料；从全新 Git 历史发布。
- 部分历史资源生成脚本依赖未分发的研究辅助模块。目前没有“仅凭任意 APK 一键重建全部协议与图鉴”的完整工具链。正常启动不需要执行这些生成脚本。

第三方内容及许可证边界见 [NOTICE.md](NOTICE.md)。游戏服务更新、限流或账号状态可能使真实功能失效；本仓库的单元测试通过不代表当前官方服务已验证可用。只对自己的账号及有权访问的服务使用，遵守相关服务规则。

## 环境与安装

需要 Python 3.12+、Node.js 22.22.2+。以下命令在仓库根目录运行。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
npm --prefix frontend ci
npm --prefix scripts ci
```

Linux / macOS：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e backend
npm --prefix frontend ci
npm --prefix scripts ci
```

### 先运行离线界面

此入口禁用真实游戏登录，适合查看图鉴、开发界面及 API；不会使用游戏账号。

```powershell
.\.venv\Scripts\python.exe -m rumi_market.preflight --framework-only
.\start.ps1 -FrameworkOnly
```

打开 `http://127.0.0.1:5173/`。停止本启动器创建的进程：

```powershell
.\start.ps1 -Stop
```

跨平台也可在两个终端分别运行：

```sh
python -m uvicorn rumi_market.app:app --app-dir backend/src --host 127.0.0.1 --port 8000
npm --prefix frontend run dev
```

使用虚拟环境中的 Python；本地开发默认仅监听回环地址。

### 启用真实账号功能

配置自备 APK 和已安装的 Chrome / Chromium 可执行文件。没有桌面的服务器使用网页人工验证码模式。示例中的路径需替换为自己的实际路径：

```powershell
$env:RUMI_APK_PATH = 'D:\YourAuthorizedResources\base.apk'
$env:RUMI_CHROMIUM_EXECUTABLE = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$env:RUMI_CAPTCHA_MODE = 'web'
.\.venv\Scripts\python.exe -m rumi_market.preflight
.\start.ps1
```

Linux 可使用同名环境变量，并将 API 入口改为 `rumi_market.live_app:app`。预检只核对本地材料，不登录或购买。请先确认预检结果，再通过网页手动登录。

可选 `RUMI_NODE_EXE` 指定 Node 可执行文件；`RUMI_API_TARGET` 指定 Vite 代理使用的后端地址。不要将真实配置或资源提交到 Git。

## 测试与构建

```sh
python -m pytest backend
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run build:book
node --test scripts/manual_captcha_web.test.mjs
```

后端测试从 `backend/src` 加载代码。依赖原始研究材料或未分发游戏素材的用例单独标记，默认不运行；合成 fixture 和业务逻辑测试保持可独立执行。具体开关见 `backend/tests/conftest.py`。涉及手动浏览器的测试需要本机 Chromium。

完整版前端输出在 `frontend/dist/`。独立图鉴构建输出在 `frontend/dist-book/`，还需要导出公开数据：

```sh
python scripts/export_public_book.py --frontend frontend/dist-book --catalog data/pets/catalog.json --output outputs/public-book
```

输出目录必须尚不存在。纯静态托管 `outputs/public-book/` 即可；该目录不包含登录、交易、订单或个人 API。未提供的图片和技能文案不会被导出。

## 目录与部署

| 目录 | 内容 |
| --- | --- |
| `frontend/src/` | 完整 Vue 界面及测试 |
| `backend/src/rumi_market/` | API、会话、游戏适配器、任务及协议 codec |
| `backend/tests/` | 业务和协议测试 |
| `data/` | 非账号配置事实；缺失素材由使用者自行提供 |
| `scripts/` | 人工验证码、图鉴导入 / 导出及资源辅助工具 |
| `deploy/ubuntu/` | Nginx / systemd 参考模板，域名使用 `example.com` |

部署说明见 [deploy/ubuntu/README.md](deploy/ubuntu/README.md)。公开站点可只部署静态图鉴；若自行部署完整账号功能，必须使用 HTTPS，并保护运行目录。请求体加密不能替代 HTTPS。单进程服务在内存维护游戏会话和任务，不支持直接增加多个独立 worker。

## 贡献

提交前运行相关测试，并保持改动小且易审查。不要在 Issue、测试 fixture 或提交中附上真实账号、会话、验证码、APK、游戏素材或原始研究数据。报告官方服务兼容性问题时只提供脱敏错误码及版本信息。

原创代码采用 [MIT](LICENSE) 许可证。
