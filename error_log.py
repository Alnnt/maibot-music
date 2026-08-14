"""音乐插件独立错误日志。"""

from __future__ import annotations

from pathlib import Path

import logging


class PluginErrorLog:
    """只写入错误信息的插件本地日志。"""

    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)

        self._logger = logging.Logger(f"maibot-music.error.{id(self)}", level=logging.ERROR)
        self._logger.propagate = False
        self._handler = logging.FileHandler(
            log_dir / "error.log",
            encoding="utf-8",
            delay=True,
            errors="backslashreplace",
        )
        self._handler.setLevel(logging.ERROR)
        self._handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self._logger.addHandler(self._handler)

    def error(self, message: str, *args: object) -> None:
        """写入一条错误日志。"""
        self._logger.error(message, *args)

    def exception(self, message: str, *args: object) -> None:
        """写入当前异常及其堆栈。"""
        self._logger.exception(message, *args)

    def close(self) -> None:
        """关闭文件句柄。"""
        self._logger.removeHandler(self._handler)
        self._handler.close()
