from abc import ABC, abstractmethod

from app.models import PlatformState, Watch
from app.schemas import PlatformResult, RawAdapterData
from app.services.matching import filter_shows


class ConfigurationRequiredError(ValueError):
    phase = "configuration"


class DiscoveryFailedError(RuntimeError):
    phase = "discovery"


class TicketPlatformAdapter(ABC):
    name: str
    forced_direct_url: str | None = None

    @abstractmethod
    async def raw_search(self, watch: Watch) -> RawAdapterData:
        """Return parsed shows, diagnostics, evidence classification, and safe metadata."""

    async def search(self, watch: Watch) -> PlatformResult:
        from app.platforms.parsing import BlockedPageError

        try:
            raw = await self.raw_search(watch)
            matches, reasons = filter_shows(watch, raw.shows)
            reason = "; ".join(raw.diagnostics + reasons)[:4000]
            status = (
                PlatformState.AVAILABLE
                if matches
                else PlatformState.UNAVAILABLE
                if raw.shows
                else raw.empty_status
            )
            return PlatformResult(
                self.name,
                status,
                matches,
                reason=reason,
                screenshot_path=raw.screenshot_path,
                checked_url=getattr(self, "_checked_url", ""),
                phase="matching" if raw.shows else raw.phase,
                configured_mode=raw.configured_mode,
                supplied_url=raw.supplied_url,
                discovered_url=raw.discovered_url,
                final_url=raw.final_url,
                page_outcome=raw.page_outcome,
                page_title=raw.page_title,
                structured_sources=raw.structured_sources,
                raw_candidate_count=len(raw.shows),
                matching_count=len(matches),
                block_classification=raw.block_classification,
                ray_id=raw.ray_id,
                parser_version=raw.parser_version,
            )
        except ConfigurationRequiredError as exc:
            return PlatformResult(
                self.name,
                PlatformState.CONFIGURATION_REQUIRED,
                error=str(exc),
                phase=exc.phase,
                checked_url=getattr(self, "_checked_url", ""),
                **getattr(self, "_diagnostic_context", {}),
            )
        except DiscoveryFailedError as exc:
            return PlatformResult(
                self.name,
                PlatformState.DISCOVERY_FAILED,
                error=str(exc),
                phase=exc.phase,
                checked_url=getattr(self, "_checked_url", ""),
                **getattr(self, "_diagnostic_context", {}),
            )
        except BlockedPageError as exc:
            context = getattr(self, "_diagnostic_context", {})
            return PlatformResult(
                self.name,
                PlatformState.BLOCKED,
                error=str(exc),
                screenshot_path=exc.screenshot_path,
                phase="page_loading",
                checked_url=getattr(self, "_checked_url", ""),
                final_url=exc.final_url,
                page_outcome=exc.page_outcome,
                page_title=exc.page_title,
                block_classification=exc.classification,
                ray_id=exc.ray_id,
                **context,
            )
        except Exception as exc:  # Adapter failures remain isolated.
            return PlatformResult(
                self.name,
                PlatformState.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                phase="check",
                checked_url=getattr(self, "_checked_url", ""),
                **getattr(self, "_diagnostic_context", {}),
            )
