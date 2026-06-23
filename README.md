# 音乐插件

MaiBot 音乐插件，支持搜索点歌、解析音乐链接，发送语音音频。

## 功能

- **搜索点歌**：通过关键词搜索歌曲，发送可播放的语音音频
- **双平台支持**：网易云音乐（163）和QQ音乐（qq）
- **命令触发**：使用 `/点歌` 命令快速点歌
- **LLM 调用**：通过自然语言让 AI 帮你点歌
- **链接解析**：自动识别消息中的音乐链接，发送语音音频
- **卡片解析**：自动识别音乐分享卡片（QQ音乐、网易云卡片和小程序），通过 NapCat HTTP API 获取原始消息中的 jumpUrl 精确解析歌曲 ID，回退时搜索歌名
- **网易云分享文本**：自动识别 `分享xxx的单曲《歌名》: URL (来自@网易云音乐)` 格式
- **网易云短链接**：自动解析 `163cn.tv` 短链接

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/点歌 <歌曲名>` | 使用默认平台点歌 | `/点歌 晴天` |
| `/点歌 163 <歌曲名>` | 使用网易云点歌 | `/点歌 163 晴天` |
| `/点歌 qq <歌曲名>` | 使用QQ音乐点歌 | `/点歌 qq 晴天` |
| `/选歌 <序号>` | 选择搜索结果中的歌曲 | `/选歌 1` |

搜索到多首歌曲时会列出选择列表，使用 `/选歌 <序号>` 选择。只有一首结果时直接发送。

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `false` | 是否启用插件 |
| `music.default_platform` | `"163"` | 默认音乐平台：`163`(网易云) 或 `qq`(QQ音乐) |
| `music.auto_parse_url` | `true` | 是否自动解析消息中的音乐链接 |
| `music.auto_parse_card` | `true` | 是否自动解析音乐分享卡片（QQ音乐/网易云卡片和小程序） |
| `music.search_limit` | `5` | 搜索结果数量 |
| `netease.MUSIC_U` | `""` | 网易云 `MUSIC_U`（用于高音质/付费歌曲） |
| `netease.csrf_token` | `""` | 网易云 `__csrf`（与 MUSIC_U 配对） |
| `qq.uin` | `""` | QQ音乐 `uin`（QQ 号） |
| `qq.qqmusic_key` | `""` | QQ音乐 `qqmusic_key`（用于高音质） |
| `napcat.http_url` | `"http://127.0.0.1:3000"` | NapCat HTTP API 地址（用于解析音乐卡片原始数据） |
| `napcat.http_token` | `""` | NapCat 访问令牌（留空则不鉴权） |

### Cookie 获取方法

不配置 Cookie 时，插件以匿名态请求 API，通常只能获取 128kbps 低音质音频。配置登录 Cookie 后可获取 320kbps 甚至无损音质。

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
