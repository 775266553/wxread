# 阅伴 · GitHub Pages 微信读书多账户控制台

**界面运行在 GitHub Pages，扫码、阅读和发邮件由 GitHub Actions 执行。** 不需要部署服务器或启动 Docker。

Pages 只托管 `pages/` 中的 HTML、CSS 和 JavaScript。页面通过 GitHub API 触发 Actions；Actions 在临时浏览器中生成微信读书的真实二维码，扫码成功后保存账户。阅读任务最多并行 4 个账户，每个账户独立 Cookie、目标时长和收件邮箱。

## 发布到 GitHub Pages

1. 将本次代码推送到你的仓库默认分支。
2. 仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions** 中运行 **Publish GitHub Pages**。Fork 后还需要启用 Actions。
4. 发布成功后打开工作流输出的网址，例如 `https://你的用户名.github.io/wxread/`。

页面地址取决于仓库所有者和仓库名，不使用本机 `127.0.0.1` 地址。Pages 工作流只上传 `pages/`，不会把 Python 文件、数据库、`.env` 或本地登录信息发布出去。

## 第一次连接仓库

页面点击「连接仓库」，填写 `owner/repository` 和自己的 **fine-grained personal access token**。

创建 Token 时只选择这个仓库，授予以下 Repository permissions：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Contents | Read and write | 初始化、读取和更新加密状态分支 |
| Actions | Read and write | 触发扫码/阅读、查看和取消工作流 |
| Secrets | Read and write | 第一次初始化加密密钥 Secret |
| Metadata | Read | GitHub 默认授予 |

可从 [GitHub Token 设置](https://github.com/settings/personal-access-tokens)创建。使用自己的仓库；控制台是给仓库管理者使用的，不是向访客开放的多租户服务。

首次连接会创建：

- 仓库 Secret **`WXREAD_CONTROL_KEY`**：用于解密账户状态和页面指令的私钥。
- 分支 **`wxread-state`**：存储公钥、加密账户配置、加密任务记录和加密扫码结果。

Token 只保存在页面内存中，不写入仓库或浏览器存储。刷新页面需要重新连接；仓库名称可以记住。已经存在的密钥不会在正常重新连接时重新生成。保留 `WXREAD_CONTROL_KEY`，删除或替换它会使已保存的账户状态无法解密。

## 扫码绑定多个账户

1. 连接完成后，点击「添加账户」。
2. 填写备注、每次目标时长，以及可选的独立通知邮箱。
3. 点击「生成扫码二维码」，等待 Actions 启动和准备浏览器，通常需要数十秒，也可能因 GitHub 排队更久。
4. 使用微信扫一扫，在手机上确认登录。页面显示绑定成功后即可关闭扫码窗口。
5. 重复操作即可绑定多个账户。

每次使用独立浏览器会话。重复绑定同一账户会更新登录信息，保留目标、邮箱和启用状态；「重新绑定」必须扫描原账户。

二维码有效期约三分钟，从 Actions 生成二维码起计算。页面刷新会丢失当前浏览器的临时解密密钥，请在扫码完成前保持页面打开。扫码过期后可以重新生成；官网额外验证、网络问题或登录布局变化时，可以通过页面导入 cURL。

扫码和配置操作会等待 Actions 执行，不会像常驻服务器一样立即响应。账户控制工作流串行执行；阅读工作流与它分开运行。工作流保留了原有的 `AutoRead` 环境；如果该环境要求审批，请在 GitHub Actions 中批准运行后继续。

## 定时阅读和运行记录

默认时间为 **北京时间每天 01:07**，GitHub 的 Cron 为 UTC `7 17 * * *`。可在页面「计划与通知」启停自动计划；手动运行仍可使用。

改变时间请编辑 `.github/workflows/deploy.yml` 的 `schedule`，例如：

```yaml
on:
  schedule:
    - cron: '30 0 * * *'  # 北京时间每天 08:30
```

GitHub 定时工作流可能延迟，长期无仓库活动时还可能停用，不能保证精确到分钟。[官方 schedule 说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

「运行全部」执行所有启用账户，也可以单独运行一个账户。一个账户失败不会阻止其余账户；登录失效后标记为需要重新绑定。点击「停止本批」会取消该 Actions 阅读工作流，包含它同时执行的所有账户。

刷新后的 Cookie 会写回加密状态，下一次工作流自动读取，不需要人工回写 Secrets。进度最多约每分钟同步一次；取消工作流时展示最后保存的进度。页面显示最近 100 条记录，加密状态保留最近 1000 条用于统计。

时长由脚本按成功阅读请求计算，不是从微信读书排行榜拉取的官方统计。运行状态和通知依赖第三方接口及 GitHub Actions 的可用性。

## 邮件发送

GitHub 没有通用发信 API，[邮箱 API](https://docs.github.com/en/rest/users/emails)用于管理账户邮箱。

- **只接收 Actions 运行结果提醒**：在 [GitHub 通知设置](https://github.com/settings/notifications)开启 Actions 的 Email 通知。[官方说明](https://docs.github.com/en/subscriptions-and-notifications/how-tos/managing-github-actions-notifications)。
- **接收各账户的阅读结果邮件**：在页面「计划与通知」配置 SMTP 服务器、端口、加密方式、用户名、授权码、发件邮箱和默认收件邮箱。邮件由 Actions 通过你配置的邮箱服务发送。

「发送测试邮件」会触发 Actions，用表单中填写的配置发送，不会自动保存。保存时授权码留空代表保留原授权码。可为账户设置独立邮箱，留空则使用默认收件邮箱。默认地址支持英文逗号分隔多个收件人。

SMTP 授权码加密保存，页面读取设置时只返回「是否已保存授权码」，不会返回原值。任务成功或失败均可通知，手动停止不发送邮件。

## 加密和数据范围

账户 Cookie、SMTP 授权码、邮箱、备注和运行记录均加密后写入状态分支。页面指令使用仓库公钥加密；二维码及页面状态再使用当前浏览器的临时公钥加密，只有发起请求的页面能读取。

公开 Pages 和公开仓库中可以看见页面代码、公钥、密文和工作流元数据，无法直接看到 Cookie 或邮件授权码。Token、私钥和 Cookie 不写入工作流输入或明文日志。状态分支的 Git 历史会保留旧密文；保护仓库访问权限和加密密钥。拥有仓库 Actions/代码管理权限的人应被视为可信管理者。

代码使用本地随页面发布的 libsodium，不从第三方脚本 CDN 加载加密代码。浏览器只需要访问 GitHub API；微信读书登录由 Actions 的 Chromium 访问。

## 兼容原有账户配置

如果已经设置了仓库 Secret `WXREAD_CURL_BASH` 或 `WXREAD_ACCOUNTS_JSON`，首次创建加密状态时会导入。

多账户 JSON 示例：

```json
[
  {"name":"我的账户","curl":"完整的微信读书 read 请求 cURL","read_num":40,"email":"reader1@example.com"},
  {"name":"家人","cookies":{"wr_vid":"占位账户ID","wr_skey":"占位登录值"},"read_num":20,"email":"reader2@example.com"}
]
```

使用完整有效的 Cookie；示例占位值不能登录。`read_num` 每次 30 秒。页面导入只解析 cURL 文本，不执行命令。

未初始化 Pages 控制台时，原有定时脚本仍可从这些 Secrets 运行。初始化后，加密状态成为主要配置，旧 Secret 不再覆盖页面设置。原有 PushPlus、WxPusher、Telegram、ServerChan 环境变量通知继续兼容。

## 文件和验证

| 文件 | 用途 |
| --- | --- |
| `pages/` | GitHub Pages 静态界面 |
| `.github/workflows/pages.yml` | 发布页面 |
| `.github/workflows/control.yml` | 扫码、设置和测试邮件 |
| `.github/workflows/deploy.yml` | 定时或手动阅读 |
| `github_worker.py` | Actions 执行业务操作 |
| `github_state.py` | 加密状态和 GitHub API |
| `requirements-actions.txt` | Actions 专用依赖 |

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

浏览器预览静态界面可用任意静态文件服务器。生产使用 GitHub Pages，不需要运行 `web.py`、`scheduler.py` 或 Docker。
