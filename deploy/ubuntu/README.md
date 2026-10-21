# Ubuntu 部署参考

模板使用 `example.com` 和 `/opt/rumi-assistant`，不包含可直接访问的服务器、证书或密钥。请先阅读根目录 README 完成依赖安装、材料预检和本机验证，再按自己的布局修改模板。

## 纯图鉴

1. 构建 `npm --prefix frontend run build:book`。
2. 用 `scripts/export_public_book.py` 导出公开目录。
3. 将输出放到 `/opt/rumi-assistant/public-book-current`（可用自己管理的版本目录和软链接）。
4. 修改 `rumi-book.nginx.conf` 的域名和 TLS 证书路径，执行 `nginx -t` 后启用。

纯图鉴不需要 Python 后端、游戏登录或账号材料。更新时只替换静态站点；不要同时启用完整服务模板。

## 完整版自托管

`rumi-assistant.service` 和 `rumi-assistant.nginx.conf` 是参考布局：

```text
/opt/rumi-assistant/current/rumi-market/    本仓库根目录
/opt/rumi-assistant/venv/                  Python 虚拟环境
/opt/rumi-assistant/node/bin/              Node 可执行文件
/opt/rumi-assistant/shared/runtime/        私有运行数据
/etc/rumi-assistant.env                   私有环境配置
```

创建独立系统用户 `rumi-assistant`；源码与依赖由管理员管理，只允许服务用户写入私有运行目录。将仓库的 `runtime` 路径指向上述共享运行目录，再按实际路径修改 systemd 的 `ReadWritePaths`。该目录不属于网页根目录，不随更新覆盖。

安装根 README 所列的 Python、前端及 `scripts` Node 依赖，并构建完整版前端。安装 Chromium 及系统运行依赖，在私有环境文件中设置 `RUMI_APK_PATH`、`RUMI_CHROMIUM_EXECUTABLE` 和 `RUMI_CAPTCHA_MODE=web`；环境文件权限应限制为管理员可读。不要在公开模板中填写凭据。

先以服务用户执行预检。确认 TLS、反向代理同源转发、文件权限与回环 API 正常，再安装并启动单 worker 服务。Nginx 配置中的 `/api` 指向 `127.0.0.1:18082`；不要把该端口或源码目录直接暴露到公网。

`rumi-assistant-renew.*` 为现有 certbot 布局的参考，需自行安装 certbot、申请证书并调整路径后使用。证书续签失败必须自行监控。

升级前检查并停止自动任务；后端重启会结束游戏会话及任务。私有运行目录、账号数据和 TLS 材料应独立备份，禁止打包进代码发布。
