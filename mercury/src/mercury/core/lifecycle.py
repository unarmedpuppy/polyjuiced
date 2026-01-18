"""
Component lifecycle protocols.

Defines standard interfaces for startable, stoppable, and health-checkable components.
"""
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class HealthStatus(str, Enum):
    """Component health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def healthy(cls, message: str = "OK", **details: Any) -> "HealthCheckResult":
        """Create a healthy result."""
        return cls(status=HealthStatus.HEALTHY, message=message, details=details)

    @classmethod
    def degraded(cls, message: str, **details: Any) -> "HealthCheckResult":
        """Create a degraded result."""
        return cls(status=HealthStatus.DEGRADED, message=message, details=details)

    @classmethod
    def unhealthy(cls, message: str, **details: Any) -> "HealthCheckResult":
        """Create an unhealthy result."""
        return cls(status=HealthStatus.UNHEALTHY, message=message, details=details)


@runtime_checkable
class Startable(Protocol):
    """Protocol for components that can be started."""

    @abstractmethod
    async def start(self) -> None:
        """Start the component.

        This method should:
        - Initialize any resources needed
        - Start any background tasks
        - Connect to external services

        Raises:
            Exception: If startup fails
        """
        ...

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Check if component is currently running."""
        ...


@runtime_checkable
class Stoppable(Protocol):
    """Protocol for components that can be stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the component gracefully.

        This method should:
        - Cancel any background tasks
        - Close connections
        - Flush pending data
        - Release resources
        """
        ...


@runtime_checkable
class HealthCheckable(Protocol):
    """Protocol for components that can report their health."""

    @abstractmethod
    async def health_check(self) -> HealthCheckResult:
        """Check component health.

        Returns:
            HealthCheckResult indicating current health status
        """
        ...


class Component(Startable, Stoppable, HealthCheckable, Protocol):
    """Full component protocol combining all lifecycle interfaces."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Get component name."""
        ...


class BaseComponent:
    """Base class for components providing common lifecycle functionality.

    Subclasses should override:
    - _do_start() - Component-specific startup logic
    - _do_stop() - Component-specific shutdown logic
    - _do_health_check() - Component-specific health check

    Usage:
        class MyService(BaseComponent):
            async def _do_start(self) -> None:
                await self._connect_to_database()

            async def _do_stop(self) -> None:
                await self._close_connection()

            async def _do_health_check(self) -> HealthCheckResult:
                if self._db.is_connected:
                    return HealthCheckResult.healthy()
                return HealthCheckResult.unhealthy("Database disconnected")
    """

    def __init__(self, name: Optional[str] = None) -> None:
        """Initialize component.

        Args:
            name: Component name (defaults to class name)
        """
        self._name = name or self.__class__.__name__
        self._running = False
        self._started_at: Optional[datetime] = None

    @property
    def name(self) -> str:
        """Get component name."""
        return self._name

    @property
    def is_running(self) -> bool:
        """Check if component is running."""
        return self._running

    @property
    def uptime_seconds(self) -> float:
        """Get component uptime in seconds."""
        if not self._started_at:
            return 0.0
        return (datetime.utcnow() - self._started_at).total_seconds()

    async def start(self) -> None:
        """Start the component."""
        if self._running:
            return
        await self._do_start()
        self._running = True
        self._started_at = datetime.utcnow()

    async def stop(self) -> None:
        """Stop the component."""
        if not self._running:
            return
        await self._do_stop()
        self._running = False

    async def health_check(self) -> HealthCheckResult:
        """Check component health."""
        if not self._running:
            return HealthCheckResult.unhealthy("Component not running")
        return await self._do_health_check()

    async def _do_start(self) -> None:
        """Component-specific startup logic. Override in subclass."""
        pass

    async def _do_stop(self) -> None:
        """Component-specific shutdown logic. Override in subclass."""
        pass

    async def _do_health_check(self) -> HealthCheckResult:
        """Component-specific health check. Override in subclass."""
        return HealthCheckResult.healthy(uptime_seconds=self.uptime_seconds)
