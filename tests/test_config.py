"""Smoke tests for app.services.chat.config — env-var override + sensible defaults."""
from __future__ import annotations

import importlib

import pytest

import app.services.chat.config as config_module


@pytest.fixture(autouse=True)
def reload_config_around_each_test(monkeypatch):
    """Reload the config module before AND after each test so env-var overrides
    set inside one test don't leak into the next."""
    importlib.reload(config_module)
    yield
    importlib.reload(config_module)


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_defaults_are_sensible():
    cfg = importlib.reload(config_module)
    # History
    assert cfg.HISTORY_TURN_PAIRS == 5
    assert cfg.HISTORY_MAX_CHARS == 6000
    assert cfg.CLARIFY_HISTORY_MSGS == 4
    assert cfg.SESSION_TURN_WINDOW == 20
    # COMPARE
    assert cfg.COMPARE_MAX_FULL == 5
    assert cfg.COMPARE_MAX_SUMMARY == 15
    # Retrieval
    assert cfg.SEARCH_TOP_K == 10
    assert cfg.RETRIEVAL_RRF_K == 60
    assert cfg.RETRIEVAL_POOL_MULTIPLIER == 2
    # Token budgets
    assert cfg.MAX_TOKENS_ROUTER == 400
    assert cfg.MAX_TOKENS_META == 600
    assert cfg.MAX_TOKENS_SEARCH == 600
    assert cfg.MAX_TOKENS_STRUCTURED_LLM == 900
    assert cfg.MAX_TOKENS_HYBRID == 900
    assert cfg.MAX_TOKENS_COMPARE == 1000
    assert cfg.MAX_TOKENS_GENERAL_GK == 500
    assert cfg.MAX_TOKENS_CLARIFY == 200
    # Temperatures
    assert cfg.TEMPERATURE_ROUTER == 0.0
    assert cfg.TEMPERATURE_ANSWER == 0.3
    assert cfg.TEMPERATURE_GENERAL_GK == 0.4
    assert cfg.TEMPERATURE_CLARIFY == 0.3
    # UI
    assert cfg.MAX_SOURCE_CARDS == 5
    # RBAC
    assert cfg.RBAC_WITHIN_DAYS == 30
    assert cfg.RBAC_MAX_MEETINGS == 30


# ── Env-var overrides — int ───────────────────────────────────────────────────

@pytest.mark.parametrize("env_name, attr", [
    ("CHAT_HISTORY_TURN_PAIRS", "HISTORY_TURN_PAIRS"),
    ("CHAT_HISTORY_MAX_CHARS", "HISTORY_MAX_CHARS"),
    ("CHAT_CLARIFY_HISTORY_MSGS", "CLARIFY_HISTORY_MSGS"),
    ("CHAT_SESSION_TURN_WINDOW", "SESSION_TURN_WINDOW"),
    ("CHAT_COMPARE_MAX_FULL", "COMPARE_MAX_FULL"),
    ("CHAT_COMPARE_MAX_SUMMARY", "COMPARE_MAX_SUMMARY"),
    ("CHAT_SEARCH_TOP_K", "SEARCH_TOP_K"),
    ("CHAT_RETRIEVAL_RRF_K", "RETRIEVAL_RRF_K"),
    ("CHAT_RETRIEVAL_POOL_MULTIPLIER", "RETRIEVAL_POOL_MULTIPLIER"),
    ("CHAT_MAX_TOKENS_ROUTER", "MAX_TOKENS_ROUTER"),
    ("CHAT_MAX_TOKENS_META", "MAX_TOKENS_META"),
    ("CHAT_MAX_TOKENS_SEARCH", "MAX_TOKENS_SEARCH"),
    ("CHAT_MAX_TOKENS_STRUCTURED_LLM", "MAX_TOKENS_STRUCTURED_LLM"),
    ("CHAT_MAX_TOKENS_HYBRID", "MAX_TOKENS_HYBRID"),
    ("CHAT_MAX_TOKENS_COMPARE", "MAX_TOKENS_COMPARE"),
    ("CHAT_MAX_TOKENS_GENERAL_GK", "MAX_TOKENS_GENERAL_GK"),
    ("CHAT_MAX_TOKENS_CLARIFY", "MAX_TOKENS_CLARIFY"),
    ("CHAT_MAX_SOURCE_CARDS", "MAX_SOURCE_CARDS"),
    ("CHAT_RBAC_WITHIN_DAYS", "RBAC_WITHIN_DAYS"),
    ("CHAT_RBAC_MAX_MEETINGS", "RBAC_MAX_MEETINGS"),
])
def test_each_int_constant_can_be_overridden_via_env(monkeypatch, env_name, attr):
    monkeypatch.setenv(env_name, "42")
    cfg = importlib.reload(config_module)
    assert getattr(cfg, attr) == 42


# ── Env-var overrides — float ─────────────────────────────────────────────────

@pytest.mark.parametrize("env_name, attr", [
    ("CHAT_TEMPERATURE_ROUTER", "TEMPERATURE_ROUTER"),
    ("CHAT_TEMPERATURE_ANSWER", "TEMPERATURE_ANSWER"),
    ("CHAT_TEMPERATURE_GENERAL_GK", "TEMPERATURE_GENERAL_GK"),
    ("CHAT_TEMPERATURE_CLARIFY", "TEMPERATURE_CLARIFY"),
])
def test_each_float_constant_can_be_overridden_via_env(monkeypatch, env_name, attr):
    monkeypatch.setenv(env_name, "0.7")
    cfg = importlib.reload(config_module)
    assert getattr(cfg, attr) == pytest.approx(0.7)


# ── Bad env values fall back, don't crash ─────────────────────────────────────

def test_invalid_int_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_TURN_PAIRS", "not-an-int")
    cfg = importlib.reload(config_module)
    assert cfg.HISTORY_TURN_PAIRS == 5


def test_invalid_float_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CHAT_TEMPERATURE_ANSWER", "warm")
    cfg = importlib.reload(config_module)
    assert cfg.TEMPERATURE_ANSWER == pytest.approx(0.3)


def test_blank_int_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CHAT_COMPARE_MAX_FULL", "")
    cfg = importlib.reload(config_module)
    assert cfg.COMPARE_MAX_FULL == 5
