# duofu-picks

一个自动跟随开源项目 Release 的 AltStore / AltSource 源。

不需要手动上传 IPA，也不需要手动改版本号：GitHub Actions 每 6 小时跑一次脚本，
去各个开源仓库的 Release 里找最新的 `.ipa`，更新 `repo.json`，提交后由 GitHub Pages 自动发布。

**不镜像、不重打包、不代传文件。** 源里的 `downloadURL` 直接指向上游发布地址，
用户下载时是从原作者那里拉的。

## 目录结构

```
duofu-picks/
├── config.json              # 你要维护的唯一文件：收录哪些仓库
├── build.py                 # 生成脚本
├── repo.json                # 产物，脚本自动写，不要手改
├── appinfo.json             # 可选缓存，开启 IPA 探测后才出现
├── .nojekyll                # 让 Pages 跳过 Jekyll
└── .github/workflows/sync.yml
```

## 配置说明

`config.json` 的 `repository` 是源自身的元信息：

| 字段 | 说明 |
|---|---|
| `name` | 源名称，必填 |
| `identifier` | 源的唯一标识，建议用反写域名，改了会被当成另一个源 |
| `subtitle` / `description` | 列表页和详情页的文案 |
| `website` | 展示用链接 |
| `iconURL` | 源图标地址，建议 512×512 |
| `headerURL` | 顶部横幅 |
| `tintColor` | 主色调，`#RRGGBB` |
| `baseURL` | 部署后的站点根地址。上传本地 IPA 或自动提取图标时必须填 |

`apps` 数组里每一项是一个应用，三种来源三选一：

| 字段 | 必填 | 说明 |
|---|---|---|
| `repo` | 三选一 | `owner/name` 形式，从 GitHub Release 自动跟踪 |
| `ipa` | 三选一 | 外部 IPA 直链，脚本只索引不托管 |
| `file` | 三选一 | 仓库内相对路径，如 `ipas/MyApp.ipa` |
| `bundleIdentifier` | 建议 | 留空则从 IPA 里探测 |
| `name` | 否 | 留空时依次回退到 IPA 内显示名、仓库名 |
| `version` | 否 | 仅 `ipa` / `file` 条目有效，留空则从 IPA 里读 |
| `asset` | 否 | 选哪个资产的正则，默认 `\.ipa$` |
| `versionFrom` | 否 | `tag`（默认）或 `asset`，版本号从哪取 |
| `includePrereleases` | 否 | 该仓库是否接受 pre-release |
| `featured` | 否 | 是否进 `featuredApps` |
| `iconURL` / `subtitle` / `description` / `category` / `tintColor` | 否 | 覆盖默认值 |
| `minOSVersion` / `buildVersion` | 否 | 写进最新版本条目 |

全局字段：

- `maxVersions`：每个应用保留几个历史版本，默认 0 表示全留
- `includePrereleases`：所有仓库的默认 pre-release 策略
- `detectAppInfo`：是否读取 IPA 探测 `bundleIdentifier`

## 部署

1. 在 GitHub 建一个 **public** 仓库（免费账号的 Pages 只对公开仓库开放）。
2. 推到 `main`。
3. 仓库 → Settings → Pages → Source 选 **Deploy from a branch** → Branch 选 `main`、目录 `/ (root)` → Save。
4. 把 `config.json` 里的 `website` 改成你的实际地址，`identifier` 改成自己的。
5. 等一两分钟，源地址是：

```
https://<你的用户名>.github.io/<仓库名>/repo.json
```

在 Feather 的 Sources 页点 `+` 粘贴这个地址，或用 `feather://source/<地址>` 直接唤起。

## 本地调试

```bash
python build.py            # 生成 repo.json
python build.py --check    # 只判断有没有变化，有变化时退出码 1
python build.py --detect   # 强制重新探测 IPA 元数据
```

设了 `GITHUB_TOKEN` 环境变量会用认证额度（5000 次/小时），不设只有 60 次/小时。
每个应用大约消耗 2 次请求（Release 列表 + 仓库信息），所以不认证时收录 20 多个应用、
再本地反复调试几次就会打满。打满后脚本会拒绝覆盖现有 `repo.json` 并列出每个应用的失败原因。

在 GitHub Actions 里不用额外配置，工作流已经把内置的 `secrets.GITHUB_TOKEN` 传进去了。

## 加一个新应用

1. 找到这个 App 的 `CFBundleIdentifier`。最省事的方式是把 `bundleIdentifier` 留空、
   把 `detectAppInfo` 设为 `true`，脚本会用 HTTP Range 只读取 IPA 里的 `Info.plist`，
   不会下载整个包，结果缓存进 `appinfo.json`。
2. 在 `apps` 里加一条 `{ "repo": "owner/name" }`。
3. 推上去，等 Actions 跑完。

## 收录不开源的应用（自己上传 IPA）

`apps` 里除了 `repo`，还支持两种条目，差别在于 IPA 放在哪：

```json
{ "repo": "owner/name" }                                  // 上游 GitHub Release，自动跟踪
{ "ipa": "https://example.com/app.ipa" }                   // 任意外部直链，不托管
{ "file": "ipas/MyApp.ipa" }                               // 仓库内自带文件，走 Pages 分发
```

`file` 这条就是「自己上传」：把 IPA 丢进 `ipas/` 目录，配置里写相对路径，其余交给脚本。
它会自动从 IPA 里读出 `bundleIdentifier`、版本号、构建号和显示名，再把 `AppIcon*.png`
里最大的一张提取到 `icons/{bundleID}.png`，最后拼出 `baseURL` 下的下载地址和图标地址。

前提是在 `repository` 里配好 `baseURL`：

```json
"baseURL": "https://<用户名>.github.io/<仓库名>"
```

需要注意的上限：**GitHub 拒绝超过 100MB 的单文件**，而 IPA 很容易超。超了就走
`ipa` 直链（放对象存储、自家服务器），或者用 Release 附件再由 `repo` 条目引用。

### 这条路的边界

源的所有者能为所有下载内容背书。放什么，就等于你在分发什么：

- **可以**：自己开发的应用、作者书面允许再分发的、企业内部分发给员工、把官方直链做索引
- **不可以**：把别人付费应用、App Store 应用的解密包转存进来分发

后者的风险和「用别人的破解源」是一样的，只是把风险从别人转移到了你自己的账号上。

## 几个实际会遇到的坑

**pre-release 默认不收。** 像 Provenance 这种只在 pre-release 通道发版的，必须给那个仓库
单独写 `"includePrereleases": true`，否则会报「没有匹配的资产」。它的 `alpha` 标签下挂着
`Provenance-iOS.ipa`、`Provenance-tvOS.ipa`、`Provenance-iOS-NoExtensions.ipa` 三个包，
所以还要用 `"asset": "^Provenance\\.ipa$"` 精确指定，否则可能选到 tvOS 包。

**资产命名不统一。** `iTorrent.ipa`、`StikDebug-3.2.0.ipa`、`BuildBox-2.1.4.ipa` 都是常见写法。
默认的 `\.ipa$` 能覆盖绝大多数；一个 Release 挂多个 IPA 时才需要写正则。

**版本号来自 tag。** `v2.2.0-1` 会变成 `2.2.0-1`（只剥掉开头的 `v`）。
如果项目 tag 是 `release-2026` 这种没有语义版本号的，改用 `"versionFrom": "asset"`，
从资产文件名里抽 `1.2.3` 形式的数字。

**tag 是 commit 哈希的仓库不要收。** 有些项目每次发版用 `ee70177` 这种短哈希当 tag，
`version` 字段就会变成一串乱码，客户端里的更新提示毫无意义。判断方法：打开它的 Releases 看一眼 tag 名，
不是 `1.2.3` / `v1.2.3` 形式的基本都不适合收录。

**上游删了 Release，你的源里对应版本也会消失。** 每次都按当前 Release 列表重新生成，
这是刻意为之——不保留已经不存在的下载地址。

**Pages 有约 10 分钟缓存。** 改了 `config.json` 推上去后，客户端可能还要等一会儿才看到新内容。

**Actions 提交不会自我触发。** 工作流的 `push` 触发限定了只监听 `config.json` 和 `build.py`，
脚本自己提交的 `repo.json` 不会造成循环。

## 注意

聚合分发他人的 IPA 涉及原作者授权与许可协议，请确认每个项目允许再分发，或仅作个人自用。
本模板默认不镜像文件，下载流量仍然打在上游。
