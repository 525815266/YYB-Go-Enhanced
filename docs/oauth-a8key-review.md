# 公众号自动授权：附件与镜像实测评估

日期：2026-10-22。关联：[issue #75](https://github.com/525815266/YYB-Go-Enhanced/issues/75)。

## 结论

镜像提供了有效线索。维护者已用本项目的现有 YYB 会话、自己编写的隔离诊断程序，在飞牛复现 **A8Key → 微信授权页 → 确认授权 → 带 code/state 的业务回调 Location**。微信实际返回 HTTP 301，回调域名、路径和 state 均与本次请求匹配；没有访问业务回调或兑换业务 token。

这支持继续实现公众号自动授权，不再只是从接口名称推测可行性。但收到的 diff 走的是另一条错误链路，不能原样合入。此次是协议验证和评审，运行中的 v0.2.33 与 `/wx/oauth` 接口行为没有改变。

## issue 中“已经成功”指什么

| 来源 | 实际表达 / 证据 | 能确认的范围 |
| --- | --- | --- |
| X-nancy，10 月 16 日 | 提供 smallcat-client v2.0.0 的请求/响应，含 code、带会话票据的 full_url 和业务回调 URL | 对方接口返回了完整授权结果，与本项目仅构造 URL 存在实际能力差异 |
| Bigyayaya，10 月 19 日 | “我朋友自己改的 yyb 协议也增加了公众号自动授权出 code” | 转述已有实现，未附对应源码 |
| 241306，10 月 19 日 | “我自己加个补丁改好了” | 明确表示其自己的补丁已完成；不能直接认定是后来另一个账号上传的 diff |
| qingyunzhishang5173，10 月 22 日 | 上传说明，评论“包子给写的 参考一下”，之后提供 smallcat 镜像 | 说明记录 build/vet/test 通过，同时明确写有“待真机验证项”；没有证明这份 diff 在真实 YYB 会话上成功 |
| 本次维护者验证 | 独立 A8Key 请求成功，微信确认页返回匹配的回调 code | 已复现到微信签发网页授权 code，尚未验证业务 token 交换、全部公众号或全部部署环境 |

因此，反馈者指出的能力确实存在；前一次针对 js-login 写法的失败不能推广为“YYB 做不到”。同样，也不能把别人不同实现的成功直接归到这份附件补丁上。

## 镜像实际链路

对附件中的两个 Go ELF 进行函数表和调用点分析，再用断网容器、假参数核对请求序列化结果。没有将真实账号凭据交给第三方二进制。

```text
现有 YYB / ILink 登录会话
    → 构造公众号 authorize URL
    → GetA8Key（cmd=5112，/ilink/ilinkapp/mm/ilinkgeta8key）
    → 获得带会话票据的 FullURL
    → GET 微信 authorize 页面
    → 根据页面返回字段请求 authorize_reply
    → 微信返回 301 Location：目标回调?code=…&state=…
    → 校验回调与 state，取出 code；不自动访问业务回调
```

实际观察到授权页先返回 HTTP 200，随后确认接口返回 HTTP 301。不能只支持一次 302 跳转，也不能把 HTTP 200 的授权页当成最终成功。

与附件说明相冲突的关键证据：

| 文件 / 函数 | 观察 |
| --- | --- |
| smallcat `main.handleCompatWXOAuth`（`0x730100`） | 调用 `runLocalWX` 后提取 A8Key FullURL，必要时继续网页授权 |
| smallcat `0x759380` | 实际函数名是 `main.extractA8KeyFullURL`，说明却把它解释为 js-login |
| smallcat `main.(*application).runLocalWXOnce` | OAuth 分支给 helper 传入 `-get-a8key`、`-geta8key-url` |
| wmpf-pure `main.buildGetA8KeyIlinkAppRequest`（`0x6ff7e0`） | 使用命令号 `5112` 和独立 ILink CGI，非 `js-login`、非 `operateWxData` |
| smallcat `main.fetchAuthorizeReplyLocation` | 解析网页中的确认参数，访问 `authorize_reply` 后读取 Location |

函数地址只对应本次附件，不能作为其他版本的固定地址。

## 本次真实验证

使用维护者自有账号、同一有效缓存会话和直连出口，公众号参数来自此前本人授权的 HAR。诊断程序只读数据库、输出脱敏判定；开始前通过现有服务正常取码建立缓存，没有直接修改账号、代理配置或清除会话。真实网络测试执行的是本项目编译的诊断程序，不是镜像程序。

| 步骤 | 实际结果 |
| --- | --- |
| 普通小程序取码对照 | 传输成功，返回 32 字节 code |
| 原补丁的 js-login URL 写法（此前对照） | 内层 `-12000`，无 code |
| 独立 ILink GetA8Key | 业务状态 0，FullURL 为微信 authorize 地址，带会话票据，state 匹配 |
| 请求微信授权页 | HTTP 200，出现本期授权确认表单 |
| 请求微信确认授权 | HTTP 301，Location 指向本次指定的业务回调，包含非空 code 和匹配的 state |
| 访问业务回调 / 兑换业务 token | 未执行，不将其记为已通过 |

前述会话票据、code、账号身份和原始授权页均未写入公开报告。

## diff 的具体问题

1. **协议路径不符。** diff 把 authorize URL 放到 JSLoginRequest 字段 3，并假设微信会直接返回回调。镜像实际走独立 GetA8Key；公开 JSLogin 定义中字段 3 还是 scope，不是 URL。此前实测也已否定该写法。
2. **缺少网页确认阶段。** 本次真实流程需要处理授权页和 `authorize_reply`，不是改一次 js-login 请求即可完成。
3. **成功判断过宽。** `parseOAuthCodeResponse` 将任意非空裸字符串接受为 OAuth code，不核对目标回调和 state；有候选值时也没有严格检查内层业务失败。不能用小程序裸 code 替代网页授权 code。
4. **state 有重复生成风险。** HTTP handler 构建一次授权 URL，协议层又 Build 一次；省略 state 时可能生成两套值，应该只生成一次并贯穿全程。
5. **错误信息可能暴露票据。** 截断 URL 仍可能保留 key/code 等敏感 query，错误应返回阶段和状态，不回显原始地址。
6. **重试和并发需要重新设计。** 不应对业务拒绝一律清缓存、换出口重试；确认授权会签发一次性 code，不能笼统视为无副作用。应复用账号并发控制，并区分取票据失败、待确认、已签发及未知结果。

`git apply --check` 通过只证明补丁格式及上下文匹配。附件中的模拟测试按假定响应构造成功样例，无法证明假定协议成立。

## 接入本项目还需处理的内容

已有的 ILink 会话、加密和短链传输可以复用，不需要让生产服务依赖第三方镜像。需要新增 GetA8Key 请求与响应解析，单独处理授权页确认，并将成功标准收紧到预期回调和 state。

还发现一项底层兼容点：当前 `pbLen` / `pbVar` 将字段标签直接转成单字节；字段号达到 16 时，应将整个标签按 varint 编码。A8Key 使用了 16–19 等字段，诊断程序用正确编码构造后与镜像的假数据输出逐字节一致。正式实现时应修复工具函数并补回归测试，不能照搬现有 helper 后忽略高编号字段。

自动授权应有明确的调用方式，保留现有“只生成 URL”的兼容行为；只请求必要的微信 HTTPS 地址、限制跳转和响应大小，不自动访问调用方传入的任意回调地址。协议取票据和网页确认应保持账号配置的出口，不能在中途静默切换直连。第三方平台 `component_appid`、首次授权等尚未覆盖的场景应明确返回待授权，而非伪造成功。

本次只证明一个自有账号、一个公众号场景的关键链路。正式接入仍需测试代理、过期票据、拒绝授权、并发调用和平台兼容；青龙脚本需要的是最终 OAuth code/业务登录态，不能直接复用现有小程序 code。

## 附件识别

- 镜像标签：`smallfawn/smallcat:latest`，Linux amd64。
- `smallcat镜像.tar` SHA256：`4528c74b9bb6a9f760a1a17101d229f9cb043f9f6a2fb83886a094d97517a174`。
- diff SHA256：`e3cb4ac8bf04c15b071c2690547c485fda60c0e9188f7d9cb9e8c562f5dbb6fa`。
- smallcat 二进制 SHA256：`9d14f5b799ec3286ff491b1cac132e787e791d27f1e3a5e7c162a262f886b155`。
- wmpf-pure 二进制 SHA256：`34c2c744fe4b1caf05e136b8c12105dbcb400268add64bd26650f8f867469e07`。

本报告不分发第三方镜像或其完整恢复代码，只记录互操作所需的观察和验证边界。
