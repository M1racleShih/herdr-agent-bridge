"""Capabilities declared by each coding-agent adapter."""

from dataclasses import dataclass

from herdr_agent_bridge.bridge_common import BridgeError


@dataclass(frozen=True)
class AgentCapabilities:
    development_runtimes: frozenset[str]
    native_commands: frozenset[str]


AGENT_CAPABILITIES = {
    "codex": AgentCapabilities(frozenset({"native"}), frozenset({"/fast", "/goal", "/model"})),
    "pi": AgentCapabilities(frozenset({"isolated", "native"}), frozenset({"/name"})),
}


def require_development_runtime(agent: str, runtime: str) -> None:
    capabilities = AGENT_CAPABILITIES.get(agent)
    if capabilities is None or runtime not in capabilities.development_runtimes:
        raise BridgeError(
            f"{agent} development is unsupported in the {runtime} runtime",
            code="execution_policy_invalid",
        )
