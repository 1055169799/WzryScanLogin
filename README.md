# WzryScanLogin —— 王者营地扫码登录辅助插件

独立插件，放在 `gsuid_core/plugins/WzryScanLogin`，**不修改 WzryUID 的任何文件**，
WzryUID 更新时不会因为本插件而报错。

作用：通过微信 / QQ 扫码登录王者营地，自动把 WzryUID 需要的 **营地 UID** 和 **CK**
写入 WzryUID 的账号池，并可选地自动绑定 UID。所有参数都能在
**GsCore 管理控制台（http://localhost:8765/app）→ 插件 → WzryScanLogin** 里调整。

---

## 1. 它和 WzryUID 的数据是怎么对上的

分析 WzryUID 的代码后可以确认，它查询营地数据时只需要两样东西：

| WzryUID 位置 | 含义 | 本插件扫码后写入的值 |
| --- | --- | --- |
| `wzryuser.uid` | 营地账号的用户 ID（营地 UID） | 营地 `/user/login` 返回的 `data.userId` |
| `wzryuser.cookie` | 营地登录凭证（大家常说的营地 CK） | 营地 `/user/login` 返回的 `data.token` |
| `wzrybind.uid` | 要查询的玩家营地 UID | 默认自动绑定上面的营地 UID |

补充说明：

- `wzryuser.stoken` 是 gsuid_core `User` 基类自带的字段，WzryUID 并没有读取它，
  所以本插件写入时留空。
- WzryUID 的 `get_ck()` 会从 `wzryuser` 表随机取一条有效 CK，再用
  `header["token"]` / `header["userid"]` 访问营地接口；这两项正是上面的
  `cookie` 和 `uid`。
- 登录返回的 `encodeRes` 解密后还有 `userKey`，本插件会解析并保留在登录结果里，
  但 WzryUID 当前版本不使用它，所以不入库。

---

## 2. 扫码登录是怎么实现的

### 微信（纯 HTTP）

参考 Yunzai 的 `GloryOfKings-Plugin/utils/wechatLogin.js`，完整移植为 Python：

1. `POST https://ssl.kohsocialapp.qq.com:10001/a/getwxsdkticket` 获取 `sdkTicket`
2. 用 `appid + noncestr + timestamp + sdk_ticket` 做 SHA1 签名，
   `GET https://open.weixin.qq.com/connect/sdk/qrconnect` 拿到二维码和 `uuid`
3. 长轮询 `https://long.open.weixin.qq.com/connect/l/qrconnect?f=json&uuid=...`
   - `wx_errcode=408` 等待扫码
   - `wx_errcode=404/405` 已扫码，提示用户确认
   - `wx_errcode=405` 且带 `wx_code` 时成功
   - `402` 过期 / `403` 取消 / `500` 服务异常
4. 把 `code` 和 RSA 加密后的 `specialEncodeParam` 一起 POST 到
   `https://ssl.kohsocialapp.qq.com:10001/user/login`
5. 解析返回的 `userId` / `token` / `encodeRes`

### QQ（浏览器 + 协议换票）

参考 `GloryOfKings-Plugin/utils/qqLogin.js`。QQ 的授权页要求二维码和轮询在**同一个浏览器会话**里完成，
所以这里用 Playwright 启动本机 Chrome / Edge：

1. 打开
   `https://openmobile.qq.com/oauth2.0/m_authorize?client_id=1105200115&scope=all&redirect_uri=auth://tauth.qq.com/&style=qr&response_type=code`
2. 从页面里截取二维码（优先元素截图，失败时整页截图）
3. 监听页面请求 / 响应，捕获最终回调 `auth://tauth.qq.com/?code=...` 中的 `code`
4. 用 YSDK 的 `QQCodeLogin` 接口换取 `accessToken` / `openId` / `payToken`
   （HMAC-SHA256，密钥和签名格式与参考插件一致）
5. 带上三件套 POST 营地 `/user/login`，拿到同样的 `userId` / `token` / `encodeRes`

### 登录后

1. 可选：调用营地 `game/allrolelistv3` 校验 CK 是否立即可用
2. 写入 `wzryuser`（uid + cookie）
3. 可选：写入 `wzrybind`，把该营地 UID 绑定到发起人，并切换为当前 UID

写入时优先复用已经加载的 WzryUID SQLModel；如果找不到模型（例如 WzryUID 未加载），
退化为直接 SQL，仍然写同一张表。这样不会和 WzryUID 的 import 路径强耦合。

> GsCore 对单条入站消息的处理预算是 30 秒（`handler._INBOUND_BUDGET_S`）。
> 扫码可能等几分钟，所以指令本身只负责登记会话并立即返回，
> “等扫码 → 校验 → 入库 → 回复”都在后台 asyncio 任务里执行，
> 不会被 30 秒预算打断。发送「王者取消扫码登录」会立刻打断微信长轮询并释放 QQ 浏览器。

---

## 3. 安装与启动

本插件只依赖 gsuid_core 已有依赖：

- `aiohttp`
- `cryptography`
- `playwright`（仅 QQ 扫码需要；gsuid_core 已依赖）
- 本机安装 Chrome 或 Edge（QQ 扫码用）

把整个 `WzryScanLogin` 文件夹放到：

```
<gsuid_core>/gsuid_core/plugins/WzryScanLogin
```

然后重启 GsCore（发送 `gs重启`，或在管理控制台点重启）。启动日志里应出现：

```
[王者扫码] 插件已加载，配置文件：...\data\WzryScanLogin\config.json
```

如果 QQ 扫码报浏览器启动失败，可在控制台把「QQ登录浏览器」改成 `chrome` 或 `msedge`；
也可以先在本机确认浏览器能正常打开。

---

## 4. 指令

| 指令 | 说明 |
| --- | --- |
| `王者扫码登录` / `王者营地扫码登录` | 使用控制台里的默认方式 |
| `王者微信扫码登录` / `王者wx扫码登录` | 强制微信扫码 |
| `王者QQ扫码登录` / `王者qq扫码登录` | 强制 QQ 扫码 |
| `王者扫码登录 wx` / `王者扫码登录 qq` | 临时指定方式 |
| `王者扫码状态` | 查看进行中的会话和本账号已保存的 CK |
| `王者取消扫码登录` | 关闭当前二维码 / 浏览器 |
| `王者扫码帮助` | 指令帮助 |
| `王者检查营地CK` | 检查账号池中全部 CK 是否有效（只读） |

默认只允许**私聊**发起（防止群里的二维码被别人扫走）。
管理员可以在控制台打开「允许群聊发起」。

发起扫码后，机器人会先提示：

> 此处扫码使用的账号为王者营地账号，在这里登录会使王者营地APP被顶号。
> 扫码后请勿在营地APP登陆此账号，否则会顶号失效。
> 建议使用不登营地APP的账号扫码。

这条提示会和二维码、其他提示语一起按「扫码消息撤回」配置自动撤回。

---

## 5. 管理控制台配置项

打开 `http://localhost:8765/app`，登录后进入「插件」→ `WzryScanLogin`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 默认扫码方式 | `wx` | `王者扫码登录` 默认用微信还是 QQ |
| 启用微信扫码 | 开 | 关闭后微信指令不可用 |
| 启用QQ扫码 | 开 | 关闭后 QQ 指令不可用 |
| 允许群聊发起 | 关 | 关闭时只允许私聊 |
| 二维码有效期 | 180 秒 | 等待扫码 / 确认的最长时间 |
| 扫码消息撤回 | 90 秒 | 二维码和三条提示语发送后自动撤回；0 表示不撤回 |
| 轮询间隔 | 1.5 秒 | 查询扫码状态的间隔 |
| 自动保存CK | 开 | 写入 `wzryuser` |
| 自动绑定UID | 开 | 写入 `wzrybind` |
| 自动切换UID | 开 | 多 UID 时把新 UID 切为当前 |
| 保存前校验CK | 开 | 保存前调用一次营地接口 |
| QQ登录浏览器 | `auto` | `auto` 依次尝试 Chrome → Edge → Chromium |
| QQ无头模式 | 开 | 后台运行浏览器 |
| QQ页面加载超时 | 90 秒 | 打开授权页的最长等待 |
| 检查并发数 | 3 | 发送「王者检查营地CK」时同时请求数 |

> 当前 GsCore 前端（0.4.2）还没有可用的配置按钮控件，
> 所以 CK 检查放在聊天指令「王者检查营地CK」里，
> 控制台只保留并发数配置，避免出现无法操作的控件。

### 限时撤回说明

二维码和三条提示语默认在发送后 **90 秒** 自动撤回（可在控制台调整，0 = 关闭）。
取消或登录失败时会立即撤回，不再等 90 秒。

撤回依赖 GsCore 的 `wait_recall` 回执和 `excute_delete_message` 下发，
需要适配器支持：

- 支持回执的 OneBot / NoneBot2 适配器可直接使用。
- 本机的 HoshinoBot 桥接文件原本没有实现这两类协议，已在
  `hoshino/modules/GenshinUID/GenshinUID/client.py` 和 `models.py` 中补齐，
  并保留了 `*.wzry_scan_login.bak` 备份。若以后更新了这两个文件，需要重新应用补丁。
- 适配器不支持时消息仍会正常发送，只是无法撤回，插件不会因此卡住扫码流程。

---

## 6. 文件说明

```
WzryScanLogin/
├── __init__.py     插件入口：注册配置和指令
├── config.py       GsCore 控制台配置模板
├── login.py        微信 / QQ 扫码协议
├── store.py        写入 wzryuser / wzrybind，读取账号池，CK 校验
└── README.md
```

配置文件位置：

```
<gsuid_core>/data/WzryScanLogin/config.json
```

---

## 7. 常见问题

**扫码后提示“营地登录成功，但 CK 校验未通过”**
说明营地签发的 token 暂时不能用。可先关闭「保存前校验CK」再试，或重新扫码；
账号本身有风险时也可能出现。

**QQ 扫码报 `BROWSER_UNAVAILABLE`**
本机没有可用的 Chrome/Edge，或 Playwright 驱动被安全软件拦截。
安装 Chrome/Edge 后在控制台切换「QQ登录浏览器」，仍不行就改用微信扫码。

**提示“你没有绑定过UID”**
本插件默认会自动绑定。若关闭了「自动绑定UID」，登录成功后发送
`王者绑定UID <营地UID>` 即可。

**会不会影响 WzryUID 更新？**
不会。本插件不在 WzryUID 目录内，也没有 import WzryUID 的内部模块路径；
它只读写 WzryUID 使用的数据库表，表结构来自 gsuid_core 的 `User` / `Bind` 基类。

**安全性**
扫码登录等价于把营地账号的登录态交给 bot。建议使用小号，并只在自己的私聊里扫码。
本插件不会在日志里记录完整 token，指令回复里的 CK 也会打码。
