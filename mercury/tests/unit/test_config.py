"""
Unit tests for ConfigManager.

Tests verify:
- TOML loading
- Environment variable overrides
- Reload functionality with callbacks
- Type-specific getters
"""
import os
import tempfile
from pathlib import Path
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from mercury.core.config import ConfigManager


class TestConfigBasics:
    """Tests for basic ConfigManager functionality."""

    def test_empty_config(self):
        """Verify ConfigManager works without a config file."""
        config = ConfigManager()
        assert config.get("any.key") is None
        assert config.get("any.key", "default") == "default"

    def test_load_toml_file(self):
        """Verify ConfigManager loads TOML config file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
[mercury]
log_level = "debug"
dry_run = true

[strategies.gabagool]
enabled = true
min_spread = 0.015
""")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)
            assert config.get("mercury.log_level") == "debug"
            assert config.get("mercury.dry_run") is True
            assert config.get("strategies.gabagool.enabled") is True
            assert config.get("strategies.gabagool.min_spread") == 0.015
        finally:
            os.unlink(config_path)

    def test_get_with_default(self):
        """Verify default values work correctly."""
        config = ConfigManager()
        assert config.get("missing.key", "default_value") == "default_value"
        assert config.get("missing.key", 42) == 42

    def test_get_bool(self):
        """Verify get_bool returns correct boolean values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
[settings]
flag_true = true
flag_false = false
""")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)
            assert config.get_bool("settings.flag_true") is True
            assert config.get_bool("settings.flag_false") is False
            assert config.get_bool("settings.missing", default=True) is True
        finally:
            os.unlink(config_path)

    def test_get_decimal(self):
        """Verify get_decimal returns Decimal values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
[trading]
min_spread = 0.015
max_size = 25.50
""")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)
            assert config.get_decimal("trading.min_spread") == Decimal("0.015")
            assert config.get_decimal("trading.max_size") == Decimal("25.50")
            assert config.get_decimal("trading.missing", Decimal("100")) == Decimal("100")
        finally:
            os.unlink(config_path)


class TestEnvOverrides:
    """Tests for environment variable overrides."""

    def test_env_overrides_toml(self):
        """Verify environment variables override TOML values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
[mercury]
log_level = "info"
""")
            f.flush()
            config_path = Path(f.name)

        try:
            # Set environment variable
            os.environ["MERCURY_MERCURY_LOG_LEVEL"] = "debug"

            config = ConfigManager(config_path=config_path)
            assert config.get("mercury.log_level") == "debug"
        finally:
            os.unlink(config_path)
            del os.environ["MERCURY_MERCURY_LOG_LEVEL"]

    def test_env_boolean_parsing(self):
        """Verify environment variable boolean parsing."""
        os.environ["MERCURY_TEST_FLAG"] = "true"

        try:
            config = ConfigManager()
            assert config.get("test.flag") is True
        finally:
            del os.environ["MERCURY_TEST_FLAG"]


class TestReloadCallbacks:
    """Tests for configuration reload callback functionality."""

    def test_register_reload_callback(self):
        """Verify callbacks can be registered."""
        config = ConfigManager()
        callback = MagicMock()

        config.register_reload_callback(callback)

        assert callback in config._reload_callbacks

    def test_unregister_reload_callback(self):
        """Verify callbacks can be unregistered."""
        config = ConfigManager()
        callback = MagicMock()

        config.register_reload_callback(callback)
        config.unregister_reload_callback(callback)

        assert callback not in config._reload_callbacks

    def test_unregister_nonexistent_callback(self):
        """Verify unregistering non-existent callback is safe."""
        config = ConfigManager()
        callback = MagicMock()

        # Should not raise
        config.unregister_reload_callback(callback)

    def test_reload_triggers_callbacks(self):
        """Verify reload() triggers all registered callbacks."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
[mercury]
value = "original"
""")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)

            callback1 = MagicMock()
            callback2 = MagicMock()
            config.register_reload_callback(callback1)
            config.register_reload_callback(callback2)

            # Modify the file
            with open(config_path, "w") as f:
                f.write("""
[mercury]
value = "updated"
""")

            # Reload
            config.reload()

            # Both callbacks should be called
            assert callback1.called
            assert callback2.called

            # Check callback arguments (old_config, new_config)
            old_config, new_config = callback1.call_args[0]
            assert old_config["mercury"]["value"] == "original"
            assert new_config["mercury"]["value"] == "updated"

        finally:
            os.unlink(config_path)

    def test_reload_with_no_file(self):
        """Verify reload() is safe when no config file exists."""
        config = ConfigManager()
        callback = MagicMock()
        config.register_reload_callback(callback)

        # Should not raise
        config.reload()

        # Callback should not be called
        assert not callback.called

    def test_callback_exception_does_not_stop_others(self):
        """Verify callback exception doesn't prevent other callbacks."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("[test]\nvalue = 1")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)

            failing_callback = MagicMock(side_effect=RuntimeError("Callback error"))
            success_callback = MagicMock()

            config.register_reload_callback(failing_callback)
            config.register_reload_callback(success_callback)

            # Update file
            with open(config_path, "w") as f:
                f.write("[test]\nvalue = 2")

            # Reload - should not raise
            config.reload()

            # Both callbacks should be called
            assert failing_callback.called
            assert success_callback.called

        finally:
            os.unlink(config_path)

    def test_multiple_reloads(self):
        """Verify callbacks are called on each reload."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("[test]\nvalue = 1")
            f.flush()
            config_path = Path(f.name)

        try:
            config = ConfigManager(config_path=config_path)
            callback = MagicMock()
            config.register_reload_callback(callback)

            # First reload
            with open(config_path, "w") as f:
                f.write("[test]\nvalue = 2")
            config.reload()

            # Second reload
            with open(config_path, "w") as f:
                f.write("[test]\nvalue = 3")
            config.reload()

            # Callback should be called twice
            assert callback.call_count == 2

        finally:
            os.unlink(config_path)
