# 即梦生图（astrbot_plugin_volc_jimeng）

把火山引擎「视觉智能开放平台 · 即梦AI-图片生成」接进 AstrBot，内置 4.6、4.0、文生图3.1 三个模型：

- **AI 自主决定是否画图**：注册 `jimeng_draw_image` 工具，模型根据对话自己判断；不需要用户记指令
- **自带 WebUI 控制台**：概览 / 生成 / 历史 / 图库 / 预设 / 设置 / R2 指南
- **直连官方接口**：AK/SK 签名调用 `visual.volcengineapi.com`，不经过任何中转
- **完整的限额与留痕**：每日总量、单用户上限、冷却、白名单、历史记录与本地图片

## 安装

1. 把整个 `astrbot_plugin_volc_jimeng` 目录放到 AstrBot 的 `data/plugins/` 下；
2. 在「插件」页重载插件（依赖 `volcengine` 会随插件安装，若失败可在日志页手动安装）；
3. 打开插件详情页，进入「即梦生图控制台」视图。

要求 AstrBot 4.24+（插件可视化视图依赖 `register_web_api` 与 `views/` 机制）。

## 配置

在控制台「设置」页填写，或直接在插件配置里改：

| 配置 | 说明 |
| --- | --- |
| `AccessKey ID` / `SecretAccessKey` | 火山 IAM 的 AccessKey，用于 V4 签名。**不是**方舟 API Key |
| 生图模型 | 4.6（`jimeng_seedream46_cvtob`）、4.0（`jimeng_t2i_v40`）、文生图3.1（`jimeng_t2i_v31`）；4.6/4.0 支持图生图，3.1 仅文生图 |
| 默认尺寸 | 1K～4K 多档，默认 2K 1:1；3.1 最高 2K |
| scale | 文本影响程度，默认 50 |
| force_single | 强制单图，避免一次出多张按张计费 |
| enable_img2img | 是否允许参考图（图生图），默认开 |
| max_input_images | 单次最多参考图数量，默认 4，最大 14 |
| timeout / poll_interval | 异步轮询的超时与间隔 |
| 每日总量 / 单用户上限 / 冷却 / 白名单 | 防止刷额度 |
| 启用 AI 工具 / 注入提示 / 工具内等待 | 控制“AI 自主画图”的行为 |

### 模型能力

| 模型 | req_key | 文生图 | 图生图 | 参考图上限 |
| --- | --- | --- | --- | --- |
| 即梦AI-图片生成4.6 | `jimeng_seedream46_cvtob` | ✅ | ✅ | 14 |
| 即梦AI-图片生成4.0 | `jimeng_t2i_v40` | ✅ | ✅ | 10 |
| 即梦AI-文生图3.1 | `jimeng_t2i_v31` | ✅ | ❌ | 0 |

3.1 只接受文生图；如果带着参考图调用，插件会直接提示「该模型不支持图生图」，不会把图发给接口。
3.1 的尺寸面积上限是 2048×2048，选 4K 档会报错，需要换回 2K 或更小。

### 关于两种凭证

火山控制台里有两套东西，别填错：

- **AccessKey（AccessKey ID + SecretAccessKey）**：给 `visual.volcengineapi.com` 做 HMAC 签名，**本插件用这个**；
- **API Key（API Key ID + API Key Secret）**：方舟 `ark.../api/v3` 用的 Bearer 凭证，本插件用不到。

子账号建议只授予 `CVFullAccess`（或更窄的自定义策略），并关闭控制台登录。

## 使用

| 方式 | 用法 |
| --- | --- |
| 聊天（AI 自主） | 直接说“画一只在雨里的猫”，模型自己决定调用 `jimeng_draw_image` |
| 改图（AI 自主） | 发一张图 + “把背景换成白色”，或引用图片说“改成赛博朋克风” |
| 指令 | `/画图 雨夜霓虹街道，电影感` |
| 控制台 | 「生成」页填提示词；可上传参考图或粘贴图片 URL |

生成通常 20～60 秒。默认「工具内等待」关闭，图片画好后会单独发到当前会话，避免模型工具超时；如果打开它，记得把 AstrBot 的工具调用超时调大（≥180 秒）。

### 图生图（参考图）

4.6 支持 0～14 张输入图，4.0 支持 0～10 张，文生图3.1 不支持输入图。插件会在以下场景自动带上参考图：

- 当前消息里带了图片；
- 引用了带图片的消息（OneBot 适配器会取回被引用消息的图片）；
- 控制台「生成」页上传了图片，或填了图片 URL。

本地图片的两种提交方式：

1. 图片本身有公网 URL（QQ 图片通常有）→ 直接提交；
2. 只有本地文件 → 用 AstrBot 文件服务生成公网 URL，需要先在
   **系统设置 → 网络 → 外部回调地址（`callback_api_base`）** 填写一个外网能访问的地址。

即梦 4.6 **不接受 base64/data URI**，所以没有 `callback_api_base` 时本地上传会直接报错，
请改用图片链接。控制台上传时也会先检查文件服务是否可用。

限制：JPEG/PNG、单图 ≤15MB、最长边 ≤4096、最多 14 张；参考图越多越慢。

### 比例控制

- **文生图**：指令支持 `/画图 9:16 一只猫`，或让模型给 `jimeng_draw_image` 传 `ratio=9:16`；Web 端也内置了 9:16、3:4 等竖版尺寸。
- **图生图**：输出尺寸会按**第一张参考图的宽高比**计算（面积仍取设置里的尺寸档位），不再固定为设置里的比例。
- 消息里显示的是**实际生成的像素和比例**，不是设置值。

### 生成完成后的反馈

| `feedback_mode` | 效果 |
| --- | --- |
| `image_only` | 仅图片 |
| `image_size` | 图片 + 实际尺寸 |
| `image_caption` | 图片 + 实际尺寸和完整提示词 |
| `image_llm`（默认） | 图片 + 大模型回复（模型看图后说一句） |
| `image_size_llm` | 图片 + 尺寸 + 大模型回复 |
| `image_caption_llm` | 图片 + 尺寸和完整提示词 + 大模型回复 |

带 `_llm` 的方式会额外调用一次聊天模型（需支持图片输入），并把图片和回复放在同一条消息里；
失败时只记录日志，图片照常发送。

### Cloudflare R2 托管参考图（推荐）

本地图片也可以先传到 Cloudflare R2，再把 R2 自定义域的公共链接交给即梦，不需要给 ECS 开公网文件服务。

1. 建 R2 bucket，并绑定一个已托管在 Cloudflare 的自定义域（如 `img.example.com`），**不要用 `r2.dev`**（有限速，仅测试）；
2. 创建 R2 API Token：Object Read & Write，Scope 只选该 bucket；
3. 在「设置 → Cloudflare R2」填 Account ID、Bucket、公共地址、AccessKey/Secret；
4. 点「测试 R2 上传」，成功后打开「启用 R2 上传」。

图片会以 `jimeng/日期/UUID.jpg` 命名，默认生成完成后删除；建议再加一条生命周期规则兜底。
R2 不可用时会自动回退 AstrBot 文件服务。完整步骤也写在插件的「R2 指南」页里。

## 目录结构

```
astrbot_plugin_volc_jimeng/
├── main.py                 # 插件逻辑：签名调用、任务、历史、限额、Web API
├── metadata.yaml
├── _conf_schema.json       # 原生配置项（控制台设置页也会写回这里）
├── requirements.txt
├── .astrbot-plugin/i18n/   # 视图标题
└── views/dashboard/        # 控制台页面（index.html / app.js / style.css）
```

运行数据保存在 `data/plugin_data/astrbot_plugin_volc_jimeng/`：

- `history.json`：生成记录
- `presets.json`：提示词预设
- `usage.json`：每日额度计数
- `images/`：原图与缩略图

## 常见问题

**提示词被拦截**：接口会做内容审核（`Text Risk Not Pass`），换一种描述即可。

**鉴权失败**：检查 AK/SK 是否配对、服务器时间是否准确、子账号是否选中了正确策略。

**没有权限 / 模型未开通**：在「即梦AI」控制台确认当前模型已开通，且子账号策略包含 `cv` 服务权限。

**生成超时**：2K 单图通常 20～60 秒；4K 或组图更久。可调大超时，或改用 2K。

## 许可

MIT
