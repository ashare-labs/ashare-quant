"""The installed package is the executor: source bytes are its portable identity."""

from ashare_quant import __version__
from ashare_quant.identity import source_sha256

EXECUTION_VERSION = __version__
EXECUTION_SOURCE_SHA256 = source_sha256()
