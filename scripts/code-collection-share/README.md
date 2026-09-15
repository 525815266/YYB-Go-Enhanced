# code-collection-share YYB 适配版

本目录基于 `L0NE-6/code-collection-share` 的提交
`907bc3b47ba64f9a113ad74536a7c5bb7998b1da` 整理。原始 136 个 Python
脚本经过文件哈希、AST 结构和名称归一化三轮检查，没有发现可安全删除的重复项，
因此去重结果为 `136 -> 136`。

所有脚本统一优先读取：

```text
YYB_SERVER=yyb-go:8000@1
yyb-go:8000@3
```

每一行对应一个 YYB 账号 ID 或 OpenID，并通过 `POST /wxapp/getCode` 获取
该小程序的动态 `wx.login` code。动态 AppID 脚本会把当前业务 AppID 原样传给
YYB，不再固定使用默认 AppID。未设置 `YYB_SERVER` 时，脚本仍保留原
`CODE_SERVER` 调用方式作为兼容回退。

YYB 启用了协议接口鉴权时，设置：

```text
YYB_API_KEY=协议接口令牌
```

脚本会发送 `Authorization: Bearer <YYB_API_KEY>`。

## 能力限制

- 需要手机号授权的脚本会调用 `POST /wxapp/getPhoneNumber`。只有 YYB 和当前微信
  账号真实返回所需字段时才能继续，不会伪造手机号授权数据。
- `/wx/getuserinfo` 返回账号资料，不等于小程序运行时产生的
  `encryptedData`、`iv`、`signature`。依赖这些加密字段的业务仍可能要求真机授权。
- YYB 取码成功只证明微信协议调用成功；未注册会员、未授权手机号、活动风控和
  上游接口变化仍由各业务平台决定。

青龙任务路径应使用：

```text
task 525815266_YYB-Go-Enhanced/scripts/code-collection-share/脚本名.py
```

本目录已脱离上游订阅管理，更新由本仓库审核、适配后发布，避免订阅覆盖本地修复。
