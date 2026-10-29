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

- **AutoAuth 不是 OAuth refresh token。** 官方 `TdiAutoLoginAsync`（`0x3ff80`）读取独立自动登录密钥，长度为零时在 `0x4020a` 分支进入“require manual login”并返回本地错误 `-14`；调用者 `0xed50a` 属于 TdiManager 的自动登录流程。项目目前通过登录票据做 ManualAuth 并缓存有限时长会话，没有这套 AutoAuth 恢复。下节进一步确认成功响应密钥的来源与持久化，但轮换、异常恢复和完整网络请求仍待验证，不能仅加一个同名请求当作已支持。
- **SmartHeartbeat 是 Mars 传输层组件。** 官方字符串的源码路径明确位于 `mars/stn/src/smart_heartbeat.cc`，涉及连接建立、断连和心跳间隔。项目握手完成后关闭长连接、通过短连接发 JSAPI；没有证据说明补发传输心跳可以延长 OAuth refresh token。
- **设备与身份需要分清。** 持久 AppDeviceId、票据自带 device_id、短连接设备字段及会话密钥不是同一个字段。固定设备标识需单独隔离验证，不使用官方账号的真实标识去覆盖生产其他账号。

自然续期验证仍使用真实扫码与到期时间，按实际部署记录版本变化。格式样例通过、会话重建成功与 token 后台续期成功是三个不同的结论。

## AutoAuth 密钥与请求的进一步对照

同一 DLL 同时包含 `ilinkcliproto` 生成消息与 `ilink` 的 `WXMessageLiteCPP` 消息，两者内存布局不同，不能跨类型套用偏移。继续解析后者的静态字段表，并核对实际读写，得到：

| 原生证据 | 已确认内容 |
| --- | --- |
| `0x1e3790` | 根据虚表第 12 项返回的字段表，按零起始索引访问对象；每项 40 字节，字段号、类型与对象偏移分别在表项 `+0`、`+5`、`+8` |
| `UnifyAuthResponse` 表 `0x9f01b0` | 索引 1 是字段 2，类型 message；工厂与 RTTI 对应 `ilink::AuthSectResp` |
| `AuthSectResp` 表 `0x9effc8` | 字段 3 / 4 均为 bytes，在该动态消息的对象偏移为 `0x30` / `0x38` |
| `0x7a0ad`、`0x7a41e`、`0x7a469` | 成功处理函数取上述认证段，读取字段 3，Base64 编码后调用 INI `set` 保存到 `kTdiKeyAutoauthKey` |
| `0x7a4c8`、`0x7a513` | 同样把字段 4 Base64 编码，保存到 `kTdiKeyAutoauthEncKey` |
| getter `0x3a640` / `0x3a800` | 读取这两个 INI 项并 Base64 解码；对应调用桩可核对到 `ini_file::get` 与 `base64_decode` 导入符号 |
| `0x7b56f`、`0x7b5f7` | 清理流程对这两项调用 INI `del`；不是把它们当作永久有效的固定密钥 |
| `AutoAuthRequest` 表 `0x9f04f0` | 字段 1 message、字段 2 uint32、字段 3 bytes、字段 4 bytes；内层 AppBase 表 `0x9efcf8` 只有设备字段 1 |
| `0x3e94e`、`0x3ef57`、`0x3ef74` | 自动登录构造分支读取已保存的 autoauth key，版本字段设为 1901，并把 key 写到请求字段 3 |
| `0x3efaf`–`0x3efee` | 请求字段 4 根据登录上下文存在标记条件设置；不能因为响应字段 4 是加密密钥，就假定请求字段 4 也直接填该密钥 |

可用仅依赖 `pefile` 的静态工具复核字段表，不执行 DLL：

```bash
python tools/inspect_native_auth_schema.py --binary /path/to/ilink2.dll
```

工具同样校验样本 SHA256，只输出静态字段号、类型、索引和偏移，不读取官方配置中的真实密钥。项目 `extractSession` 当前仅提取收发密钥、部分会话字段和身份，没有保存认证段字段 3 / 4 或发送 AutoAuth。这是已确认的功能差异，但目前没有证据说明补齐它会使已撤销的 OAuth refresh token 恢复，也没有完成自动恢复请求的端到端验证。

## v0.2.42 发布与现场结果

- 全量 Go 测试、`go vet ./...`、5 项 Release 说明测试和原生字段模拟通过；main / tag 原生、main / tag Docker 和 Magisk 五个工作流均成功。
- [v0.2.42 Release](https://github.com/525815266/YYB-Go-Enhanced/releases/tag/v0.2.42) 保留七种单二进制、七种完整包、Magisk 与 checksums 共 16 项资产。Windows amd64 正式文件完成 SHA256 校验和实际隔离启动，内嵌模板、版本接口及匿名协议请求 401 通过。
- 飞牛精确版本镜像部署成功，健康正常，21 个账号身份、挂载、端口、环境变量与启动参数均保留，已留私有回滚备份。
- 第一轮授权在 10:08:41 的请求为 HTTP 409，但升级前数据库备份里该账号已经是 `expired`，且凭据与票据均已清空。因此该次调用没有进入 v0.2.42 的新握手，不能说是字段修补导致上游拒绝；旧轮的具体业务拒绝阶段尚缺直接记录。此前 09:34 的会话重建成功并没有延续到凭据正常刷新窗口。
- 用户重新扫码后，第二轮新授权在 v0.2.42 的真实取码为 HTTP 200、非空 32 字符 code。新基准扫码时间为北京时间 10:10:28，凭据原到期为 12:10:27，后台正常刷新窗口从 12:06:27 开始。旧观察完整保留，新观察单独建档，不覆盖或混合两次授权的数据。

当前能够确认字段修正通过真实新授权取码；快速失效根因、后台自然续期与 30 天保活均未据此宣告解决。
