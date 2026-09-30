import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from app import config

log = logging.getLogger("app")


def _setup_logging() -> None:
    handler = RotatingFileHandler(
        os.path.join(config.LOG_DIR, "smart-smoke-detector.log"),
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=config.LOG_LEVEL, handlers=[handler])
    # PySide6 prints exceptions raised in slots to stderr and carries on; keep them in the log too.
    sys.excepthook = lambda *exc: log.error("Unhandled exception", exc_info=exc)


def main() -> int:
    _setup_logging()
    if "--demo" in sys.argv:
        config.DEMO = True

    # Import Qt only after cheap setup so failures above are reported quickly.
    from PySide6.QtWidgets import QApplication

    from app.ui import theme
    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setStyleSheet(theme.STYLESHEET)
    window = MainWindow()
    # SIGTERM (shutdown, logout, `kill`) and Ctrl+C: close normally so the BLE link disconnects
    # and the event log closes its open episodes. The 1 Hz clock timer lets Python see the signal.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: window.close())
    if config.FULLSCREEN:
        window.showFullScreen()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
