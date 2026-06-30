# Spotify Male Artist Auto Skipper

一个 Windows 11 可运行的命令行工具：当 Spotify 正在播放歌曲时，程序会通过 Spotify 官方 Web API 读取当前播放状态，判断当前 track 的 artist 是否为 `male solo artist`，如果命中规则就调用 `POST /v1/me/player/next` 跳到下一首。

它不使用屏幕识别，也不模拟鼠标键盘。

## 功能

- Spotify Authorization Code with PKCE 登录，不需要 client secret。
- token 自动刷新，本地保存到 `token_cache.json`。
- 每 3 秒轮询当前播放歌曲，可在 `config.json` 修改。
- 跳过 podcast/episode、暂停状态、不可控设备、Spotify 明确禁止跳过的状态。
- 播放来源是 Liked Songs / 喜欢的歌曲时默认保留，不进行自动跳过。
- 本地 `artist_gender_cache.json` 优先，缓存缺失时先查 MusicBrainz，再用 Wikidata 兜底。
- 团体/乐队会尽量判断成员性别组成，例如 `all_male`、`all_female`、`mixed`。
- 支持手动标记 artist gender，也支持运行时提示标记 unknown artist 和 unknown 团体组成。
- 支持本地网页客户端，显示当前播放信息，控制上一首/下一首/喜欢歌曲，并用按钮修正 unknown artist。
- 支持 `dry_run`，只打印判断结果，不真的跳过。

## 创建 Spotify App

1. 打开 [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)。
2. 点击 **Create app**。
3. 填写 app 名称和说明，例如 `Spotify Male Artist Auto Skipper`。
4. 在 app 设置里添加 Redirect URI：

```text
http://127.0.0.1:8888/callback
```

5. 保存设置。
6. 复制 app 的 **Client ID**。不要使用 client secret；本项目使用 PKCE。

## 安装

需要 Python 3.11+。

```powershell
cd spotify-male-skipper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
copy config.example.json config.json
```

如果 PyPI 连接失败或遇到 SSL 证书错误，可以改用清华镜像安装依赖：

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
```

如果已经创建过错误 Python 版本的虚拟环境，可以重新创建 Python 3.11 虚拟环境。下面这段里的 `rmdir /s /q` 适合在 CMD 中运行：

```cmd
cd /d "%USERPROFILE%\Documents\Codex\2026-06-24\spotify-male-artist-auto-skipper-spotify\spotify-male-skipper"
rmdir /s /q .venv
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
```

编辑 `.env`：

```text
SPOTIFY_CLIENT_ID=你的 Spotify Client ID
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
MUSICBRAINZ_USER_AGENT=SpotifyMaleArtistAutoSkipper/1.0 (your-email@example.com)
```

MusicBrainz 要求合理的 User-Agent，建议把 `your-email@example.com` 改成你的联系邮箱或项目主页。

## 运行

```powershell
python main.py
```

第一次运行时会打开 Spotify 登录页面。授权成功后，浏览器会跳回：

```text
http://127.0.0.1:8888/callback
```

终端会开始打印当前歌曲、artist 判断结果和动作：

```text
Now playing: Song A - Artist B
Album: Album C
Artist B => male, confidence=0.92, source=musicbrainz
Action: skip
```

## 网页客户端

启动本地网页控制台：

```powershell
python main.py web
```

默认会打开：

```text
http://127.0.0.1:8890
```

网页会显示当前播放歌曲、专辑、artist 判断结果和当前规则下是否会跳过。Web 模式默认也会主动调用 Spotify 跳过接口；同一首歌只会处理一次，避免页面刷新导致重复跳过。网页里也提供进度条、当前时间、总时长、`Prev`、`Next` 和 `Like` 按钮，用于拖动歌曲进度、上一首、下一首和把当前歌曲加入 Liked Songs。遇到 `unknown` artist 时，可以直接点击按钮标记为男、女、其他、团体或未知。按钮会写入本地 `artist_gender_cache.json`，之后命令行自动跳过流程也会使用同一份缓存。

`Like/Liked` 按钮需要 Spotify 授权 scopes `user-library-read` 和 `user-library-modify`。如果你之前已经登录过，旧的 `token_cache.json` 可能没有这些 scope；遇到喜欢状态不更新、喜欢或取消喜欢失败时，删除 `token_cache.json` 后重新运行并授权一次。

如果不想自动打开浏览器：

```powershell
python main.py web --no-open
```

如果只想在网页里观察判断结果，不真的跳过：

```powershell
python main.py web --dry-run
```

如果希望网页刷新时终端也持续打印当前播放和判断结果：

```powershell
python main.py web --verbose
```

当手机或其他非本机设备访问网页客户端时，会要求登录。本机 `127.0.0.1` 访问不需要登录。可以在 `.env` 里固定用户名和密码：

```text
WEB_AUTH_USERNAME=admin
WEB_AUTH_PASSWORD=换成一个你自己的长密码
```

如果没有设置 `WEB_AUTH_PASSWORD`，程序会在启动时生成一个临时密码并打印在终端里。本地局域网访问示例：

```powershell
python main.py web --host 0.0.0.0 --port 8890 --verbose
```

如果通过 Cloudflare Tunnel、反向代理或公网端口转发访问，必须强制所有来源登录，避免代理转发后被识别成本机请求：

```powershell
python main.py web --host 0.0.0.0 --port 8890 --auth-all --verbose
```

也可以在 `.env` 里固定开启：

```text
WEB_AUTH_ALL=true
```

## Dry Run

在 `config.json` 里设置：

```json
{
  "dry_run": true
}
```

或临时运行：

```powershell
python main.py run --dry-run
```

dry run 模式会输出 `Action: dry_run_skip`，但不会调用 Spotify 跳过接口。

## 配置

`config.json`：

```json
{
  "poll_interval_seconds": 3,
  "queue_prefetch_tracks": 3,
  "smart_skip_max_tracks": 10,
  "skip_if_any_artist_male": true,
  "skip_unknown": false,
  "skip_groups": false,
  "skip_all_male_groups": false,
  "keep_male_composers": true,
  "keep_liked_songs": true,
  "prompt_on_unknown": false,
  "dry_run": false
}
```

- `skip_if_any_artist_male=true`：主歌手或 feat 里任意 artist 是 male 就跳过。
- `skip_if_any_artist_male=false`：只看第一个 main artist。
- `queue_prefetch_tracks=3`：web 模式读取 Spotify queue，提前解析后续 3 首 track 的 artist 并写入本地缓存。设为 `0` 可关闭，最大值会限制为 `10`。
- `smart_skip_max_tracks=10`：web 里的“跳到保留”按钮最多连续跳过多少首。该按钮点击后会先直接跳到下一首，再从新播放的歌曲开始用本地缓存判断；循环过程中不会临时查 MusicBrainz/Wikidata。
- `skip_unknown=true`：MusicBrainz 和 Wikidata 都查不到或结果模糊时也跳过。
- `skip_groups=true`：artist 是 group/band 时也跳过。
- `skip_all_male_groups=true`：只跳过成员组成判断为 `all_male` 的团体。
- `keep_male_composers=true`：如果 male artist 被识别为作曲家、配乐师、film score / soundtrack composer，则不跳过。
- `keep_liked_songs=true`：当播放来源是 Liked Songs / 喜欢的歌曲时，整首歌直接保留，不做性别跳过判断。
- `prompt_on_unknown=true`：运行时遇到 unknown artist 或 unknown 团体组成时，在终端提示手动标记。
- `dry_run=true`：只打印判断结果，不真的跳过。

## 手动标记 Artist

当自动判断不准时，可以手动写入本地缓存：

```powershell
python main.py label "Spotify Artist ID" male
python main.py label "Spotify Artist ID" female
python main.py label "Spotify Artist ID" other
python main.py label "Spotify Artist ID" group
python main.py label "Spotify Artist ID" unknown
```

也可以带上名字：

```powershell
python main.py label "Spotify Artist ID" male --name "Artist Name"
```

如果要手动修正团体组成，可以这样：

```powershell
python main.py label "Spotify Artist ID" group --name "Band Name" --group-composition all_male
python main.py label "Spotify Artist ID" group --name "Band Name" --group-composition all_female
python main.py label "Spotify Artist ID" group --name "Band Name" --group-composition mixed
python main.py label "Spotify Artist ID" group --name "Band Name" --group-composition unknown
```

如果要手动保护男作曲家/配乐师，可以这样：

```powershell
python main.py label "Spotify Artist ID" male --name "Composer Name" --artist-role composer_or_score
```

运行时也可以标记 unknown artist 或 unknown 团体组成。把 `config.json` 里的 `prompt_on_unknown` 改成 `true`，或者临时运行：

```powershell
python main.py run --prompt-on-unknown
```

遇到 unknown artist 时会提示：

```text
Unknown artist: Artist Name (Spotify Artist ID)
Label now? [male/female/other/group/unknown, Enter=leave unknown]:
```

输入 `male` 或 `m` 会立刻写入缓存，并且当前歌曲会继续按 male 规则处理；如果 `dry_run=false`，当前歌曲会被跳过。等待输入时如果 Spotify 已经播放到另一首歌，提示会自动取消，脚本会在下一轮处理新歌。

遇到团体但成员性别组成 unknown 时会提示：

```text
Group composition unknown: Band Name (Spotify Artist ID)
Label group composition? [all_male/all_female/mixed/all_other/unknown, Enter=leave unknown]:
```

输入 `all_male` 或 `m` 会立刻写入缓存。如果 `skip_all_male_groups=true` 且 `dry_run=false`，当前歌曲会按全男团体规则跳过。

缓存格式：

```json
{
  "spotify_artist_id": {
    "name": "Artist Name",
    "gender": "male",
    "group_composition": "not_group",
    "artist_role": "unknown",
    "source": "manual",
    "confidence": 1.0
  }
}
```

团体缓存示例：

```json
{
  "spotify_artist_id": {
    "name": "Band Name",
    "gender": "group",
    "group_composition": "all_male",
    "source": "wikidata",
    "confidence": 0.8
  }
}
```

## 判断逻辑

优先级：

1. 读取本地 `artist_gender_cache.json`。
2. 缓存没有时查询 MusicBrainz。
3. MusicBrainz 不明确、报错、超时或限流时，查询 Wikidata。
4. Wikidata 仍不明确时，返回 `unknown`。
5. 最终结果写入缓存。

MusicBrainz 会优先匹配名字完全一致的 artist；没有完全一致时再看高分结果。`type=Group` 会标记为 `group`，`gender=male` 标记为 `male`，`gender=female` 标记为 `female`，`other` / `non-binary` 等会标记为 `other`，`not applicable` 不会当成 `male`。MusicBrainz 请求会带 User-Agent 和 timeout，并且 search / lookup 共用限速，默认低于每秒 1 次请求。

Wikidata 会先搜索同名 entity，并尽量筛选人类、音乐人、歌手或音乐组合相关实体，再读取 `P21` / `sex or gender`。`male` 和 `female` 会分别映射，`non-binary`、`transgender female`、`transgender male`、`intersex`、`agender` 等统一映射为 `other`。多个强匹配无法确认时返回 `unknown`。

如果 male artist 的 MusicBrainz tags/disambiguation 或 Wikidata occupation/description 明确包含 composer、film score、score composer、soundtrack、video game music 等线索，会写入 `artist_role=composer_or_score`。默认 `keep_male_composers=true` 时，这类男作曲家/配乐师不会被跳过。

当 artist 是团体时，成员组成优先从 MusicBrainz 查询：

1. 先用 artist search 确认 `type=Group`。
2. 再用 MusicBrainz artist lookup 请求 `inc=artist-rels`。
3. 从 artist-to-artist 关系中读取成员 artist 的 gender。
4. 如果 MusicBrainz 成员关系不明确，再用 Wikidata 兜底。

Wikidata 兜底会先找到团体 QID，然后合并两类成员来源：

- `P527 has part(s)`：团体实体直接列出的成员。
- `P463 member of`：通过 SPARQL 反查“哪些人是这个团体的成员”。

对每个成员读取 `P21` / `sex or gender` 后，写入 `group_composition`：

- `all_male`：已知成员全部为 male。
- `all_female`：已知成员全部为 female。
- `mixed`：已知成员里出现多种性别。
- `all_other`：已知成员全部为 other。
- `unknown`：成员信息缺失或不足以确认。

如果成员关系缺失，但 Wikidata 的类型或描述明确是 `boy band` / `girl group`，会作为窄范围兜底分别标记为 `all_male` / `all_female`。

## 测试

```powershell
python -m pytest
```

## 已知限制

- Spotify 不提供歌手性别字段。
- MusicBrainz / Wikidata 可能查不到或查错。
- 乐队/组合无法准确判断主唱性别。
- 合作歌曲和翻唱可能误判。
- 需要 Spotify Premium 才能调用跳过接口。
