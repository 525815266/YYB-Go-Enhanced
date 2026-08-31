# 青龙修复脚本

这个目录收录了对 `SuperNaiBA/YYB-GO-Script` 中已确认报错脚本的最小修复版，用于 YYB Go 多账号调用。

## 已修复

- `DDYX.py`、`DSMMHYSCQD.py`、`DSTX.py`、`DTSH.py`、`JTC.py`、`JYXEJYFHS.py`、`LDXQ.py`、`NWDJG.py`、`NXDC.py`、`QC.py`、`SANF.py`、`THYC.py`、`XFJ.py`、`byd_sign.py`：修复 `YYB_SERVER` 配置提示代码的缩进错误。
- `WRN.py`：补充实际运行所需的 `sys` 导入。
- `MS.js`：兼容会员信息的新旧返回结构，缺少 `memberId` 时停止当前账号，避免连续异常。
- `jyk.py`：正确解析 `地址@账号标识` 格式，避免把账号 ID 当作 YYB 服务地址。
- `TCLXLC.js`：移除登录流程中对未定义 `parsedServer` 变量的引用。

## YYB 活动脚本

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
