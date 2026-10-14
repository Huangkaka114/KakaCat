"""预留的业务接口。

当前版本不接入任何真实业务接口：这里只定义参数载体与适配器的形状，
方便未来接入时不需要改动下载核心。**本模块不会发起任何网络请求，
也不会推测两个可选参数的业务含义。**

未来实现步骤：

1. 新建一个 :class:`BusinessAdapter` 的子类，实现 :meth:`~BusinessAdapter.execute`；
2. 在 :func:`get_default_adapter` 中返回该实现；
3. 需要登录态时，把凭据交给一个新的 ``requests.Session``，再通过
   :func:`hls_downloader.downloader.download_hls_video` 的 ``session``
   参数传入 —— 下载核心本身不需要修改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessParams:
    """界面收集到的预留参数。

    界面只负责原样携带这两个值，不把它们拼接到 URL、请求头或 Cookie 中。
    """

    optional_a: str | None = None
    optional_b: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.optional_a or "").strip() and not (self.optional_b or "").strip()


class BusinessAdapter(ABC):
    """未来业务接口的占位基类。"""

    #: 展示给用户的适配器名称
    name: str = "未接入"

    @abstractmethod
    def execute(self, optional_a: str | None, optional_b: str | None):
        """未来业务接口的占位方法。

        当前版本不发起网络请求、不猜测参数含义、不执行任何实际业务逻辑。
        """
        raise NotImplementedError("业务接口尚未实现")


class NullBusinessAdapter(BusinessAdapter):
    """默认实现：明确拒绝执行，避免调用方误以为业务已经接入。"""

    name = "未接入（当前版本不做任何处理）"

    def execute(self, optional_a: str | None, optional_b: str | None):
        raise NotImplementedError("业务接口尚未实现，当前版本不会发起任何业务请求。")


def get_default_adapter() -> BusinessAdapter:
    """返回当前生效的业务适配器。"""
    return NullBusinessAdapter()
