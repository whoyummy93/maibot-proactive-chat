"""主动搭话插件：随机间隔主动开口，优先接原话题，可 @ 人并查人物画像。

配置由 WebUI 渲染（每项都有中文说明），改完保存即生效，无需重启。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any, ClassVar, List, Optional

from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from pydantic import Field


class ProactiveSettings(PluginConfigBase):
    """[plugin] 配置节。每项 description 会显示在 WebUI 配置页。"""

    __ui_label__ = "主动搭话"

    config_version: str = Field(default="1.0.0", description="配置版本，插件升级时用于迁移，一般不用改。")

    enabled: bool = Field(default=True, description="总开关。关闭后插件不再主动说话。")

    target_groups: List[str] = Field(
        default_factory=list,
        description="生效的群号列表。留空 = 所有群聊都生效；只填想让它主动说话的群更安全。",
    )

    min_interval_minutes: float = Field(
        default=25.0, ge=1.0, description="两次判定的最小间隔（分钟）。实际间隔在 min~max 之间随机。",
    )
    max_interval_minutes: float = Field(
        default=75.0, ge=1.0, description="两次判定的最大间隔（分钟）。调小会明显更活跃。",
    )

    probability: float = Field(
        default=0.45, ge=0.0, le=1.0,
        description="每次判定时真正开口的概率（0~1）。0.45 表示约一半的判定会说话，其余跳过。",
    )

    quiet_minutes: float = Field(
        default=12.0, ge=0.0,
        description="群里安静满多少分钟才允许主动开口。设为 0 表示不检查闲聊状态（容易插嘴）。",
    )

    active_hours_start: int = Field(
        default=8, ge=0, le=23, description="活跃时段开始（0~23 点）。不在时段内不主动说话。",
    )
    active_hours_end: int = Field(
        default=23, ge=0, le=24, description="活跃时段结束（0~24 点，可填 24 表示到当天结束）。",
    )

    max_per_day: int = Field(
        default=5, ge=0, description="每天主动开口的上限次数。0 表示禁止。",
    )

    allow_at: bool = Field(
        default=True,
        description="允许主动 @ 某个人。关闭后只发普通消息，不会点名。",
    )

    recall_profile: bool = Field(
        default=True,
        description="要求它先查人物画像（query_person_profile）和回顾历史对话，再决定说什么、@ 谁。关闭可省一次调用但会显得没记性。",
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
        description=(
            "主动开口时给模型的指令。决定它怎么选话题、怎么选人、用什么语气。"
            "默认已包含「回顾历史→接原话题→没有就问候撒娇→可 @ 聊过的人」。"
        ),
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
        self._task = asyncio.create_task(self._loop())
        self.ctx.logger.info("主动搭话插件已启动（间隔 %.0f~%.0f 分钟，概率 %.2f）",
                             self._settings().min_interval_minutes,
                             self._settings().max_interval_minutes,
                             self._settings().probability)

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


    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """WebUI 保存配置后回调：只记日志，新配置在下一轮判定时自动生效。"""
        try:
            cfg = self._settings()
            self.ctx.logger.info(
                "主动搭话配置已更新：scope=%s version=%s enabled=%s 间隔=%.0f~%.0f 概率=%.2f 静默=%.0f 每日上限=%d",
                scope, version, cfg.enabled, cfg.min_interval_minutes,
                cfg.max_interval_minutes, cfg.probability, cfg.quiet_minutes, cfg.max_per_day,
            )
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
            self.ctx.logger.debug("主动搭话：没有可用群聊流")
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
