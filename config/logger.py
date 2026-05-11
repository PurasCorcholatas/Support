import logging
import sys

def setup_logger(name: str = "support_ia") -> logging.Logger:
    """Configura y retorna el logger centralizado del proyecto."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


# Logger principal — importar desde cualquier archivo:
# from config.logger import logger
logger = setup_logger()
