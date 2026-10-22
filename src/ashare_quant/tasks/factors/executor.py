"""Factor runner using the existing isolated process and resource supervisor."""

from pathlib import Path

from ashare_quant.tasks.minute.executor import Executor as Base


class Executor(Base):
    def runner(self):
        return Path(__file__).with_name("runner.py")
