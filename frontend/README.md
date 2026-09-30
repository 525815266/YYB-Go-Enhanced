# YYB Vben 控制台（测试分支）

Vue 3、TypeScript、Vite、Pinia、Ant Design Vue，以及官方 Vben 的核心布局和菜单组件。
不是把原生页面重新命名为 Vben，也不是完整拷贝官方示例应用。

Vben 源码固定于 `vendor/UPSTREAM.json` 中记录的提交，MIT 授权保留在 `vendor/LICENSE`。
`vendor/layout-icons.json` 为 Element Plus 图标集的 fold/expand（MIT），避免运行时请求图标 CDN。
上游依赖的样式和组件源码保留原貌，业务页面位于 `src/views`。

## 构建

需要 Node.js >= 22.18（建议 Node 24）。

```sh
cd frontend
npm ci
npm test
npm run build
```

构建结果写入 `resource/static/console/`，该目录不提交 Git。
Go 编译前完成构建，生成的资源会嵌入独立二进制。
Docker、Release、Magisk 构建脚本已接入这一步；不需要在运行设备安装 Node。
单独使用 `go build` 而未构建前端时，旧页面仍可使用，`/console/` 返回明确的未构建错误。

新控制台入口：`http://服务器地址:端口/console/`。旧入口 `/` 暂时保留，不自动切换。
会话继续使用后端 HttpOnly Cookie。没有浏览器保存的管理员密码或另一个认证数据库。
默认 SQLite、本地 MySQL 配置及公开协议 API 均未改变。

## 迁移范围

Vue 原生页面：登录、注册、微信账号、用户管理、个人设置、调用配置、运行管理、扫码、代理、授权链接和系统维护。
旧入口仍保留以便回归；新控制台路由不再嵌入旧页面。代理页面保留账号 ID 和返回位置。
扫码页在服务端启用本机微信登录时显示对应入口，其他情况下直接显示二维码。

生产微信授权、代理出口及真实青龙执行需要独立配置验证；测试数据不证明外部服务成功。
更多测试信息见 [迁移说明](../docs/ui-redesign.md)。
