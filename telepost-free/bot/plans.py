"""Тарифы. Всё базовое бесплатно, платные только «тяжёлые» вещи (ИИ и объёмы)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    name: str
    max_channels: int
    max_pending_posts: int
    rewrites_per_day: int
    max_feeds: int


# Публикация сразу, отложка и премиум-эмодзи/стикеры бесплатны на любом тарифе:
# при copy_message Telegram сохраняет все entity исходного сообщения.
FREE = Plan("Free", max_channels=3, max_pending_posts=15, rewrites_per_day=5, max_feeds=2)
PRO = Plan("Pro", max_channels=25, max_pending_posts=1000, rewrites_per_day=150, max_feeds=30)