"""主动搭话插件：随机间隔主动开口，优先接原话题，可 @ 人并查人物画像。

配置由 WebUI 渲染（字段标题与说明均为中文），改完保存即生效，无需重启。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any, List, Optional

from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from pydantic import Field


class ProactiveSettings(PluginConfigBase):
    """[plugin] 配置节。label 是网页上显示的字段名，description 是下面的说明。"""

    __ui_label__ = "主动搭话"

    config_version: str = Field(
        default="1.0.0",
        json_schema_extra={"label": "配置版本"},
        description="插件升级时用于迁移配置，一般不用改。",
    )

    enabled: bool = Field(
        default=True,
        json_schema_extra={"label": "启用主动搭话"},
        description="总开关。关闭后只在被 @ 或点名时回应，不会自己找话聊。",
    )

    target_groups: List[str] = Field(
        default_factory=lambda: ["902606839", "1077122131"],
        json_schema_extra={"label": "生效群号"},
        description="只在这些群里主动开口。留空 = 所有群；当前已填 ZNR 群与杰林 Quest3 群（不含黑迷）。",
    )

    min_interval_minutes: float = Field(
        default=25.0,
        ge=1.0,
        json_schema_extra={"label": "最小间隔（分钟）"},
        description="两次判定之间的最短等待时间。实际间隔在最小~最大之间随机取。",
    )

    max_interval_minutes: float = Field(
        default=75.0,
        ge=1.0,
        json_schema_extra={"label": "最大间隔（分钟）"},
        description="两次判定之间的最长等待时间。调小会明显更活跃。",
    )

    probability: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        json_schema_extra={"label": "开口概率"},
        description="每次判定时真正开口的概率。0.45 表示约一半的判定会说话，其余跳过。",
    )

    quiet_minutes: float = Field(
        default=12.0,
        ge=0.0,
        json_schema_extra={"label": "静默阈值（分钟）"},
        description="群里安静满多少分钟才允许主动开口。设为 0 表示不检查（容易插嘴）。",
    )

    active_hours_start: int = Field(
        default=8,
        ge=0,
        le=23,
        json_schema_extra={"label": "活跃时段-开始（点）"},
        description="只在此时刻之后主动说话。",
    )

    active_hours_end: int = Field(
        default=23,
        ge=0,
        le=24,
        json_schema_extra={"label": "活跃时段-结束（点）"},
        description="到此时刻前停止主动说话。填 24 表示到当天结束。",
    )

    max_per_day: int = Field(
        default=5,
        ge=0,
        json_schema_extra={"label": "每日上限（次）"},
        description="每天最多主动开口几次。0 表示禁止。",
    )

    allow_at: bool = Field(
        default=True,
        json_schema_extra={"label": "允许 @ 人"},
        description="允许主动点名 @ 某个群友。关闭后只发普通消息。",
    )

    recall_profile: bool = Field(
        default=True,
        json_schema_extra={"label": "先查人物画像"},
        description="开口前先查对方画像和历史对话，优先找聊过的人。关闭可省一次模型调用，但会显得没记性。",
    )

    intent: str = Field(
        default=(
            "你主动开口说话。按这个顺序来："
            "① 先回顾最近的聊天记录，看有没有还没聊完、可以接着说的正经话题；"
            "② 有就顺着原话题接一句，别硬开新坑；"
            "③ 没有合适话题，就做日常性问候（问对方在干嘛、最近怎么样、吃了没），语气卖萌撒娇一点；"
            "④ 想找人说话时可以 @ 对方：先用 query_person_profile 查这个人的画像、以及你们以前聊过什么，"
            "优先找跟你搭过话的人，别 @ 完全没交流过的；"
            "⑤ 整段话要短，一到两句，别说教、别像客服、别报菜单。"
        ),
        json_schema_extra={"label": "开口指令"},
        description="主动开口时给模型的指令。决定它怎么选话题、怎么选人、用什么语气，可自由修改。",
    )


class ProactiveConfig(PluginConfigBase):
    plugin: ProactiveSettings = Field(default_factory=ProactiveSettings)


class ProactiveChatPlugin(MaiBotPlugin):
    """主动搭话插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_activity: dict[str, float] = {}
        self._group_of_stream: dict[str, str] = {}
        self._today = ""
        self._count_today = 0

    async def on_load(self) -> None:
        self._stopping = False
        cfg = self._settings()
        self._task = asyncio.create_task(self._loop())
        self.ctx.logger.info(
            "主动搭话插件已启动：enabled=%s 群=%s 间隔=%s~%s 分钟 概率=%s",
            cfg.enabled, cfg.target_groups or "全部", cfg.min_interval_minutes,
            cfg.max_interval_minutes, cfg.probability,
        )

    async def on_unload(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.ctx.logger.info("主动搭话插件已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """WebUI 保存配置后回调：只记日志，新配置在下一轮判定时自动生效。"""
        try:
            cfg = self._settings()
            self.ctx.logger.info(
                "主动搭话配置已更新：enabled=%s 群=%s 间隔=%s~%s 概率=%s 静默=%s 每日上限=%s",
                cfg.enabled, cfg.target_groups or "全部", cfg.min_interval_minutes,
                cfg.max_interval_minutes, cfg.probability, cfg.quiet_minutes, cfg.max_per_day,
            )
        except Exception:
            pass

    @HookHandler(
        "chat.receive.before_process",
        name="proactive_chat_activity",
        description="记录群内最近发言时间，用于主动搭话的静默判定",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def _record_activity(self, message: dict | None = None, **kwargs: Any) -> None:
        try:
            if not isinstance(message, dict) or message.get("is_notify"):
                return
            session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                return
            self._last_activity[session_id] = time.monotonic()
            info = message.get("message_info") or {}
            group_info = info.get("group_info") if isinstance(info, dict) else None
            if isinstance(group_info, dict):
                gid = str(group_info.get("group_id") or "").strip()
                if gid:
                    self._group_of_stream[session_id] = gid
            if len(self._last_activity) > 200:
                for key, _ in sorted(self._last_activity.items(), key=lambda kv: kv[1])[:50]:
                    self._last_activity.pop(key, None)
        except Exception:
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while not self._stopping:
            cfg = self._settings()
            lo = max(1.0, float(cfg.min_interval_minutes))
            hi = max(lo, float(cfg.max_interval_minutes))
            delay = random.uniform(lo, hi) * 60.0
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                await self._maybe_speak(cfg)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("主动搭话判定异常，已跳过本轮: %s: %s", type(exc).__name__, exc)

    def _settings(self) -> ProactiveSettings:
        cfg = getattr(self, "config", None)
        settings = getattr(cfg, "plugin", None)
        return settings if isinstance(settings, ProactiveSettings) else ProactiveSettings()

    def _is_active_hour(self, cfg: ProactiveSettings) -> bool:
        hour = datetime.now().hour
        start, end = int(cfg.active_hours_start), int(cfg.active_hours_end)
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _maybe_speak(self, cfg: ProactiveSettings) -> None:
        if not cfg.enabled:
            return
        if not self._is_active_hour(cfg):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            self._today, self._count_today = today, 0
        if self._count_today >= int(cfg.max_per_day):
            return
        if random.random() > float(cfg.probability):
            self.ctx.logger.debug("主动搭话：掷骰未命中，跳过")
            return

        streams = await self._target_streams(cfg)
        if not streams:
            self.ctx.logger.debug("主动搭话：没有可用的目标群")
            return
        session_id, group_id = random.choice(streams)

        if cfg.quiet_minutes > 0:
            last = self._last_activity.get(session_id)
            if last is not None and (time.monotonic() - last) < cfg.quiet_minutes * 60.0:
                self.ctx.logger.debug("主动搭话：群 %s 刚有人说话，跳过", group_id)
                return

        intent = cfg.intent
        if not cfg.allow_at:
            intent += "（本次不要 @ 任何人。）"
        if cfg.recall_profile:
            intent += "（说话前先调用 query_person_profile 回忆对方画像与你们的历史对话。）"

        reason = f"静默超过 {cfg.quiet_minutes:.0f} 分钟，随机触发"
        await self.ctx.maisaka.proactive.trigger(
            session_id,
            intent,
            reason=reason,
            priority="low",
            metadata={"plugin": "proactive-chat", "group_id": group_id},
        )
        self._count_today += 1
        self.ctx.logger.info("主动搭话已触发：群=%s 今日第%d次", group_id, self._count_today)

    async def _target_streams(self, cfg: ProactiveSettings) -> list[tuple[str, str]]:
        try:
            result = await self.ctx.chat.get_group_streams(platform="qq")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("获取群聊列表失败: %s", exc)
            return []
        if isinstance(result, dict):
            items = result.get("data") or result.get("streams") or []
        else:
            items = result or []
        out: list[tuple[str, str]] = []
        allow = {str(g).strip() for g in cfg.target_groups if str(g).strip()}
        for item in items:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
            gid = str(item.get("group_id") or item.get("item_id") or "").strip()
            if not sid:
                continue
            gid = gid or self._group_of_stream.get(sid, "")
            if allow and gid not in allow:
                continue
            out.append((sid, gid))
        return out


ProactiveChatPlugin.config_model = ProactiveConfig


def create_plugin() -> ProactiveChatPlugin:
    return ProactiveChatPlugin()
