# 主动搭话插件（MaiBot）

让麦麦（MaiBot）**自己找话聊**：随机间隔主动开口，优先接着原有话题；没有话题就做日常问候、卖萌撒娇；需要时可以 **@ 聊过的群友**——开口前会先查人物画像（`query_person_profile`）和历史对话。

> MaiBot 核心只提供 `maisaka.proactive.trigger` 注入接口，本身不会主动发起。本插件补上这一层。

## 特性

- **随机节奏**：最小/最大间隔之间随机取值，不是死板定时
- **静默判定**：群里刚有人说话就不插嘴（可配阈值）
- **活跃时段**：只在白天的指定小时区间活动
- **每日上限**：防止一天刷屏
- **接原话题优先**：默认指令要求它先回顾最近聊天，有未聊完的话题就接着聊
- **可 @ 人**：允许关闭；开启时会先查对方画像与历史，优先找聊过的人
- **配置可视化**：所有参数在 WebUI 设置页可调，每项带中文说明，保存即生效

## 安装

把本仓库放进 MaiBot 的 `plugins/` 目录（文件夹名随意，例如 `whoyummy_proactive_chat`），重启 MaiBot，在 WebUI 的插件页启用。

```bash
git clone https://github.com/whoyummy93/maibot-proactive-chat.git \
  /path/to/maibot/plugins/whoyummy_proactive_chat
```

## 配置

WebUI → 插件 → 主动搭话，可调：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `target_groups` | `[]` | 生效群号，留空=所有群 |
| `min_interval_minutes` / `max_interval_minutes` | `25` / `75` | 判定间隔随机区间 |
| `probability` | `0.45` | 每次判定真正开口的概率 |
| `quiet_minutes` | `12` | 群安静多久才允许开口，`0` 关闭该检查 |
| `active_hours_start` / `active_hours_end` | `8` / `23` | 活跃时段 |
| `max_per_day` | `5` | 每日上限 |
| `allow_at` | `true` | 是否允许 @ 人 |
| `recall_profile` | `true` | 是否先查画像与历史 |
| `intent` | 见插件内默认值 | 主动开口时给模型的指令 |

仓库根目录不放 `config.toml`（遵循插件市场规范），首次运行时由宿主生成。

## 兼容性

- MaiBot `>= 1.3.18`
- 依赖插件能力：`chat.get_group_streams`、`maisaka.proactive.trigger`
- 使用 `chat.receive.before_process` 钩子记录群活跃时间（只读观察，不修改消息）

## 工作原理

```
每轮：随机等待 min~max 分钟
  ├─ 非启用 / 非活跃时段 / 超过每日上限 → 跳过
  ├─ 掷骰未命中 probability → 跳过
  ├─ 目标群最近 quiet_minutes 内有发言 → 跳过
  └─ 否则调用 maisaka.proactive.trigger(session_id, intent)
        → 模型按 intent 回顾历史、查画像、决定是否 @ 谁
```

## License

MIT