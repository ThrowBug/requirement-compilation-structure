"""Text and visual model transport; stage-specific prompts remain with their stages."""

REQUEST_MAX_RETRIES = 5


class ProviderTransportUnavailable(Exception):
    """The model request failed after the provider client's retry policy."""
