# 官方 ILink 登录字段对照（2026-10-29）

继续从原生代码核对协议，确认一处实际消息层级错误：v0.2.41 及此前实现把版本值 `1901` 放在内层 `AppBaseRequest`，而官方写入并序列化的是外层 `ManualAuthRequest` 第 2 字段。v0.2.42 修正这处布局，不修改设备标识生成、OAuth 续期或代理策略。尚无证据证明该差异导致近期批量失效。

## 样本与定位

本机应用宝主程序为 5.10.7600.6722，实际安装的协议运行库为 `WmpfRuntime/5.10.7300.414/runtime/ilink2.dll`。两者版本不同，以下 RVA 仅适用于该 DLL，不能直接套用其他版本。

SHA256：`8b69772a79e5892ab48aff13a096a1a806219061a17395d14f311305d4a74d7a`。

| 位置 | 已核对的行为 |
| --- | --- |
| ctor `0x145e10` / vtable `0x7e96b0` | RTTI 为 `ilinkcliproto::AppBaseRequest`，只有设备字符串存储与其存在标记 |
| ctor `0x146470` / vtable `0x7e9718` | RTTI 为 `ilinkcliproto::ManualAuthRequest`，包含内层对象、版本数值与其他登录字段 |
| `0x10318c`、`0x103194` | 获取持久 AppDeviceId 并写入内层设备字符串，设置存在标记 |
| `0x1032c9`、`0x1032d4` | 将内层对象放入 ManualAuthRequest，向外层对象偏移 `0x18` 写入 `0x76d`，即 1901 |
| AppBase 序列化 `0x168280` | 仅把设备字符串传给第 1 字段的 bytes writer，没有内层第 2 字段 |
| ManualAuth 序列化 `0x169ae6`–`0x169af1` | 从外层偏移 `0x18` 读取数值，以字段号 2 调用整数 writer `0x1eefc0` |
| 整数 writer `0x1eefc0` | 字段号左移 3 后写 varint tag，再写数值，确认 wire type 为 0 |

该登录构造分支明确设置外层字段 1、2、3、4、7，以及上下文提供时的字段 8；没有设置第 6 字段。项目旧实现总是发送空第 6 字段，v0.2.42 也移除这一无依据的空值。

## 修正前后

| 内容 | 官方已确认布局 | v0.2.41 及此前 | v0.2.42 |
| --- | --- | --- | --- |
| AppBase 字段 1 | 设备标识 bytes | 有 | 保留 |
| AppBase 字段 2 | 此消息中不存在 | 错放版本 1901 | 移除 |
| ManualAuth 字段 2 | uint32 值 1901 | 缺失 | 补齐 |
| ManualAuth 字段 3 | 包含登录票据的序列化数据 | 有 | 保留 |
| ManualAuth 字段 4 | 本分支为 4 | 4 | 保留 |
| ManualAuth 字段 6 | 本分支未设置 | 空 bytes | 移除 |
| ManualAuth 字段 7、8 | 由上下文提供 | 0、6 | 本次保留，不据此声称覆盖全部客户端分支 |

这里确认的是版本字段的层级与类型。设备标识仍存在另一个独立差异：官方持久化默认 16 个十六进制字符，项目仍每次握手生成 32 个原始随机字节；没有把该候选与此次格式修补混在一起。

## 可复核的离线模拟

工具：[`tools/inspect_native_manualauth.py`](../tools/inspect_native_manualauth.py)。需要 Python、`pefile` 和 `unicorn`，从本机提供已核对哈希的 DLL：

```bash
python tools/inspect_native_manualauth.py --binary /path/to/ilink2.dll
```

工具将 PE 映像放入 Unicorn，使用完全虚构的设备与票据，执行官方 AppBase 和 ManualAuth 字段序列化代码。已确认的 protobuf bytes / uint32 / 子消息 writer 用等价原语替代并记录字段。不会把 DLL 加载进 Windows，不读取真实数据库、进程内存或网络；样本哈希不符直接停止。

因此它验证的是官方代码选择的字段、层级、存在条件与固定样例字节，不是原生程序全部端到端行为，也不是上游接受或续期证明。设备与上下文结构由已核对的构造函数布局设置，没有把真实账号材料嵌入测试。

虚构设备 `0123456789abcdef`、票据 `synthetic-ticket`、版本 1901、登录类型 4、上下文值 0 / 6 的结果：

```text
0a120a103031323334353637383961626364656610ed0e1a120a1073796e7468657469632d7469636b6574200438004006
```

Go 回归 `TestManualAuthRequestMatchesOfficialFieldLayout` 使用该固定样例：旧实现失败，修正版本层级及空字段后应通过。

## 其他仍在核对的差异

- **AutoAuth 不是 OAuth refresh token。** 官方 `TdiAutoLoginAsync`（`0x3ff80`）读取独立自动登录密钥，长度为零时在 `0x4020a` 分支进入“require manual login”并返回本地错误 `-14`；调用者 `0xed50a` 属于 TdiManager 的自动登录流程。项目目前通过登录票据做 ManualAuth 并缓存有限时长会话，没有这套 AutoAuth 恢复。成功响应中密钥如何保存、轮换及何时实际选用仍待完整复核，不能仅加一个同名请求当作已支持。
- **SmartHeartbeat 是 Mars 传输层组件。** 官方字符串的源码路径明确位于 `mars/stn/src/smart_heartbeat.cc`，涉及连接建立、断连和心跳间隔。项目握手完成后关闭长连接、通过短连接发 JSAPI；没有证据说明补发传输心跳可以延长 OAuth refresh token。
- **设备与身份需要分清。** 持久 AppDeviceId、票据自带 device_id、短连接设备字段及会话密钥不是同一个字段。固定设备标识需单独隔离验证，不使用官方账号的真实标识去覆盖生产其他账号。

自然续期验证仍使用真实扫码与到期时间，按实际部署记录版本变化。格式样例通过、会话重建成功与 token 后台续期成功是三个不同的结论。
