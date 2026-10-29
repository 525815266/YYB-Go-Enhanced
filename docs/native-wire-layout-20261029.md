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

该网络内部登录分支明确设置外层字段 1、2、3、4、7，以及上下文提供时的字段 8；没有设置第 6 字段。v0.2.42 按此分支移除空第 6 字段。进一步定位的 TDI ThirdApp 分支却显式设置空第 6 字段，见下方补充；不能把“官方所有分支都不发送第 6 字段”作为结论，也没有证据认定空值导致快速失效。

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

这里确认的是版本字段的层级与类型。设备标识仍存在持久化方面的独立差异，但长度必须按分支区分：网络内部 `AppDeviceId` 默认是 16 个十六进制字符，TDI 应用登录的 `kTdiKeyDeviceId` 则是 32 个原始随机字节。不能据前一个分支认定项目的 32 字节长度错误；没有把设备候选与此次版本格式修补混在一起。

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

### AutoAuth 的网络会话上下文

继续从请求构造追到网络层，确认 AutoAuth 不只是 ManualAuth 换一个 URL：

| 位置 | 已确认内容 |
| --- | --- |
| `0x78210` 构造；`0x7826c`、`0x78291` | RTTI 对应 `pb_export::IlinkRequestInfo`，字段 1 命令号为 `0xc7e`（3198），字段 2 是 `/ilink/ilinkapp/sys/ilinkapp_autoauth` |
| `0x78327`–`0x78336`；字段表 `0x9ec810` | 明确设置字段 4 为 0、字段 6 为 1、字段 7 为 2；后两者是 enum。这里仅记录数值，不猜枚举名称或直接把它们等同于自定义包头字段 |
| `0x7383c`、`0x73849`、`0x7387c`、`0x7398b` | 创建上述请求描述，序列化 AutoAuth 消息并放入描述字段 3，再交给 Tdi 网络发送 |
| `0x3e95e`、`0x3ecad`、`0x3ee18`、`0x3ee4a` | 读取 AutoAuth enc key，构造 RTTI 为 `pb_export::IlinkAppCommSessionInfo` 的对象，把该 key 写入其字段 3；该消息不同于线上 `AutoAuthRequest` |
| `0x3ee4f`–`0x3eebd` | 网络会话信息字段 4 为保存的身份值，字段 5 为保存的用户名；序列化后传给 `UpdateSession`（`0x7eb90`） |
| `0x7ec64`–`0x7ec82` | `UpdateSession` 将序列化的会话信息和会话类型交给网络对象虚表方法；最终封包、加密模式及重定向仍需继续复核 |
| 登录上下文表 `0x9eb318`；`0x3efaf`–`0x3efee` | 条件读取的是 `pb_export::IlinkLoginRequest` 字段 8（bytes，偏移 `0x50`），复制到 AutoAuth 请求字段 4；并非直接复制 enc key |
| ManualAuth / AutoAuth 响应表 `0x9f0430` / `0x9f0560` | 前者的统一认证响应在字段 3，后者在字段 2。恢复响应不能直接交给当前按 ManualAuth 字段 3 解包的 `extractSession` |

静态工具现包含上述请求描述、网络会话信息及两种认证响应的字段表。项目目前既没有保存 AutoAuth 密钥，也没有对应网络会话更新与恢复响应解析；这些差异可作为后续隔离实现的依据，但本次没有给生产账号添加未经端到端验证的自动恢复。

## v0.2.42 发布与现场结果

- 全量 Go 测试、`go vet ./...`、5 项 Release 说明测试和原生字段模拟通过；main / tag 原生、main / tag Docker 和 Magisk 五个工作流均成功。
- [v0.2.42 Release](https://github.com/525815266/YYB-Go-Enhanced/releases/tag/v0.2.42) 保留七种单二进制、七种完整包、Magisk 与 checksums 共 16 项资产。Windows amd64 正式文件完成 SHA256 校验和实际隔离启动，内嵌模板、版本接口及匿名协议请求 401 通过。
- 飞牛精确版本镜像部署成功，健康正常，21 个账号身份、挂载、端口、环境变量与启动参数均保留，已留私有回滚备份。
- 第一轮授权在 10:08:41 的请求为 HTTP 409，但升级前数据库备份里该账号已经是 `expired`，且凭据与票据均已清空。因此该次调用没有进入 v0.2.42 的新握手，不能说是字段修补导致上游拒绝；旧轮的具体业务拒绝阶段尚缺直接记录。此前 09:34 的会话重建成功并没有延续到凭据正常刷新窗口。
- 用户重新扫码后，第二轮新授权在 v0.2.42 的真实取码为 HTTP 200、非空 32 字符 code。新基准扫码时间为北京时间 10:10:28，凭据原到期为 12:10:27，后台正常刷新窗口从 12:06:27 开始。旧观察完整保留，新观察单独建档，不覆盖或混合两次授权的数据。

当前能够确认字段修正通过真实新授权取码；快速失效根因、后台自然续期与 30 天保活均未据此宣告解决。

## v0.2.43 诊断修补与验证

#105 最新首次 409 仅显示 `check_login / -109 / 42007`。继续检查发现 `RefreshCredentials` 用 `%v` 拼接主刷新错误，只用 `%w` 保留 check-login，因此主刷新虽实际执行，诊断却无法识别其类型。v0.2.43 改为保留两个实际原因；同时修正失效判断遍历完整错误链，避免第一个含义不明的 `-109` 遮住后面的明确拒绝。

- 最小回归先在旧实现确认错误链丢失，以及直接改为双原因后首个模糊错误会遮住明确拒绝；两处修补后通过。HTTP 409 回归同时验证四阶段诊断、脱敏、凭据清理与重复调用不再请求上游；网络超时和仅 access token 到期仍可重试。
- Windows 全量 Go 测试、静态检查、5 项 Release 说明测试通过；原生 main / tag、Docker main / tag、Magisk 五个工作流均成功。[v0.2.43 Release](https://github.com/525815266/YYB-Go-Enhanced/releases/tag/v0.2.43) 包含 16 项资产。
- 正式 Windows amd64 发布文件完成 SHA256 校验并实际隔离启动，内嵌模板、版本、协议令牌输入框和匿名拒绝通过。
- 飞牛精确版本镜像先隔离验证，再保留配置部署；健康正常，21 个账号身份一致并保留回滚备份。10:35 的只读观察显示 ID1 仍 alive，扫码 10:10:28、原到期 12:10:27、凭据摘要未变，观察单独记录部署版本变化。没有提前请求 OAuth 刷新。
- 已回复 #105 / #109，保持开放。此次诊断修补不能恢复已被上游拒绝的 token，也不等于快速失效根因已解决。

## 2026-10-29 隔离 AutoAuth 验证

以下均为研究验证，没有给 v0.2.43 增加生产 AutoAuth。用户授权使用现有测试账号，额外握手已记录为观察干预；不写生产凭据、不改扫码时间、不提前刷新 OAuth。

### 两套设备标识与 TDI ThirdApp 分支

| 原生位置 | 可复核的实际行为 |
| --- | --- |
| `0x1783d0` / `0x178595` | 网络内部读取 `AppDeviceId`，缺省生成 8 随机字节，转换为 16 个十六进制字符并持久化 |
| getter `0x3a490` | TDI 读取 `kTdiKeyDeviceId`，经 Base64 解码取得原始设备 bytes |
| `0xf0dc6`、`0xf0de3`、`0xf0e14`、`0xf0e57` | TDI 初始化在缺值时生成 32 随机字节，Base64 编码后保存 |
| helper `0x68350`；调用者 `0x6ef01` | TDI ManualAuth 的 AppBase 字段 1 使用上述 TDI 设备值 |
| `0x3ef16` | TDI AutoAuth 使用同一 getter；设备值需要与原授权对应，不能把网络内部默认值直接替换进来 |
| `0x6ef0a`、`0x6ef31`、`0x6ef9a`、`0x6efbd` | TDI ThirdApp 手动登录设置外层版本 1901、ThirdApp 数据、登录类型 4，并显式设置空第 6 字段；版本层级与前述网络分支一致 |
| `0xfb380`、`0xfb577`、`0x1099dd`、`0x109a2d` | 网络实例另存创建参数字符串，经 PackRequest 进入请求头设备字段；其完整外部来源还需核对，不能先验认定等于 AppBase 设备或票据设备 |

当前已确认的设备差异是项目每次握手重新生成应用设备值，官方 TDI 持久保存。16 字符、32 字节和票据设备的几次短时取码成功，只能说明这些隔离请求被接受，不能证明更换设备格式可以解决长期失效。

### 加密与包头

官方 Hybrid 加密会用保存的 enc key（最多取前 24 字节）包装同一个 32 字节 CEK，结果进入 Hybrid 请求字段 4；它不是直接放到 AutoAuth 明文字段 4。`0x11ba6c` 调用 AES-GCM 的输入为 enc key、第一阶段 32 字节哈希 AAD 和 CEK；`0x11bf7f`–`0x11bfd6` 把结果赋给消息字段 4。HKDF 盐 `security hdkf expand` 的原生长度为 20 字节，与项目常量一致。

`PackRequest` 中应用 UIN、设备与用户名分别进入 wpkg 字段 22、24、27。`Req2Buf` 的字段 2 来自独立网络账号存储，但完整会话更新调用者存在把应用 UIN 写入该存储的路径，不能据“独立存储”推断它必须为 0，详见下方调用者复核。更下层还有账号类型分支：应用账号分支 `0x1a2d7c`–`0x1a2e01` 写入字段 18 为 0，非应用账号分支 `0x1a3897`–`0x1a38f2` 写入 1；随后应用分支还可能根据网络会话包装外层数据。因此仅修改这个标志，并不构成完整的官方网络实现。

新增离线工具：

```bash
python tools/inspect_native_autoauth.py --binary /path/to/ilink2.dll
```

工具校验同一 DLL 哈希，用虚构数据执行 enc key 截断、可选 CEK 包装、字段 4 赋值及两种 Req2Buf 字段选择分支。AES 原语和字段写入使用等价替代，原生代码选择输入与分支。空 / 16 / 24 / 32 字节 key 的四组检查、三组包头检查通过；这仍不是完整原生加密执行或服务器接受证明。

### 实测结果与边界

| 北京时间 | 隔离操作 | 结果 |
| --- | --- | --- |
| 11:18、11:24 | 原有 32 字节应用设备，分别检查两种网络账号头候选 | AutoAuth 命令 3198 返回 wpkg 字段 23 `-20014`，无加密响应体 |
| 11:27–11:32 | 新建 16 字符应用设备的 ManualAuth，再用其新密钥 AutoAuth | ManualAuth 真实取码成功，code 长度 32；AutoAuth 同为字段 23 `-20014` |
| 11:41–11:42 | 应用设备与票据 / 请求头设备一致的隔离候选 | ManualAuth 取码成功，AutoAuth 仍为字段 23 `-20014` |
| 11:51–11:52 | 新建 32 字节设备的隔离会话，仅切换 AutoAuth 的字段 18 为官方应用分支值 0 | 返回 wpkg 字段 4 `-20011`、字段 23 为 0，仍无加密响应体；不是成功响应 |
| 11:53 | 使用生产服务真实取码，核对实验后的状态 | HTTP 200、code 长度 32；生产扫码、OAuth token 摘要与原到期时间不变 |

上述两种负数码的具体业务含义尚未确认，不把 `-20014` 猜成 OAuth 撤销或解密失败，也不因字段 23 为 0 忽略字段 4 的拒绝。实验保存原始响应于私有文件，公开记录只保留时间、阶段、固定错误码和长度；未使用密钥重放、多节点盲试或将实验会话覆盖到生产。

隔离客户端位于 `internal/protocol/native_autoauth_audit_test.go`，仅以 `protocolaudit` build tag 编入，普通构建与测试不包含。执行还需要显式指定只读数据库、账号 1、手动 / 自动阶段和独立私有状态文件；默认跳过。它用于维护者已授权的实验，不是可对外使用的 AutoAuth 支持。下一步需要核对独立网络账号初始化、外层包与成功响应验签，现阶段不应仅保存 AutoKey / EncKey 就宣布会话恢复完成。

### 两层加密字段的进一步分离

继续执行原生包头分支，确认不能仅凭字段 25 是 17 就判定完整传输格式已经一致：

| 位置 | 字段或行为 |
| --- | --- |
| `0x1a2cba`–`0x1a2ccb` | 对象 `+0x118` 写入 wpkg 字段 25，来自应用请求的加密选择 |
| `0x1a2d9b`–`0x1a2dac` | 应用账号分支把对象 `+0x178` 写入 wpkg 字段 7；不同于字段 25 |
| `0x1a336e`–`0x1a3388` | 同一 `+0x178` 决定网络层请求体分支：0 复制、16 AES-GCM、17 Hybrid ECDH |
| `0x1a343d`、`0x1a3592`、`0x1a3686` | 分别进入 Hybrid、AES-GCM、复制分支；后续输入来自网络账号上下文 |

`inspect_native_autoauth.py` 现用虚构输入分别验证字段 25 固定为 17、字段 7 取 0 / 16 / 17，以及上述三个原生跳转分支。此前只改字段 18 的候选没有验证网络账号、字段 7 和实际请求体完整一致；该负结果不能证明 AutoAuth 本身不可行。当前仍未确定应该在此环境选择哪一网络账号初始化路径，不能猜一个加密枚举后接入生产。

同一隔离测试文件还提供显式私有会话输入的 `TestNativeCachedSessionCodeAudit`：不创建握手、不调用 OAuth、不删除生产会话，按控制应用 / 京东应用 / 控制应用交替取码。2026-10-29 12:18 的空 code 复现及 v0.2.44 修补边界见[第三轮记录](native-keepalive-audit-20261028.md#第三轮授权与空-code-复现2026-10-29-1218)。

### 官方网络会话更新的独立验证

继续把 TDI 的虚调用解析到真实实现，避免仅根据请求消息里的密钥推断完整网络登录：

| 位置 | 已确认行为 |
| --- | --- |
| `0x7ec82`；`IlinkNetworkImpl` 主虚表 `0x7e5a18` 第 9 项 | TDI `UpdateSession` 调入 `0x100c40`，原生日志名称为 `UpdateAppSession` |
| `0x100d5b`–`0x100d62` | 用网络实例 `+0xd8` 的独立应用会话对象调用 `0x170a00` 解析会话数据和类型 |
| `0x3eb70`–`0x3eb9e`、`0x3eebd` | AutoAuth 读取持久配置 `kTdiKeyIlinkAppSessionType`，把实际类型一起交给网络层，不能省略类型或任意猜值 |
| `0x100f62`、`0x173220` | 应用会话更新写入 `NetCoreBridgeAccount` 的应用区域，和网络账号的密钥 / UIN 分开 |
| 构造 `0x171d03`、getter `0x172390` | 网络桥账号缺省应用账号标记为 true；完整线上分支还取决于后续初始化，不能只靠构造缺省值就改所有请求 |

新增工具执行原生 `0x1732f9`–`0x1733dd` 的实际复制指令，只替代标准字符串赋值原语；五组虚构应用字符串、两组数值和存在标记均进入应用区域，网络密钥区与网络 UIN 的哨兵数据保持不变：

```bash
python tools/inspect_native_session_update.py --binary /path/to/ilink2.dll
```

工具验证样本哈希，不读取配置 / 数据库 / 进程，也不联网。它进一步确认应用与网络会话并非同一组字段；没有据此猜测外层网络账号的真实初始化值，完整 AutoAuth 封包仍待端到端通过。

工具进一步执行 `0x170a72` 起的真实类型分支，在进入任何解析器前停止。虚构非空输入下，类型 1、2、3、5、6、7 进入 `IlinkAppCommSessionInfo` 分支 `0x170aa5`；类型 4 进入 `IlinkAppGetSessionInfo` 分支 `0x170b8e`；类型 8 进入 auth-flag 分支 `0x170e68`；0 走空会话，9 走不支持的类型。前两个消息名由构造函数 `0x11cf50` / `0x11cf90` 的虚表与 RTTI 交叉确认。这里验证的是类型决定的解析路径，不是读取了官方客户端的真实类型，更不能直接认定当前授权应填 4 或某个固定枚举。

另沿调用链确认：`0x7a643` 将上层提供的会话类型保存为 `kTdiKeyIlinkAppSessionType`，AutoAuth 的 `0x3eb99` 读取后交给网络层；`0x100f89` 在复制应用会话后还调用 `SetUin`（`0x1728b0`），后者涉及按身份读取网络同步数据。此前隔离候选中把应用字段和网络字段简单拼到同一包头，尚未覆盖这些初始化条件；没有将仅修改包头标志或收到不同拒绝码描述为完整兼容。

另增加只在 `protocolaudit` 构建存在的 `TestNativeCachedSessionWireAudit`：针对显式提供的私有缓存会话，仅记录 MMTLS 认证结果、固定数字码和长度，不输出密钥、请求和业务正文。12:38 的真实请求确认 MMTLS 认证成功后，应用头为 `-14`、无正文；v0.2.45 因此保留此类真实拒绝及首次重建错误。它与凭据恢复时另收到的 OAuth `42007` 是不同阶段，不能合并解释为同一个错误码。

### 完整调用者中的网络 UIN 更新

复制 helper 不写网络区，不等于完整调用者也不写。继续执行 `UpdateAppSession` 的 `0x10100d`–`0x101032`：应用 getter `0x1709d0` 读取应用会话 `+0x68`；`0x10102f` 调网络账号虚表 slot 10，实际 setter `0x173210` 把该值写入 `+0x390`。`Req2Buf` 在 `0x1a2b5a`–`0x1a2b79` 经 getter `0x172370` 读取同一位置，形成字段 2。

`inspect_native_session_update.py` 新增虚构 identity 的调用者模拟，只替代 wrapper 的 GetAccount accessor，实际应用 getter、虚调用和网络 setter 均执行原生代码，确认该路径的网络 UIN 等于应用 UIN。该切片从前置条件之后开始，没有验证所有会话都会走入该路径，也没有证明服务器接受。

隔离客户端新增 `nativeidentity` 候选：字段 2 取当前应用身份，字段 18 为 0、字段 25 为 17、字段 7 为 0。此前字段 2 为 0 的候选保留以便复现。13:56–13:58 新票据已被拒绝，未取得本轮可用的新 AutoKey，因此 **尚未发出这个新 AutoAuth 候选**；没有重放旧授权的密钥。

### 网络实例决定外层加密分支

继续从 `CreateIlinkNetwork` 追踪 provider，而非只看任务类名：`0x10efa2` 查询设备标识对应的 provider，`0x10f299` 将它传给网络构造，`0xfb542` 保存至网络实例 `+0x20`。`__RunTaskCheckLogic` 在 `0x109750` 调该 provider 的虚表 slot 5，按其返回值选择任务：

| provider（RTTI 与次级虚表交叉确认） | slot 5 实现 | 任务选择与 Init 中的外层模式 |
| --- | --- | --- |
| `IlinkAppInstanceImpl`，provider 虚表 `0x7ee2d8`（对象偏移 8） | `0x191fe0` 固定返回 false | 构造 `0x17a740`；Init 的 `0x17a963` 写模式 0 |
| `IlinkDeviceInstanceImpl`，provider 虚表 `0x7ee9a0`（对象偏移 8） | `0x199c80` 固定返回 true | `AesGcmCGITask` 构造 `0x17a9a0`；Init 的 `0x17abc3` 写模式 16 |

`inspect_native_autoauth.py` 新增两组离线执行：使用实际 provider 虚表、getter 和选择分支，仅替代分配器，在进入完整构造前停止，再单独执行原生模式赋值指令。两组均通过。由此可把“外层是否必须 AES-GCM”收窄到具体实例：上述应用实例路径选择模式 0，设备实例选择 16；不能因 DLL 中存在 AES-GCM 任务就要求所有 AutoAuth 都加一层 AES-GCM。尚未验证整条构造 / Init、当前线上实例创建链与完整服务器接受，这不是成功 AutoAuth 的结论。
