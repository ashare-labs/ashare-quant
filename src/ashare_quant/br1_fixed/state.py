"""Private state owned by one fresh interpreter, never an input channel."""
session = {}


class CompletedReceipt(BaseException):
    def __init__(self, receipt):
        self.receipt = receipt
