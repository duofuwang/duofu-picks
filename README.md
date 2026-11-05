# duofu-picks

一个自动同步开源项目 Release 的 AltStore / AltSource 源。

不用手动传 IPA，也不用改版本号。GitHub Actions 每 4 小时跑一次脚本（北京时间 00:07 / 04:07 / 08:07 / 12:07 / 16:07 / 20:07，分钟数错开整点避开平台拥堵），到各开源仓库的 Release 里找最新的 `.ipa`，更新 `repo.json`，提交后由 GitHub Pages 发布。

**不镜像、不重打包、不代传文件。** 源里的 `downloadURL` 直接指向上游发布地址，用户下载时连的是原作者。

只有一个例外：**「阅读」指向本项目自行构建的产物**。作者已停止在 GitHub Release 提供 IPA，只能自己编，详见[自行构建的条目](#自行构建的条目)。

## 目录结构

```
duofu-picks/
├── config.json              # 你要维护的唯一文件：收录哪些仓库
├── build.py                 # 生成脚本
├── repo.json                # 产物，脚本自动写，不要手改
├── appinfo.json             # 可选缓存，开启 IPA 探测后才出现
├── .nojekyll                # 让 Pages 跳过 Jekyll
└── .github/workflows/sync-release.yml
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

`apps` 里每一项是一个应用，来源从下面三种里选一种：

| 字段 | 必填 | 说明 |
|---|---|---|
| `repo` | 三选一 | `owner/name` 形式，从 GitHub Release 自动跟踪 |
| `ipa` | 三选一 | 外部 IPA 直链，脚本只索引不托管 |
| `file` | 三选一 | 仓库内相对路径，如 `ipas/MyApp.ipa` |
| `bundleIdentifier` | 建议 | 留空则从 IPA 里探测 |
| `name` | 否 | 留空时依次回退到 IPA 内显示名、仓库名 |
| `version` | 否 | 仅 `ipa` / `file` 条目有效，留空则从 IPA 里读 |
| `asset` | 否 | 选哪个资产的正则，默认 `\.ipa$` |
| `excludeTags` | 否 | 要跳过的 tag 列表，用于排除重复或命名混乱的发版 |
| `versionFrom` | 否 | `tag`（默认）或 `asset`，版本号从哪取 |
| `includePrereleases` | 否 | 该仓库是否接受 pre-release |
| `featured` | 否 | 是否进 `featuredApps` |
| `developerName` | 否 | 开发者名，留空取仓库 owner。条目指向自己的 fork 时要手动写成原作者，否则客户端里会显示成你 |
| `iconURL` / `subtitle` / `description` / `category` / `tintColor` | 否 | 覆盖默认值 |
| `minOSVersion` / `buildVersion` | 否 | 写进最新版本条目；留空时从 IPA 的 `Info.plist` 读 |

全局字段：

- `maxVersions`：每个应用保留几个历史版本，默认 0 表示全留
- `includePrereleases`：所有仓库的默认 pre-release 策略
- `detectAppInfo`：是否读取 IPA 探测 `bundleIdentifier`
- `refreshIcons`：默认 `true`，每次构建重新提取图标并覆盖；设为 `false` 则本地有就跳过

## 部署

1. 建一个 **public** 仓库（免费账号的 Pages 只支持公开仓库）。
2. 推到 `main`。
3. 仓库 → Settings → Pages → Source 选 **Deploy from a branch**，Branch 选 `main`、目录 `/ (root)`，保存。
4. 把 `config.json` 里的 `website` 和 `identifier` 改成自己的。
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
python build.py --prune    # 顺带清理没有应用引用的图标
python build.py --no-refresh-icons   # 跳过图标重取，本地调试用
python build.py --proxy none         # 直连；也可写 http://127.0.0.1:7897
```

`--proxy` 默认是 `auto`：Windows 上读系统代理（Internet Settings），如果环境变量里是个回环端口（IDE、沙箱注入的本地中转），会自动换成系统代理并在日志里说明。中转为绕开的情况下，一个应用约 3 秒；踩在坏中转上会退到 10 秒超时再重试，整轮能拖到 9 分钟。

脚本还会拦「成批消失」：如果这一轮比上一版 `repo.json` 少了超过两成应用（至少 3 个），会直接中止、不写文件。额度耗尽、上游抽风、网络故障都会造成这种结果，写出去就是把源写瘸。确实要一次删掉很多应用时加 `--allow-shrink`。

从 `config.json` 删掉一个应用后，`repo.json` 下次生成时就会少掉它，但 `icons/` 里的图标文件会留下。`--prune` 就是清这个的：凡是文件名对不上任何现有 `bundleIdentifier` 的图标，连同 `icons/.sources.json` 里的记录一起删掉。工作流已经带上这个参数。

设了 `GITHUB_TOKEN` 就走认证额度（5000 次/小时），不设只有 60 次/小时。每个应用大约消耗 2 次请求——Release 列表和仓库信息各一次——所以不认证时收录 20 多个应用，本地再调试几轮就会打满。打满后脚本不会覆盖现有 `repo.json`，并列出每个应用的失败原因。

在 GitHub Actions 里不用额外配置，工作流已经把内置的 `secrets.GITHUB_TOKEN` 传进去了。

## 加一个新应用

1. 找到应用的 `CFBundleIdentifier`。最省事的办法是留空 `bundleIdentifier`、把 `detectAppInfo` 设为 `true`，脚本会用 HTTP Range 只读 IPA 里的 `Info.plist`，不下载整个包，结果缓存在 `appinfo.json`。
2. 在 `apps` 里加一条 `{ "repo": "owner/name" }`。
3. 推上去，等 Actions 跑完。

## 图标是怎么来的

按顺序试四种，命中就停：

1. `config.json` 里写的 `iconURL`
2. IPA 里的 `Payload/*.app/AppIcon*.png`，挑体积最大的一张
3. 仓库源码里的 logo —— 文件名含 `icon`/`logo`、放在 `assets/` 一类的目录、2KB 到 2MB 的 PNG 或 JPEG，脚本用 tree API 列一遍文件再按权重挑
4. 都没有，退成 `github.com/{作者}.png`，也就是开发者头像，同时在日志里留一条警告

第 2 步失败的项目不少。图标编译进 `Assets.car` 的包，zip 里根本没有独立 PNG，这时靠第 3 步兜住。

第 2 步取到的 PNG 还有个坑：Xcode 打包时常用 pngcrush 把它转成 Apple 私有的 `CgBI` 格式——`IHDR` 前多一个 `CgBI` chunk、IDAT 用 raw deflate、通道顺序是 BGRA 且 alpha 预乘。iOS 的 `UIImage` 能读，浏览器和 Pillow 这类标准解码器会直接报 `broken data stream`，表现出来就是源在网页上图标全空。脚本在写入 `icons/` 前会用 `normalize_apple_png()` 转成标准 PNG；遇到看不懂的格式原样放过，不会让构建挂掉。

外链图标会被镜像：配置里写 imgur 之类的地址时，脚本下载一份存进 `icons/`，`repo.json` 指向自己的域名。下载失败就保留原地址——不会因为一次网络抖动把好图标换成别的。但如果下回来的根本不是图片（图床删了图、返回一张 HTML 错误页），那就继续走第 2、3 步，实在找不到才用头像。

第 2、3 步默认每次都重新做一遍，取到就覆盖本地那份，所以上游换图标能自动跟上。代价是每次构建都要重新读一遍 IPA —— 这活交给 Actions，本地跑会受网络影响。想省掉这部分开销，在 `config.json` 里写 `"refreshIcons": false`，就退回「本地有就跳过」。

取不到新图时会保留本地旧图，而不是顺手换成仓库里的小 logo。这个判断靠 `icons/.sources.json` 记的来源：旧图来自 IPA 的，就只有 IPA 提取成功才会替换；旧图本来取自仓库 logo 的，才会继续去仓库里刷新。

外链镜像还是缓存优先，不重复去图床拉。想换掉某张 imgur 图，得先把 `icons/` 里对应的旧文件删掉。

生成的图标必须提交，否则 Pages 上访问不到。工作流里已经有 `git add icons`。

## 收录不开源的应用（自己上传 IPA）

除了 `repo`，`apps` 还支持两种条目，区别是 IPA 放在哪：

```json
{ "repo": "owner/name" }                                  // 上游 GitHub Release，自动跟踪
{ "ipa": "https://example.com/app.ipa" }                   // 任意外部直链，不托管
{ "file": "ipas/MyApp.ipa" }                               // 仓库内自带文件，走 Pages 分发
```

`file` 就是「自己上传」：把 IPA 放进 `ipas/`，配置里写相对路径，剩下的交给脚本。它会从 IPA 里读出 `bundleIdentifier`、版本号、构建号和显示名，挑 `AppIcon*.png` 里最大的一张存到 `icons/{bundleID}.png`，再拼出 `baseURL` 下的下载地址和图标地址。

前提是在 `repository` 里配好 `baseURL`：

```json
"baseURL": "https://<用户名>.github.io/<仓库名>"
```

要注意 GitHub 对单文件有 100MB 限制，IPA 很容易超出。超了就走 `ipa` 直链（对象存储、自家服务器），或者用 Release 附件，由 `repo` 条目引用。

### 这条路的边界

源的维护者为所有下载内容背书：放什么，就等于在分发什么。

- **可以**：自己开发的应用、作者书面允许再分发的、企业内部分发给员工、把官方直链做索引
- **不可以**：把别人付费应用、App Store 应用的解密包转存进来分发

后者和你直接用别人的破解源没有区别，只是风险落在了自己的账号上。

## 自行构建的条目

「阅读」是这个仓库里唯一一条不来自上游发版的条目。

**为什么。** 上游 `CHANG-JUI-LIN/Yuedu-reader` 自 2026-06 起不再往 GitHub Release 传 IPA，官方渠道改成 App Store 付费版和 TestFlight。能抓到的最后一个包停在 `1.0.4-2`（2026-05-31），而代码已经走到 3.0.1，中间隔了两个大版本，那个旧包在较新系统上导入还有问题。

**能编到哪一版由许可证决定**，这是改动前必须先确认的事：

| 时期 | 截止提交 | 日期 | 许可证 | 能公开分发构建？ |
|---|---|---|---|---|
| 早期 | `87ffc52` | 2026-07-25 | MIT | 可以 |
| 中期 | `0479b74` | 2026-10-21 | MPL-2.0 | 可以，须提供源码 |
| 现在 | — | 10-21 之后 | PolyForm Strict 1.0.0 | **不行** |

PolyForm Strict 禁止分发构建产物，连修改源码也不允许。所以能合法编的最后一版是 `0479b74`（= 3.0.0 build 3），fork 停在那个提交上，不能往上合。

**怎么做的。** 构建仓库是 [`duofuwang/Yuedu-reader`](https://github.com/duofuwang/Yuedu-reader)，分支 `sideload-3.0.0` 基点 `0479b74`：`macos-26` runner 执行 `xcodebuild archive`，全程 `CODE_SIGNING_ALLOWED=NO`，产物发到该 fork 的 Release，本仓库的 `config.json` 指向它。

没有描述文件时 `Auth.auth()`、`Firestore.firestore()`、`CKContainer(identifier:)` 都会抛 ObjC 异常，而 Swift 抓不住，所以这些都得在构造前拦住，打一组侧载补丁。打包时还会往 `Info.plist` 写 `YueduUnsignedBuild=true`，运行时据此把云服务判为不可用，而不是让 App 崩在启动阶段。

`build-unsigned-ipa.yml` 第一步校验 `LICENSE` 首行是不是 MPL —— 有人把上游新提交合进来会让它直接失败。那是闸，不是 bug。

**条目里写清楚。** 客户端里显示的是该版本的 release notes（`config.json` 的 `description` 只在 release 没写说明时兜底），所以把「自行构建的非官方产物、云功能不可用、需要同步请用官方版本」写进 release notes；`developerName` 固定成原作者，免得客户端里显示成 fork 所有者。

**代价。** 版本永久停在 3.0.0，3.0.1 及之后都拿不到；编译链路要自己维护（成功一次后产物是静态的，不跑就一直在）；许可证不授予商标权，名字和图标严格说仍属原作者。

## 几个实际会遇到的坑

**pre-release 默认不收。** 像 Provenance 这种只在 pre-release 通道发版的，必须给那个仓库单独写 `"includePrereleases": true`，否则会报「没有匹配的资产」。它的 `alpha` 标签下挂着 `Provenance-iOS.ipa`、`Provenance-tvOS.ipa`、`Provenance-iOS-NoExtensions.ipa` 三个包，所以还要用 `"asset": "^Provenance\\.ipa$"` 精确指定，否则可能选到 tvOS 包。

**资产命名不统一。** `iTorrent.ipa`、`StikDebug-3.2.0.ipa`、`BuildBox-2.1.4.ipa` 都是常见写法。默认的 `\.ipa$` 能覆盖绝大多数；一个 Release 挂多个 IPA 时才需要写正则。

**版本号来自 tag。** `v2.2.0-1` 会变成 `2.2.0-1`（只剥掉开头的 `v`）。如果项目 tag 是 `release-2026` 这种没有语义版本号的，改用 `"versionFrom": "asset"`，从资产文件名里抽 `1.2.3` 形式的数字。

**tag 是 commit 哈希的仓库不要收。** 有些项目每次发版用 `ee70177` 这种短哈希当 tag，`version` 字段就会变成一串乱码，客户端里的更新提示毫无意义。判断方法：打开它的 Releases 看一眼 tag 名，不是 `1.2.3` / `v1.2.3` 形式的基本都不适合收录。

**上游删了 Release，你的源里对应版本也会消失。** 每次构建都按当前 Release 列表重新生成，这是有意的：不保留已经不存在的下载地址。

**Pages 有约 10 分钟缓存。** 改了 `config.json` 推上去后，客户端可能还要等一会儿才看到新内容。

**Actions 提交不会自我触发。** 工作流的 `push` 触发限定了只监听 `config.json` 和 `build.py`，脚本自己提交的 `repo.json` 不会造成循环。

## 注意

聚合分发他人的 IPA 涉及原作者授权，请确认每个项目允许再分发，或者只作个人自用。本模板不镜像文件，下载流量仍然走上游。

自行构建的条目要额外看两层：**源码许可证允不允许分发编译产物**，以及**作者是否还提供官方包**。前者决定你能不能放，后者决定你有没有必要放 —— 作者还在正常发版时，自己编一份只会让版本落后、还多一份维护成本。
