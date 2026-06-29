# 音乐插件

MaiBot 音乐插件，支持搜索点歌、解析音乐链接，发送语音音频。

## 功能

- **搜索点歌**：通过关键词搜索歌曲，发送可播放的语音音频
- **双平台支持**：网易云音乐（163）和QQ音乐（qq）
- **命令触发**：使用 `/点歌` 命令快速点歌，前缀符号可自定义（如 `#点歌`）
- **LLM 调用**：通过自然语言让 AI 帮你点歌
- **链接解析**：自动识别消息中的音乐链接，发送语音音频
- **网易云分享文本**：自动识别 `分享xxx的单曲《歌名》: URL (来自@网易云音乐)` 格式
- **短链接解析**：自动解析 `163cn.tv` 和 `c6.y.qq.com` 短链接（重定向目标仅限已知音乐域名，防止 SSRF）
- **音乐卡片解析**：自动识别音乐分享卡片（QQ音乐、网易云卡片和小程序），通过 NapCat HTTP API 获取原始消息中的 jumpUrl 精确解析歌曲 ID，回退时搜索歌名

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `{pfx}点歌 <歌曲名>` | 使用默认平台点歌 | `/点歌 晴天` |
| `{pfx}点歌 163 <歌曲名>` | 使用网易云点歌 | `/点歌 163 晴天` |
| `{pfx}点歌 qq <歌曲名>` | 使用QQ音乐点歌 | `/点歌 qq 晴天` |
| `{pfx}选歌 <序号>` | 选择搜索结果中的歌曲 | `/选歌 1` |

搜索到多首歌曲时会列出选择列表，使用 `/选歌 <序号>` 选择。只有一首结果时直接发送。若配置 `music.auto_select_first = true`，则多首结果时也跳过选歌阶段，直接发送第一首。
`{pfx}` 为配置的命令前缀，默认为 `/`，可在配置中修改（如 `#`）。

搜索到多首歌曲时会列出选择列表，使用 `{pfx}选歌 <序号>` 选择。只有一首结果时直接发送。

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `false` | 是否启用插件 |
| `music.default_platform` | `"163"` | 默认音乐平台：`163`(网易云) 或 `qq`(QQ音乐) |
| `music.command_prefix` | `"/"` | 命令前缀符号，如 `/` 或 `#` |
| `music.auto_parse_url` | `true` | 是否自动解析消息中的音乐链接 |
| `music.auto_parse_card` | `false` | 是否自动解析音乐分享卡片（QQ音乐/网易云卡片和小程序） |
| `music.search_limit` | `5` | 搜索结果数量 |
| `music.auto_select_first` | `false` | 多首结果时跳过选歌阶段，直接发送第一首 |
| `netease.MUSIC_U` | `""` | 网易云 `MUSIC_U`（用于高音质/付费歌曲） |
| `netease.csrf_token` | `""` | 网易云 `__csrf`（与 MUSIC_U 配对） |
| `qq.uin` | `""` | QQ音乐 `uin`（QQ 号） |
| `qq.qqmusic_key` | `""` | QQ音乐 `qqmusic_key`（用于高音质） |
| `napcat.http_url` | `"http://127.0.0.1:3000"` | NapCat HTTP API 地址（用于解析音乐卡片原始数据） |
| `napcat.http_token` | `""` | NapCat 访问令牌（留空则不鉴权） |

### Cookie 获取方法

##### 注：当前的可以点歌的权限和与你获取cookie的账号权限一样（eg.  你的账号有vip那么就可以点vip才可以播的歌，有专辑才可以播放专辑里的）

##### 因为使用的语音的形式发送：音质会被强制降低！！！！！

##### 当前QQ音乐的专辑内歌曲解析还没有实现，非专辑的可以正常解析

##### 不配置 Cookie 时，插件以匿名态请求 API，通常只能获取非v非专的歌。

**网易云音乐：**
1. 在浏览器中登录 [music.163.com](https://music.163.com/)
2. 打开浏览器开发者工具（F12）→ Application → Cookies
3. 找到 `MUSIC_U` 和 `__csrf` 两个字段的值
4. 填入对应配置项

**QQ音乐：**
1. 在浏览器中登录 [y.qq.com](https://y.qq.com/)
2. 打开浏览器开发者工具（F12）→ Application → Cookies
3. 找到 `uin` 和 `qqmusic_key` 两个字段的值
4. 填入对应配置项

## 支持的音乐链接格式

### 网易云音乐
- `https://music.163.com/#/song?id=12345`
- `https://music.163.com/song?id=12345`
- `https://music.163.com/m/song?id=12345`
- `https://y.music.163.com/m/song?id=12345`（卡片 jumpUrl）
- `https://163cn.tv/xxx`（短链接，自动解析）

### QQ音乐
- `https://y.qq.com/n/ryqq/songDetail/001ABC`
- `https://y.qq.com/n/m/detail/song/001ABC`
- `https://i.y.qq.com/v8/playsong.html?songmid=001ABC`（卡片 jumpUrl）
- `https://c6.y.qq.com/base/fcgi-bin/u?__=xxx`（短链接，自动解析）

## 安装

1. 将插件目录放入 `plugins/maibot-music/`
2. 在 WebUI 中启用插件，或在 `config.toml` 中设置 `plugin.enabled = true`
3. 重启 MaiBot 或通过 WebUI 热加载插件

## 依赖

- `httpx >= 0.27.0`（MaiBot 已内置）
- `cryptography >= 42.0.0`（用于网易云 eapi 加密，支持付费歌曲）

## 故障排查

- **搜索无结果**：检查网络连接，部分 API 可能需要代理
- **语音音频未发送**：部分歌曲因版权限制无法获取音频 URL
- **链接未被识别**：确认链接格式与上述支持的格式一致
- **音乐卡片未被解析**：确认 NapCat 开启了 HTTP API，且 `napcat.http_url` 配置正确。NapCat 默认 HTTP 端口为 3000

## 安全说明

### 短链接解析

插件对 `163cn.tv` 和 `c6.y.qq.com` 短链接发起 HTTP 请求以解析重定向目标。为防止 SSRF，重定向目标仅允许以下已知音乐服务域名：

- `music.163.com` / `y.music.163.com`（网易云音乐）
- `y.qq.com` / `i.y.qq.com` / `c6.y.qq.com`（QQ音乐）

重定向到白名单外域名的短链将被拒绝，并在日志中记录警告。

### NapCat 数据访问

当 `napcat.http_url` 已配置且 `music.auto_parse_card` 开启时，插件会调用 NapCat 的 `/get_msg` HTTP API 获取消息的原始 JSON 结构（含 `json` 段），以从中提取音乐卡片的 `jumpUrl` 精确解析歌曲 ID。这意味着插件可以读取适配器转换后纯文本之外的完整消息结构。此行为依赖 NapCat HTTP API 的访问权限，且不在 `_manifest.json` 的 `capabilities` 声明范围内（NapCat API 不由 MaiBot Host 管控）。
