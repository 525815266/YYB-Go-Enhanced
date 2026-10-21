# 青龙修复脚本

## YYB 适配脚本

`scripts/` 统一收录原有业务脚本和 136 个经过 YYB 多账号适配的 Python 脚本。
已完成文件哈希、AST 结构和名称归一化去重，并覆盖普通取码、动态 AppID、手机号
授权 code/加密包等调用形式。青龙不再直接订阅原仓库，防止更新覆盖适配层；配置、
青龙任务统一使用 `task 525815266_YYB-Go-Enhanced/scripts/脚本名.py`，脚本由本仓库审核发布。

适配取码格式、通过语法检查不等于每个商户的真实登录与活动都已验证。脚本报错应区分 YYB 取码、商户登录、具体任务三个阶段，不能只凭其他脚本成功或“未配置代理”推断根因。

<a id="脚本可用性记录97-2026-10-19"></a>

## 脚本可用性记录（#97，2026-10-21）

下表记录已取得证据的范围，不是实时检测。**没有列出的脚本一律视为业务未验证**，不能因为已收录、定时已启用、进程退出码为 0 或 YYB 取码成功，就标成全部可用。账号失效、代理不可达也不直接等于脚本失效。

| 脚本 | 状态 | 验证日期与依据 | 已知范围与限制 |
| --- | --- | --- | --- |
| [中国联通](chinaUnicom.py) | 部分功能已验证 | 2026-10-12，维护者飞牛青龙 3 个 App Token 账号实测 | 登录、Token 续期、首页签到并复查通过；服务密码真实登录和其他活动未验证，不能直接使用 YYB 登录 |
| [寻星电玩网页积分](findstars_web_points.py) | 部分功能已验证 | 2026-10-16，维护者飞牛青龙实测 | 本人授权 HAR 导入 FS-Token、领取闲置 N1 卡槽奖励并复查积分通过；不支持 YYB 代替公众号网页授权 |
| [迪卡侬](迪卡侬_code版.py) | 用户反馈可用 | 2026-10-17，[#95 对照日志](https://github.com/525815266/YYB-Go-Enhanced/issues/95) | 反馈者账号签到成功，不代表维护者已验证全部任务或其他账号 |
| [京东 CK 采集](京东CK采集_code版.py) | 待验证恢复 | 2026-10-10，[#81](https://github.com/525815266/YYB-Go-Enhanced/issues/81) | 已修复漏读重定向 Cookie；若上游仅返回 skey，仍无法生成完整 pt_key/pt_pin，未确认真实 CK 获取恢复 |
| [爱隐形](爱隐形_code版.py) | 签到响应兼容已修复，完整业务待回测 | 2026-10-21，[#98](https://github.com/525815266/YYB-Go-Enhanced/pull/98) 解包线索、[#95](https://github.com/525815266/YYB-Go-Enhanced/issues/95) 会员登录反馈、维护者只读响应实测 | 兼容 `error_code=0` 与新格式 `code=1/success=true/failed=false`；维护者尚未验证会员签到成功 |
| [东呈](东呈_code版.py) | 登录待适配 | 2026-10-21，[#95](https://github.com/525815266/YYB-Go-Enhanced/issues/95) 新反馈及源码核对 | code 登录接口原为推断，尚无成功链路依据；已修正签到失败计入成功、HTTP 失败接收 token、跨账号继承会话及已配置 blackbox 未发送的问题，不代表真实登录已恢复 |

“定时已启用 / 运行中 / 已停用”只说明青龙任务状态，与上表的业务可用性分开看。此记录只对应本仓库脚本；同名的第三方脚本不继承验证结论。后续反馈请附准确文件名、版本、验证日期、成功或失败的业务步骤及脱敏日志，以便更新范围；不要公开 Token、Cookie、OpenID 或完整 HAR。目前控制台尚未展示这张状态表，也不会根据一条账号错误自动修改全局脚本状态。

<a id="爱隐形适配状态95-2026-10-17"></a>
<a id="爱隐形适配状态95--98-2026-10-19"></a>

## 爱隐形适配状态（#95 / #98，2026-10-21）

感谢 @greenwave1987 在 [PR #98](https://github.com/525815266/YYB-Go-Enhanced/pull/98) 提供小程序解包线索。`爱隐形_code版.py` 改用 `POST /sso/login/minalogin`，提交 `{"sourceChannel":"Mina","code":"微信登录code"}`，不再把 code 当作 `baselogin` 的参数。原 [UserId 脚本](https://github.com/3ixi/autoscripts/blob/bae528cb8d28ec751d86daea3bbd89a699b8ba2b/aiyx.py) 的 `baselogin` 路径不等于 code 换会员身份的流程。

维护者在飞牛青龙对 2 个自有 YYB 账号实际取码、调用 minalogin，均返回 HTTP 200、顶层 `open_id / union_id / user_id` 字段与空 `error_code`。测试账号未取得会员 `user_id`，因此没有继续提交签到，也没有保存空身份；**这证明已到达身份响应阶段，尚不代表会员登录和签到已恢复**。脚本兼容顶层与 `data` 包裹的身份响应，并先检查业务失败码，避免错误响应中夹带 user_id 时误存缓存。

签到查询缺少明确状态时停止；提交后再查询确认今日已签到，失败或未确认时不报告成功、不重复提交。HTTP/JSON 异常保留现有缓存，不输出原始登录响应。不自动注册、绑定手机号或猜测会员身份。

10 月 21 日跟进 #95：用户已取得会员 `user_id`，但旧脚本只接受签到接口的 `error_code=0`。维护者通过不带会员身份的只读请求确认，当前 `getsignlist` 返回另一种格式：`code=1`、`success=true`、`failed=false`，并有 `data.todaySignInfo`；旧判断会误拒绝这类响应。现已兼容两种明确状态，签到提交、复查和资产校验共用判断；字段缺失、相互矛盾或 HTTP 错误仍失败。失败日志只列有限状态字段，不回显原始响应、Token 或会员身份。任务有失败账号时发送汇总后返回非零退出码。

这次只读请求仅用于核对响应格式，**不证明任何会员已经签到**；维护者的测试账号仍未取得会员身份，完整签到需要反馈者回测。

东呈另行核对了公开的 [Token 型脚本](https://github.com/zjk2017/ArcadiaScriptPublic/blob/main/BackUp/东呈.py)：签到请求带有 `blackbox` 参数，现已补齐 `DCJD_BLACKBOX` 的发送。保留原脚本接受的 `0 / 121300002` 状态，其中 `121300002` 的具体业务含义尚未独立验证，显示为兼容状态，不据此断言「今日已签到」或新增积分。这份来源没有 code 登录流程，因此不能用于证明当前推断登录接口有效；请针对东呈提供具体失败步骤和脱敏状态继续适配。

更新本脚本即可，无需升级 YYB 服务（仍为 v0.2.33）。已有商户缓存保留。请先在官方小程序核对会员登录/手机号绑定，再用新版反馈会员 user_id 是否取得及签到复查结果；“没有 user_id”本身不能断言唯一原因是未注册。请移除真实 code、手机号、Cookie、Token 和 OpenID，不要把完整抓包直接发到公开 issue。#95 保持开放等待会员账号回测。

## 顺丰脚本线索（#92，2026-10-16）

已找到公开的 [SuperNaiBA/YYB-GO-Script 的 sfsy.py](https://github.com/SuperNaiBA/YYB-GO-Script/blob/c55efc23d219dd5b1b61dbd1699c185fa8203cb2/sfsy.py)：使用 `YYB_SERVER`，调用 `/wxapp/getCode`，以顺丰小程序 code 经过 UCMP `appOnLogin` 和活动页换取顺丰业务 Cookie，再执行签到等任务。它是第三方脚本，尚未收录或验证完整任务成功。

当前公开源码能读取本项目 `data.result.code` 返回结构。业务 Cookie 到期时可重新登录获取，不代表 Cookie 永不过期，也不等于所有活动接口都长期有效。源码取码超时为 30 秒，可能早于 YYB 默认整体调用预算；接入前还需核对协议鉴权头、同名环境变量分隔方式与业务接口版本。若已启用 `YYB_PROTOCOL_TOKEN`，脚本取码请求必须携带对应 Bearer token。

## 中国联通融合版（2026-10-12）

`chinaUnicom.py` 基于现有 Python v1.1.0，融合用户提供的中国联通 JS v1.10 的
RSA 服务密码登录，保留原活动与功能开关。使用 `chinaUnicomCookie`，多个账号按行或 `&` 分隔：

```text
联通Token#AppId
联通Token#AppId#手机号
手机号#服务密码
```

仅手机号会尝试本地缓存；已有 Token 优先使用缓存中的续期 Token。密码登录只在没有
Token 或在线会话明确被拒绝时尝试一次，服务端要求短信/验证码时需本人完成验证。
网络故障不会触发重复密码登录，也不会清空或覆盖已保存凭据。

依赖 `requests`、`pycryptodome`，原任务仍可使用 `task chinaUnicom.py`。建议先设置
`UNICOM_TEST_MODE=login` 验证登录；`sign` 仅执行首页签到并复查；`query` 仅查询，
这些模式不执行账号间分享浏览或发送通知。取消该变量后按脚本原开关运行日常任务。
缓存默认保存在脚本目录 `unicom_token_cache.json`，可用 `UNICOM_TOKEN_CACHE_PATH` 指定
固定路径；升级脚本时保留缓存文件。

本次飞牛青龙实测：现有 3 组 Token 登录成功，全部取得轮换 Token；首页签到逐账号
提交后复查均为已签到，每个账号返回 `+0.01元`。服务密码登录已通过 RSA、错误响应及
缓存回归测试，本地未配置服务密码账号，因此尚未验证真实密码登录成功。
另发现本地代理平台拒绝提取，原因为出口未加入白名单；现在识别该错误后本轮只请求
一次，明确提示并沿用原脚本的本地 IP 回退，不会给每个账号重复提取五次。

**YYB 接入范围：** 这些 App 活动需要联通 `token_online` / `ecs_token`。当前检查到的
联通脚本，包括公开 YYB 脚本目录中的 `lt.py`，仍然使用联通 App Token；没有找到
`wx.login code → 联通 App Token` 的业务兑换链路。`YYB_SERVER` 可继续给其他小程序
脚本使用，不能直接替代本脚本的 `chinaUnicomCookie`。若提供实际联通小程序登录请求
或对应脚本，可继续验证小程序授权能运行哪些活动，不会将小程序 code 冒充 App Token。

## 青龙脚本拉取工具

Issue #66 中出现的 `Tuple[Dict[str, str] ...`、`str) ->` 等任务名，不是脚本原本的名称。
青龙自动添加任务时会在整个 Python 文件中宽泛搜索 `name:`，函数类型标注中的
`account_name: str` 因此被误当成任务名。本仓库已为可订阅脚本补齐文件头部的
`# name:` / `// name:` 元数据，并提供 [`tools/yyb-scriptctl.sh`](../tools/yyb-scriptctl.sh)
把“下载脚本”和“创建任务”拆成明确的操作。

先在青龙容器内安装管理工具：

```bash
curl -fsSL https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/tools/yyb-scriptctl.sh \
  -o /ql/data/scripts/yyb-scriptctl.sh
chmod +x /ql/data/scripts/yyb-scriptctl.sh
```

拉取整个 `scripts/` 目录，**不创建任务**：

```bash
bash /ql/data/scripts/yyb-scriptctl.sh sync
```

只拉取一个脚本，**不创建任务**：

```bash
bash /ql/data/scripts/yyb-scriptctl.sh pull 麦富迪_code版.py
```

拉取一个脚本并创建或更新青龙任务：

```bash
bash /ql/data/scripts/yyb-scriptctl.sh install 麦富迪_code版.py \
  --cron "13 1 * * *" --name "麦富迪"
```

脚本头部已有 `# cron:` 时可以省略 `--cron`。没有 cron 元数据时，`install` 会完成下载
但拒绝创建任务，必须由用户明确传入执行时间；不会再给所有脚本套用同一个随机时间。
工具按完整任务命令查重，再次执行 `install` 会更新原任务的名称和 cron，不会重复创建。

默认目录对应青龙订阅名 `525815266_YYB-Go-Enhanced_main`。如果实际目录不同，可设置：

```bash
export YYB_SCRIPT_DIR=/ql/data/scripts/你的仓库目录/scripts
export YYB_TASK_PREFIX=你的仓库目录/scripts
```

不建议对整个仓库开启青龙的“自动添加任务”：部分脚本是公共模块或没有统一适用的执行时间。
旧任务不会被 Git 拉取动作自动重命名，可对需要的脚本执行一次 `install` 更新，或删除旧任务后重建。

本次同步的 3 个脚本：

- `心相印_code版.py`：采用最新心相印 AppID 和恒安 `wxappLogin` 登录/签到接口，保留 token 缓存与备用后端。
- `益禾堂_code版.py`：采用最新企迈明文换 token 和兑吧 `3fd0cbet` 动态签到 token 解析。
- `伊利QQ星_code.py`：新增伊利 QQ 星签到、积分任务和抽奖流程。

以上脚本共用 `yyb_compat.py`，从 `YYB_SERVER` 按行读取 `地址@账号ID或OpenID`，不会伪造微信手机号授权或加密字段。

## YYB 账号公共状态缓存

`yyb_account_guard.py`（Python）和 `yyb-account-guard.js`（Node）提供共享的
账号冷却缓存，默认文件为 `/ql/data/config/yyb_account_status.json`。已接入
`aima_sign.py`、`jtexpress_sign.py`、`byd_sign.py`、`DSTX.py`、`DTSH.py`、
`DDYX.py`、`DSMMHYSCQD.py`、`JTC.py`、`JYXEJYFHS.py`、`LDXQ.py`、`lz飞天.py`、
`NACO会员商城签到.py`、`NWDJG.py`、`NXDC.py`、`QC.py`、`SANF.py`、`THYC.py`、
`WRN.py`、`XFJ.py`、`jyk.py`、`weile_coin.py` 以及对应的多账号脚本：

- 明确返回“未授权手机号、尚未注册、未绑定会员”等业务错误时，记录为
  `unbound`/`unregistered`，默认冷却 24 小时；后续脚本会直接跳过该账号并继续下一个。
- 超时、502/503、风控、活动太火爆、登录过期和 token 失效只记为临时错误，
  默认冷却 10 分钟，不会永久禁用账号。
- `YYB_GUARD_BYPASS=1` 可供需要“先授权/自动注册”的脚本临时绕过过滤。
- `YYB_ACCOUNT_STATUS_FILE` 可自定义缓存路径；缓存采用临时文件原子替换，适合多任务并发读写。

青龙新增任务 `YYB账号状态检查.py`，建议每 12 小时运行一次。由于开启网页登录
认证后 `/accounts` 不能被青龙匿名读取，该任务只探测 YYB `/health` 并清理不在
`YYB_SERVER` 的缓存条目，不调用业务接口，也不会制造未消费的 `wx.login code`。
业务脚本遇到明确的未授权响应后负责写回缓存。

v0.2.19 起服务端也兼容旧脚本的 `/healthz`。同一服务地址只探测一次（失败也不重复），
并校验 YYB JSON 响应，防止反向代理返回 HTML 页面被误判为健康。服务可达不代表微信账号有效。

这个目录收录了对 `SuperNaiBA/YYB-GO-Script` 中已确认报错脚本的最小修复版，用于 YYB Go 多账号调用。

## 已修复

- `京东CK采集_code版.py`：区分完整 `pt_key/pt_pin` 与仅下发 `skey` 的响应；缺少票据时不生成或缓存 `JD_COOKIE`，HTTP 错误也不接受响应票据。失败日志只显示状态码和票据是否存在，不打印原始登录正文。完成京东绑定不保证下发 `pt_key`，`skey` 不能直接替代它。脚本保留历史 AppID `wx2f5d8f9715c59d10`；目前没有验证过改用 `wx91d27dbf599dff74` 后取得 `pt_key` 的新方案，不能承诺更换 AppID 即恢复。后续进展见 [#81](https://github.com/525815266/YYB-Go-Enhanced/issues/81)。这次更新只需重新拉取脚本。
- `DDYX.py`、`DSMMHYSCQD.py`、`DSTX.py`、`DTSH.py`、`JTC.py`、`JYXEJYFHS.py`、`LDXQ.py`、`NWDJG.py`、`NXDC.py`、`QC.py`、`SANF.py`、`THYC.py`、`XFJ.py`、`byd_sign.py`：修复 `YYB_SERVER` 配置提示代码的缩进错误。
- `WRN.py`：补充实际运行所需的 `sys` 导入。
- `MS.js`：兼容会员信息的新旧返回结构，缺少 `memberId` 时停止当前账号，避免连续异常。
- `DW.js`：多小程序执行前按 AppID 过滤公共缓存；明确返回未绑定小程序时记录账号状态，成功账号按 AppID 标记为可用。
- `ZMNLXQ.js`：战马能量星球按 AppID 接入公共缓存，手机号/小程序授权失败只在响应明确命中时冷却账号，并修复取码失败日志中的未定义变量。
- `oleCS.py`：OLE 超市接入公共缓存，保留原有 token 和门店缓存；网络取码失败不再误报为“未注册”。
- `jyk.py`：正确解析 `地址@账号标识` 格式，避免把账号 ID 当作 YYB 服务地址。
- `TCLXLC.js`：移除登录流程中对未定义 `parsedServer` 变量的引用。

## YYB 活动脚本

- `哈啰出行签到.py`：哈啰出行 YYB 多账号版。自动登录和签到，独立查询普通宝箱、
  上方“看视频免费骑车”、下方“看视频赚奖励金”以及普通任务奖励；按服务端返回的
  周期、阶段和冷却信息停止，不把三个视频入口混成同一个任务，也不伪造广告播放。
  对服务端已标记 `FINISHED` 的普通任务自动领取，并汇总开始余额、结束余额和净变化。
  接口与上限核对过程见 `哈啰出行说明.md`。

- `laichong_points.py`：莱充积分任务 YYB 版。默认使用 HAR 确认的 AppID
  `wxa68db1dabe823e7e`，自动签到，查询每日视频和每周分享进度，并领取服务端已经
  标记完成但尚未领取的奖励。按 HAR 的 `task/complete` 请求逐次提交视频和分享任务，
  每次核对服务端进度，到本期目标即停止；分享出现待领积分时调用 `task/claim`。
  完成后会二次刷新任务列表，避免接口延迟导致漏领。
  登录后如果账号尚未绑定莱充手机号，会通过 YYB `/wxapp/getPhoneNumber` 获取真实的一次性
  手机号授权包，并调用莱充手机号绑定接口；授权包缺失或服务端未确认绑定时会明确跳过该账号。
  每个账号输出积分前后差值。任务默认按 7 小时调度；
  `LAICHONG_TRY_VIDEO`、`LAICHONG_TRY_SHARE` 默认开启，`LAICHONG_TASK_DELAY` 默认 6 秒。
  此模式只复现小程序的任务接口请求，不会实际播放微信广告或分享卡片；以服务端进度和积分为准。

- `asdcb_auto_sign.py`：阿水大杯茶 YYB 版每日签到。每周二先按活动本期券模板 ID 查询个人券包，准确区分未使用/已使用/已过期；未领券时按小程序源码生成 `MD5` 签名和 AES-CBC/PKCS7 `data`，通过 `webapi_getuserencryptkey` 获取短时效加密密钥。若协议或业务返回错误，脚本会明确显示“未提交”，不会将顶层 `success` 或静态 `receiveStatus` 误报为领取结果。可用真实动态参数通过 `ASDCB_MEMBER_CLAIM_PAYLOAD` 覆盖。7.9 折券兑换通过 `ASDCB_ENABLE_79_COUPON=1` 显式开启，默认关闭。`--dry-run` 会在券包核验后直接跳过签到、领券和兑换，并在启动行明确标记查询模式。

  ```bash
  python3 asdcb_auto_sign.py --dry-run
  # 青龙环境变量：ASDCB_ENABLE_79_COUPON=1
  # 可选：ASDCB_MEMBER_CLAIM_PAYLOAD={"activityId":"...","timestamp":"...","signature":"...","data":"..."}
  ```

- `mlgogo_sign.py`：马历小程序积分商城每日签到。使用 `wx86d2d7c2d832b4ce`
  动态获取 code，按小程序内置 RSA 签名生成每次请求的 `_s`，支持多账号、签到
  状态查询、token 持久化和失效自动重登。

- `汇翼云会员日抽奖.py`：适配 `wxab79cb37a805c1ca`（響Livehouse）会员日转盘。
  通过 `YYB_SERVER` 获取小程序登录 code，校验汇翼云账号后查询活动开放状态和
  剩余次数，最多按服务端次数循环抽奖并汇总奖品。汇翼云自己的 JWT 并非
  `wx.login` 返回值，首次需通过 `HYY_ACCESS_TOKENS` 按 `账号ID=JWT` 建立映射；
  脚本随后把 `external_userid` 原子缓存到
  `/ql/data/config/hyy_turntable_accounts.json`，后续无需反复提供 JWT。也可直接用
  `HYY_EXTERNAL_USERIDS` 按账号配置已有的 `external_userid`。多账号模式不会共用
  单账号凭据，避免串号；`HYY_DRAW=0` 可只查询不抽奖。

- `毛豆充.py`：毛豆充 YYB 版，参考菠萝充电脚本的任务编排，但独立使用 HAR
  确认的 `hichar.user.wxapp` 业务接口。支持动态登录、会员积分查询、每日签到
  和视频任务（默认最多 5 次，按服务端 `nowTimes/limitTimes` 实时限流），并按
  HAR 确认的 `/api/user/welfare/draw` 自动执行按账户实际积分抽完本轮所有次数。
  每次抽奖前重新读取 `userWelfarePoints`，只要积分达到 1000 就继续。日志会记录开始积分、任务后积分、结束积分、任务积分收益、
  抽奖消耗和本轮净积分收益；相同奖品会合并计数，`毛豆N个` 会按实际数量累加。连续领取奖励默认随机等待 15–30 秒，
  可通过 `MAODOUCHONG_REWARD_DELAY_MIN/MAX` 调整；设置 `MAODOUCHONG_LOTTERY=0` 可关闭抽奖。
- `aima_sign.py`：通过 `YYB_SERVER` 动态登录爱玛会员小程序，自动发现当前有效签到活动；不信任汇总状态字段，仅当天记录存在时跳过，否则提交一次并以当天记录校验；自动领取已达成的连续签到积分奖励（可用 `AIMA_CLAIM_SIGN_REWARDS=0` 关闭），显示 YYB 备注、会员等级、当前/累计积分、成长值、绑定车辆和优惠券数量。
- `weile_coin.py`：通过 `YYB_SERVER` 动态获取微信小游戏 code，支持多账号查询微乐每日任务，并领取 HAR 已验证的分享福利金币和订阅更新金币。默认每次运行最多领取 1 次分享福利；设置 `WEILE_DRY_RUN=1` 可只查询不领取。
- `qq音乐_code版.py`：通过 `YYB_SERVER` 动态获取 QQ 音乐小程序 code，支持多账号执行，并从 YYB 账号资料中读取备注或昵称用于日志和通知显示；未配置 `YYB_SERVER` 时保留旧版多端口兼容模式。
- `小牛电动.py`、`东风奕派签到.py`、`叮咚买菜_code版.py`、`pp停车任务_cod.py`：支持 `YYB_SERVER` 多账号动态 code，并在日志和通知中优先显示 YYB 备注或昵称；未配置时继续兼容各脚本原有 code 服务地址。
- `商联道.py`：支持 YYB 多账号动态 code，保留签到、广告、文章、互动、全勤奖励和金豆查询流程，并按 YYB 账号隔离 token 缓存。
- `太平洋碳普惠.py`：支持 YYB 多账号动态 code，保留 SM2 登录、签到、积分任务、视频和积分汇总流程，并按 YYB 账号隔离 token 缓存。
- `NACO会员商城签到.py`：支持 YYB 多账号动态 code，保留有赞会员登录、签到、等级和积分查询，并按 YYB 账号隔离 token 缓存。
- `东风奕派签到.py` 的多账号手机号：设置 `EP_PHONES`，每行使用 `账号 ID 或 OpenID#手机号`（也兼容 `账号=手机号`），例如 `1#13800000001`；没有匹配项时回退到旧变量 `EP_PHONE`。账号标识按 `YYB_SERVER` 中的 ID/OpenID 匹配，也可用该账号在本次运行中的序号匹配。
- `无忧计划.py`：收录独立账号密码版任务脚本，继续使用 `WY_ACCOUNT=账号#密码#device_id#备注`，不依赖 YYB 微信账号。

## 使用

将需要的文件覆盖到青龙订阅目录中对应的脚本，然后先做语法检查：

```bash
python3 -m py_compile /ql/data/scripts/SuperNaiBA_YYB-GO-Script/脚本名.py
node --check /ql/data/scripts/SuperNaiBA_YYB-GO-Script/脚本名.js
```

## 本批 YYB 适配脚本

以下脚本已统一改为读取 `YYB_SERVER`（每行一个 `地址@账号ID或OpenID`），通过 YYB 的 `/wxapp/getCode` 获取动态 code：

```text
lz飞天.py
格力高club.py
爱裹旧衣服回收_co.py
白鲸鱼旧衣服回收_c.py
察理王子_code版.py
回收猿旧衣服回收_c.py
牛牛免费短剧.py
印象星.py
```

YYB 开启鉴权时设置可选环境变量 `YYB_API_KEY`。脚本不再使用硬编码的本地 `127.0.0.1:8088` 或旧 `/login` code 服务。

重新执行上游订阅可能会覆盖这些修复，建议在订阅更新后重新检查。脚本仍受原项目授权和条款约束。
# 寻星电玩网页积分

`findstars_web_points.py` 调用寻星电玩网页的活动任务接口，按官网的任务进度、总领取上限判断是否可领取，并复查领取后的积分和任务状态。日常任务命令仍为 `task findstars_web_points.py`。

推荐从本人已完成网页登录授权的 HAR 导入登录态，无需手动找令牌：

```bash
python3 findstars_web_points.py --import-har /path/to/authorized.har --label 网页账号
```

导入只验证用户身份、保存令牌，不提交领取。缓存默认位于 `/ql/data/config/findstars_web_tokens.json`，容器重启后保留；非青龙环境默认使用用户目录下的 `.config/yyb-go/`，可用 `FINDSTARS_TOKEN_CACHE` 指定路径。同备注的导入缓存优先于旧变量；已有缓存绑定其他用户时拒绝覆盖。沿用旧环境变量时，`--label` 填原变量的备注，例如 `网页账号`，避免同时保留失效的旧配置。

也可从本人浏览器请求中复制 `FS-Token`，在青龙创建环境变量：

```text
FINDSTARS_TOKENS=备注#FS-Token
```

多个账号按行或 `&` 分隔，也兼容 `FINDSTARS_TOKEN` / `FINDSTARS_FS_TOKEN`。`FINDSTARS_DRY_RUN=1` 只查询、不领取。领取超时不自动重放；登录态过期、领取失败或结果待核对时退出码为 1，不再把失败当作正常执行。

该令牌属于寻星网页登录态，不是 YYB 的小程序 OpenID。2026-10-16 实测：公众号 H5 OAuth 授权成功后，导入 HAR 可以恢复积分任务；当前 YYB 公众号调用返回 `invalid api_name`，尚无已验证的自动换取或续期方案。令牌再次失效时仍需本人重新授权并导入。HAR 和缓存含账号凭据，请勿提交到仓库或公开分享。

# 百花约跑团线上投票

`baihua_vote.py` 关联 `YYB_SERVER` 中的账号与微信公众号网页活动登录态，自动检查
JWT 有效期及活动验证码，再按账号向指定选手提交一次投票。脚本尊重活动每日额度，
不会绕过验证码或重复刷同一账号。

该活动使用公众号 `snsapi_base` OAuth，不是小程序 `wx.login`。当前 YYB Go 的 iLink
会话无法直接生成公众号 OAuth code；首次需由账号本人打开活动链接授权，再从本人抓包
导入 `app_access_token`：

```bash
python3 baihua_vote.py --import-har /path/to/authorized.har --ref 1
```

也可在青龙设置 `BAIHUA_VOTE_TOKENS`，每行使用 `YYB账号ID#活动JWT`。JWT 默认持久化
到 `/ql/data/config/baihua_vote_tokens.json`，有效期内后续运行无需再次授权。

脚本内置按账号协议探测，可动态发现活动当前 OAuth URL，再调用 YYB 的公众号兼容入口：

```bash
python3 baihua_vote.py --probe-yyb-oauth --ref 2
```

截至当前 YYB iLink 服务端对此返回 `invalid api_name`，说明应用宝小程序会话不具备完整
微信客户端的 `/cgi-bin/micromsg-bin/mp-geta8key` 能力。探测失败不会提交投票，也不会
覆盖已有 token；若后续协议端补齐并返回 OAuth code，脚本会自动走活动回调并缓存 JWT。
