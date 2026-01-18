"""
Configuration management with TOML + environment variable support.

Configuration hierarchy (later overrides earlier):
1. Default values in code
2. TOML file
3. Environment variables (MERCURY_* prefix)
"""
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


class ConfigManager:
    """Centralized configuration with TOML + env var support.

    Usage:
        config = ConfigManager(Path("config/default.toml"))
        log_level = config.get("mercury.log_level")
        spread = config.get("strategies.gabagool.min_spread_threshold", 0.015)
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        env_prefix: str = "MERCURY_",
    ) -> None:
        """Initialize ConfigManager.

        Args:
            config_path: Path to TOML config file (optional)
            env_prefix: Prefix for environment variable overrides
        """
        self._data: dict[str, Any] = {}
        self._env_prefix = env_prefix
        self._config_path = config_path

        if config_path and config_path.exists():
            self._load_toml(config_path)

    def _load_toml(self, path: Path) -> None:
        """Load configuration from TOML file."""
        with open(path, "rb") as f:
            self._data = tomllib.load(f)

    def _get_nested(self, data: dict[str, Any], key: str) -> tuple[bool, Any]:
        """Get a nested value using dot notation.

        Returns (found, value) tuple.
        """
        parts = key.split(".")
        current = data

        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]

        return True, current

    def _get_env_value(self, key: str) -> tuple[bool, Any]:
        """Get value from environment variable.

        Converts key like "mercury.dry_run" to "MERCURY_DRY_RUN".
        """
        env_key = self._env_prefix + key.upper().replace(".", "_")
        if env_key in os.environ:
            value = os.environ[env_key]
            return True, self._parse_env_value(value)
        return False, None

    def _parse_env_value(self, value: str) -> Any:
        """Parse environment variable string to appropriate type."""
        # Boolean
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        if value.lower() in ("false", "0", "no", "off"):
            return False

        # Number
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            pass

        # List (comma-separated)
        if "," in value:
            return [v.strip() for v in value.split(",")]

        return value

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value with dot notation.

        Environment variables take precedence over TOML values.

        Args:
            key: Dot-notation key like "mercury.log_level"
            default: Default value if key not found

        Returns:
            Configuration value
        """
        # Check environment first (highest priority)
        found, value = self._get_env_value(key)
        if found:
            return value

        # Check TOML data
        found, value = self._get_nested(self._data, key)
        if found:
            return value

        return default

    def get_section(self, section: str) -> dict[str, Any]:
        """Get entire configuration section.

        Args:
            section: Dot-notation path to section

        Returns:
            Dictionary of section values
        """
        found, value = self._get_nested(self._data, section)
        if found and isinstance(value, dict):
            return value
        return {}

    def get_decimal(self, key: str, default: Decimal = Decimal("0")) -> Decimal:
        """Get configuration value as Decimal.

        Args:
            key: Dot-notation key
            default: Default Decimal value

        Returns:
            Decimal value
        """
        value = self.get(key)
        if value is None:
            return default
        return Decimal(str(value))

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get configuration value as boolean.

        Args:
            key: Dot-notation key
            default: Default boolean value

        Returns:
            Boolean value
        """
        value = self.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    def get_int(self, key: str, default: int = 0) -> int:
        """Get configuration value as integer.

        Args:
            key: Dot-notation key
            default: Default integer value

        Returns:
            Integer value
        """
        value = self.get(key)
        if value is None:
            return default
        return int(value)

    def get_list(self, key: str, default: Optional[list[Any]] = None) -> list[Any]:
        """Get configuration value as list.

        Args:
            key: Dot-notation key
            default: Default list value

        Returns:
            List value
        """
        if default is None:
            default = []
        value = self.get(key)
        if value is None:
            return default
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [v.strip() for v in value.split(",")]
        return [value]

    def reload(self) -> None:
        """Reload configuration from TOML file.

        Useful for hot-reloading configuration changes.
        """
        if self._config_path and self._config_path.exists():
            self._load_toml(self._config_path)

    @property
    def raw_data(self) -> dict[str, Any]:
        """Get raw configuration data (for debugging)."""
        return self._data.copy()
