# 面板接入与排错

YYB Go Enhanced 支持青龙、呆呆和 Arcadia。连接信息既可在 Web 控制台的“面板连接设置”中保存，也可通过环境变量提供；控制台保存的配置优先。

## 青龙

```dotenv
PANEL_TYPE=qinglong
QL_URL=http://qinglong:5700
QL_CLIENT_ID=你的 Client ID
QL_CLIENT_SECRET=你的 Client Secret
YYB_QINGLONG_SERVER=yyb-go:8000
YYB_QINGLONG_REPO=525815266_YYB-Go-Enhanced_main/scripts
```

`QL_URL` 是 YYB Go 访问青龙的地址；`YYB_QINGLONG_SERVER` 是青龙脚本反向访问 YYB Go 的地址。两者方向不同。

同一个 Docker 网络内可以使用容器名。跨服务器部署必须填写实际局域网 IP 或域名，不能使用另一台机器上的 `127.0.0.1` 或 Docker 容器名。

## 呆呆面板

```dotenv
PANEL_TYPE=daidai
DAIDAI_URL=http://daidai-panel:5700
DAIDAI_APP_KEY=你的 App Key
DAIDAI_APP_SECRET=你的 App Secret
YYB_QINGLONG_SERVER=yyb-go:8000
```

保存连接时如果面板类型选错，且目标返回可识别的 `404` 或 `405`，系统会尝试另一种兼容驱动。连接成功后仍以最终识别的面板类型保存。

## Arcadia

```dotenv
PANEL_TYPE=arcadia
ARCADIA_URL=http://arcadia:5678
ARCADIA_TOKEN=你的 Token
YYB_QINGLONG_SERVER=yyb-go:8000
```

Arcadia Token 至少需要以下权限：

```text
env:query env:manage cron:query cron:manage cron:run file:list file:read
```

## 同步 YYB_SERVER

扫码完成后可直接把账号合并到面板中的 `YYB_SERVER`。重复同步不会重复写入相同账号。

```text
http://yyb-go:8000@1
http://yyb-go:8000@账号OpenID
```

账号引用使用数字 ID 还是 OpenID，由 `YYB_QINGLONG_REF_MODE` 控制。每行只能包含一个服务地址和一个账号引用，不要附加 `/login`、`/wxapp/getCode` 或查询参数。

## 账号运行管理

“账号运行管理”按账号维护面板任务：

- 从配置的脚本目录选择可调用脚本；
- 创建、更新、启用、禁用或立即运行账号任务；
- 为账号设置独立推送方式；
- 按绑定任务 ID 获取该账号最近日志，避免不同账号串日志；
- 日志抽屉增量刷新，并在用户向上阅读时保持当前位置。

面板任务仍是执行面。YYB Go 负责配置、账号隔离与调用，不会代替面板调度器常驻执行脚本。

## 脚本拉取工具

在青龙容器内安装：

```bash
curl -fsSL https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/tools/yyb-scriptctl.sh \
  -o /ql/data/scripts/yyb-scriptctl.sh
chmod +x /ql/data/scripts/yyb-scriptctl.sh
```

三种模式：

```bash
# 同步整个 scripts/ 目录，不创建任务
bash /ql/data/scripts/yyb-scriptctl.sh sync

# 拉取一个脚本，不创建任务
bash /ql/data/scripts/yyb-scriptctl.sh pull 麦富迪_code版.py

# 拉取脚本，并创建或更新任务
bash /ql/data/scripts/yyb-scriptctl.sh install 麦富迪_code版.py \
  --cron "13 1 * * *" --name "麦富迪"
```

`install` 未提供 cron 时会拒绝隐式创建，防止拉取动作意外产生错误任务。更多说明见[脚本管理](../scripts/README.md#青龙脚本拉取工具)。

## 跨服务器排错

请从实际执行脚本的容器内测试，不能只在浏览器中打开服务首页：

```bash
curl -i --max-time 10 http://YYB服务器IP:8000/health
curl -i --max-time 10 -X POST http://YYB服务器IP:8000/wxapp/getCode \
  -H 'Content-Type: application/json' \
  -d '{}'
```

预期结果：

- `/health` 返回 `200` JSON；
- 空请求体调用 `/wxapp/getCode` 返回 `400` JSON，并提示缺少 `ref`，但不会实际生成 code。

常见现象：

| 现象 | 排查方向 |
| --- | --- |
| 根路径显示登录页 | 正常；根路径是控制台，不是协议接口 |
| `302`/`303` 跳转 `/login` | 请求了管理接口、URL 拼错，或外层反代改写了路径 |
| 返回 HTML 而不是 JSON | 检查统一登录、Basic Auth、路径前缀和自动跟随跳转 |
| `404` JSON | 接口路径或大小写错误，正确路径为 `/wxapp/getCode` |
| `405` JSON | 取码接口需要 `POST` |
| `401` JSON | 管理接口需要网页登录，或协议令牌未携带 |
| 容器名无法访问 | 调用方不在同一个 Docker 网络，改用局域网 IP |
| 日志读取失败 | 核对面板 API 权限、任务 ID 和面板返回是否被反代截断 |

诊断重定向时不要使用 `curl -L`，否则会跟随到登录页并隐藏最初的状态码。

## 安全建议

- 面板 Secret 只保存在服务端，不要写入前端脚本或 Issue。
- 跨服务器优先使用可信局域网或 VPN，并限制 8000 端口来源。
- 公网调用协议接口时启用 `YYB_PROTOCOL_TOKEN`。
- 面板连接测试成功不代表脚本可以回连 YYB；两个方向都需要单独验证。
