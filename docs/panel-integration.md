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

## 独立上传的脚本不在全部脚本里（Issue #73）

“账号运行管理 → 全部脚本”从自动化面板的任务中读取脚本，且只匹配 `YYB_QINGLONG_REPO` 中的目录；它不是青龙文件管理器。**单独上传文件、只执行 `pull`，或任务命令的目录不匹配，都不会显示。**

以单独上传 `example.py` 为例：

1. 确认脚本支持通过 `YYB_SERVER` 读取账号。上传至青龙的 `local_yyb/example.py`（容器路径 `/ql/data/scripts/local_yyb/example.py`）。
2. 在 YYB 服务的 `YYB_QINGLONG_REPO` 原值后追加 `,local_yyb`，保留已有目录，重建 / 重启 YYB 服务使配置生效。青龙任务里的环境变量不会改变 YYB 服务自身配置。
3. 在青龙“定时任务”新建任务，命令为 `task local_yyb/example.py`，填写适合这个脚本的 Cron。若只希望 YYB 按账号执行，应将此原始任务设为**禁用**；禁用任务仍会被列出，避免它与 YYB 生成的账号任务重复执行。
4. 回到 YYB“账号运行管理”，点击“刷新状态”，切换到“全部脚本”，搜索脚本名，为选中的账号配置 / 启用任务。

也可直接上传到已配置的目录，此时无需修改 `YYB_QINGLONG_REPO`。这里的目录相对于青龙 `scripts` 目录，不能写成 `/ql/data/scripts`。文件名含空格、命令附带参数或 shell 包装的复杂命令不属于当前自动识别格式，请先用普通的 `task 目录/文件.py` 或 `.js` 命令。

目录匹配仅代表脚本可被发现，不会自动把依赖其他 Cookie / Token 的脚本改造成 YYB 脚本。

## 第三方脚本订阅与更新

第三方仓库的文件更新由青龙「订阅管理」处理；YYB 继续读取面板任务。配置公开仓库 Git 地址、分支、文件筛选及拉库定时，手动运行订阅即可立即更新，不需要反复上传。

例如 Issue #73 提到的 `lcmovie/YYB-GO-Script-i`，其 README 提供：

```bash
ql repo "https://github.com/lcmovie/YYB-GO-Script-i.git" "^wx-script/.*\.(js|py)$" "" "" "main" "js py"
```

在青龙订阅管理也可填写同样的地址、分支 `main`、白名单 `^wx-script/.*\.(js|py)$` 和后缀 `js py`。拉库定时例如 `0 */6 * * *`（每 6 小时），不等于脚本执行定时。

拉库后，根据实际任务命令，把包含 `wx-script` 的目录追加到 YYB 的 `YYB_QINGLONG_REPO`，保留原有目录并重启 YYB。例如 `task lcmovie_YYB-GO-Script-i_main/wx-script/qqmusic.py` 对应 `lcmovie_YYB-GO-Script-i_main/wx-script`；是否带 `_main` 以实际目录为准。不要填写 GitHub 的 `/tree/main/...` 网页地址作为仓库地址。

路径不变时，已有账号任务直接运行更新后的文件。如果只使用 YYB 账号任务，首次接入或新增脚本后，检查并禁用订阅生成的原始全局任务，避免重复运行。第三方仓库的筛选规则与业务接口由其作者维护，这里的订阅示例不代表已验证全部脚本的运行效果。

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
