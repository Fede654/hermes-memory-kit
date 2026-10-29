"""Hermes MemoryProvider entry point for the installable HMK repository."""


def register(ctx):
    from .provider import HMKMemoryProvider
    ctx.register_memory_provider(HMKMemoryProvider())
