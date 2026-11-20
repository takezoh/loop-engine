"""Which test classes of an adapter that needs the real operating system exist on this platform."""
from provider_transport.host_platform import is_windows


def runs_on(platform):
    """Keep a test class of an adapter that works only against the real system ("posix" or "windows") on the
    platform whose selection starts that adapter (`create_cell_membership`, `create_cwd_authority`).

    Elsewhere no code can start the adapter, so the class is not a test there: its name is bound to None,
    which test discovery and the port registry pass over. An adapter whose system calls a fake stands in for
    is tested on every platform instead, as the Windows cell and authority are."""
    def keep(test_class):
        return test_class if (platform == "windows") == is_windows() else None
    return keep
