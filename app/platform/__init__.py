from .contracts import PlatformAdapter
from .windows.path_provider import WindowsPathAdapter
from .linux.path_provider import LinuxPathAdapter

def platform_adapter() -> PlatformAdapter:
    import platform

    if platform.system() == "Windows":
        return WindowsPathAdapter()
    return LinuxPathAdapter()

__all__ = ["PlatformAdapter", "WindowsPathAdapter", "LinuxPathAdapter", "platform_adapter"]
