# 音乐插件

MaiBot 音乐插件，支持搜索点歌、解析音乐链接，发送语音音频。

## 功能

- **搜索点歌**：通过关键词搜索歌曲，发送可播放的语音音频或音乐卡片
- **双平台支持**：网易云音乐（163）和QQ音乐（qq）
- **播放模式切换**：支持语音音频（voice）和音乐卡片（card）两种播放模式，可通过配置切换；card 模式下网易云音乐与QQ音乐均可发送音乐卡片（QQ 音乐卡片由插件自解析直链与元数据并构造）
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

搜索到多首歌曲时会列出选择列表，使用 `{pfx}选歌 <序号>` 选择。只有一首结果时直接发送。若配置 `music.auto_select_first = true`，则多首结果时也跳过选歌阶段，直接发送第一首。
`{pfx}` 为配置的命令前缀，默认为 `/`，可在配置中修改（如 `#`）。

## LLM 调用

AI 通过 `search_and_play_music` 工具点歌时，会直接播放最佳匹配（不走"列出候选→用户选歌"流程）。默认平台播放失败时自动切换到另一个平台重试，并对候选结果逐首尝试，直到成功播放一首。所有平台都无法播放时，工具返回失败信息，AI 会告知用户，不会重复调用工具。

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 是否启用插件 |
| `music.default_platform` | `"163"` | 默认音乐平台：`163`(网易云) 或 `qq`(QQ音乐) |
| `music.command_prefix` | `"/"` | 命令前缀符号，如 `/` 或 `#` |
| `music.auto_parse_url` | `true` | 是否自动解析消息中的音乐链接 |
| `music.auto_parse_card` | `true` | 是否自动解析音乐分享卡片（QQ音乐/网易云卡片和小程序） |
| `music.search_limit` | `5` | 搜索结果数量 |
| `music.auto_select_first` | `false` | 多首结果时跳过选歌阶段，直接发送第一首 |
| `music.play_mode` | `"card"` | 播放模式：`voice`(语音音频) 或 `card`(音乐卡片；网易云为平台型卡片，QQ音乐为自定义音乐卡片) |
| `music.voice_source` | `"local"` | voice 模式的音频来源：`local`(MaiBot 下载到共享缓存) 或 `remote`(NapCat 直接下载远程 URL) |
| `music.cache_storage_dir` | `"/root/maimai/MaiBot/data/music_cache"` | MaiBot 写入音乐缓存的目录 |
| `music.cache_napcat_dir` | `"/app/music_cache"` | 同一缓存目录在 NapCat 进程内的可见路径 |
| `music.cache_max_size_mb` | `1024` | 缓存容量上限，超出后按最久未使用顺序淘汰 |
| `music.cache_expire_hours` | `24` | 删除超过此时间未使用的缓存 |
| `music.cache_cleanup_interval_hours` | `24` | 过期缓存清理间隔 |
| `music.cache_max_file_size_mb` | `50` | 单个 MP3 的下载大小上限 |
| `music.cache_download_timeout_seconds` | `30` | MaiBot 下载 MP3 的超时时间 |
| `netease.MUSIC_U` | `""` | 网易云 `MUSIC_U`（用于高音质/付费歌曲） |
| `netease.csrf_token` | `""` | 网易云 `__csrf`（与 MUSIC_U 配对） |
| `qq.uin` | `""` | QQ音乐 `uin`（登录账号，搜索必需） |
| `qq.qqmusic_key` | `""` | QQ音乐 `qqmusic_key`（登录凭证，搜索必需；账号权益影响可播放范围） |
| `napcat.http_url` | `"http://127.0.0.1:9999"` | NapCat HTTP API 地址（用于解析音乐卡片原始数据、直连发送QQ音乐卡片） |
| `napcat.http_token` | `""` | NapCat 访问令牌（留空则不鉴权） |

### Cookie 获取方法

##### 注：当前的可以点歌的权限和与你获取cookie的账号权限一样（eg.  你的账号有vip那么就可以点vip才可以播的歌，有专辑才可以播放专辑里的）

##### 因为使用的语音的形式发送：音质会被强制降低！！！！！

##### QQ音乐搜索必须配置有效的 `uin` 和 `qqmusic_key`；登录态失效时需要重新获取 Cookie

##### 实际可播放范围仍取决于 Cookie 对应账号的会员、数字专辑购买和版权权限

### 播放模式

`music.play_mode` 控制歌曲以何种形式发送：

| 模式 | 说明 |
|------|------|
| `voice` | 插件获取音频 URL。默认由 MaiBot 下载 MP3 到共享缓存，NapCat 读取本地文件并上传为语音消息 |
| `card` | 网易云音乐：通过 NapCat 平台型 music 段（type=163 + id）发送，NapCat 负责解析音频和卡片展示。QQ音乐：插件自解析歌曲标题、歌手、封面与可播放直链（musicu.fcg），把 url/audio/title/image/content 拼成自定义音乐卡片 CQ 码，**直连 `napcat.http_url` 的 `/send_group_msg`（群聊）或 `/send_private_msg`（私聊）发送**，绕开 MaiBot 适配器对 music 段的改写；直连不可用时回退为自定义 music 段（type=custom）走适配器。卡片均可点击播放 |

> **QQ 音乐卡片说明**：`/点歌 qq` 搜索仍须配置有效的 `qq.uin` 与 `qqmusic_key`（登录态失效需重新获取）；歌曲详情与直链解析匿名即可（已配置时享受账号权益）。直链优先 m4a、失败回退 mp3，发送前 HEAD 校验；受版权/VIP 限制或直链失效时，自动降级发送可点击跳转的音乐卡片。
>
> QQ 音乐卡片由插件从 Runner 侧直连 NapCat HTTP API 发送，**不会写入 MaiBot 的聊天历史**（仅记录日志），且要求 `napcat.http_url` 指向机器人所在的 NapCat、`napcat.http_token` 与 NapCat 配置一致。

card 模式依赖 NapCat 适配器（已支持 `music` 出站段；网易云走平台型、QQ 音乐走 `type=custom`），不会下载或创建本地音乐缓存。

### Voice 本地缓存

`music.play_mode = "voice"` 且 `music.voice_source = "local"` 时，MaiBot 会先下载真实 MP3 到 `cache_storage_dir`，再将 `cache_napcat_dir` 下的对应路径发送给 NapCat。缓存命中会刷新使用时间；缓存超过 1GB 时立即按最久未使用顺序淘汰，并每 24 小时清理超过 24 小时未使用的文件。

MaiBot 和 NapCat 必须能读取同一份缓存文件：

- **同机非 Docker**：将 `cache_storage_dir` 和 `cache_napcat_dir` 配置为同一个绝对路径。
- **NapCat 使用 Docker**：给 NapCat 服务增加只读 volume：

```yaml
volumes:
  - /root/maimai/MaiBot/data/music_cache:/app/music_cache:ro
```

- **MaiBot 和 NapCat 都使用 Docker**：两个服务挂载同一个宿主机目录；MaiBot 挂载为可写，NapCat 挂载为只读。
- **远程 NapCat**：本地路径无法跨主机读取，应将 `music.voice_source` 改为 `"remote"`，或使用共享文件系统。

本地缓存只接受经过格式校验的 MP3，不会将 FLAC/M4A 内容改名为 `.mp3`。

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
2. 首次安装时，将 `config.example.toml` 复制为 `config.toml` 并按部署环境填写配置
3. 在 WebUI 中启用插件，或在 `config.toml` 中设置 `plugin.enabled = true`
4. 重启 MaiBot 或通过 WebUI 热加载插件

## 依赖

- `httpx >= 0.27.0`（MaiBot 已内置）
- `cryptography >= 42.0.0`（用于网易云 eapi 加密，支持付费歌曲）

## 故障排查

- **搜索无结果**：检查网络连接，部分 API 可能需要代理
- **搜索报错**：完整错误会持续追加到 `data/plugins/github.pan-ice.music/log/error.log`；该文件不会自动轮转或清理，且不记录正常运行日志
- **语音音频未发送**：音乐平台可能未返回可用音频 URL；请结合日志中的业务码确认登录态、版权限制或接口响应异常
- **链接未被识别**：确认链接格式与上述支持的格式一致
- **音乐卡片未被解析**：确认 NapCat 开启了 HTTP API，且 `napcat.http_url` 配置正确。当前配置模板使用端口 9999

## 安全说明

### 短链接解析

插件对 `163cn.tv` 和 `c6.y.qq.com` 短链接发起 HTTP 请求以解析重定向目标。为防止 SSRF，重定向目标仅允许以下已知音乐服务域名：

- `music.163.com` / `y.music.163.com`（网易云音乐）
- `y.qq.com` / `i.y.qq.com` / `c6.y.qq.com`（QQ音乐）

重定向到白名单外域名的短链将被拒绝，并在日志中记录警告。

### NapCat 数据访问

当 `napcat.http_url` 已配置且 `music.auto_parse_card` 开启时，插件会调用 NapCat 的 `/get_msg` HTTP API 获取消息的原始 JSON 结构（含 `json` 段），以从中提取音乐卡片的 `jumpUrl` 精确解析歌曲 ID。这意味着插件可以读取适配器转换后纯文本之外的完整消息结构。此行为依赖 NapCat HTTP API 的访问权限，且不在 `_manifest.json` 的 `capabilities` 声明范围内（NapCat API 不由 MaiBot Host 管控）。

## 贡献者

感谢以下贡献者为本插件提交改进：

- [Alnnt](https://github.com/Alnnt) — 自定义命令前缀、多首结果自动选择第一首、QQ音乐发送卡片
- [oversk7](https://github.com/oversk7) — 修复 Tool 调用播放失败的多个问题

欢迎通过 Pull Request 贡献代码。
